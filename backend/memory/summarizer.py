"""Whole-conversation summarization into long-term memory.

Runs off the request path when the user leaves a conversation (new thread or
switching conversations). Produces one durable summary per conversation that the
embedding recall surfaces whenever the user later asks about it.

The summarizer is idempotent: its progress is checkpointed via the message id
of the last ingested turn, so re-summarizing the same thread (toggling between
conversations) is a cheap no-op unless new messages arrived.
"""
from __future__ import annotations

from config import config


def _substantive_user_length(msgs: list[dict]) -> int:
    """Length of the longest real user message, ignoring meta-only turns."""
    best = 0
    for m in msgs:
        if m.get("role") != "user":
            continue
        text = str(m.get("content") or "").strip()
        if text:
            best = max(best, len(text))
    return best


def _feed(db, conv_id: str, after_id: int) -> list[dict]:
    """User/assistant messages in order, restricted to new turns if a checkpoint exists."""
    msgs = db.list_messages(conv_id)
    feed = []
    for m in msgs:
        if m.get("role") not in ("user", "assistant"):
            continue
        content = str(m.get("content") or "").strip()
        if content:
            m = dict(m)
            m["content"] = content
            feed.append(m)
    if after_id:
        feed = [m for m in feed if (m.get("id") or 0) > after_id]
    return feed


def _meaningful(msgs: list[dict]) -> bool:
    return _substantive_user_length(msgs) >= 12


def _chunk(items: list[dict], size: int) -> list[list[dict]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _build_provider(provider_mgr, provider_name: str, rt):
    from models.providers import ProviderManager as PM

    pm = PM(
        bearer=provider_mgr.bearer,
        use_oauth_access=provider_mgr.use_oauth_access,
        runtime=rt,
    )
    model = rt.get("model")
    if not model:
        model = config.CODEX_MODEL if provider_name == "codex" else (
            rt.get("ollama_model") or rt.get("torch_model") or config.DEFAULT_MODEL)
    return pm.build(provider_name, model)


def _call_provider(provider, running: str, turns: list[dict], response_language: str, stamp: str = "") -> str:
    """Merge a running summary with a new window of turns in one LLM call."""
    language = {"en": "English", "el": "Greek"}.get(response_language, "English")
    transcript = "\n".join(f"{m['role'].title()}: {m['content']}" for m in turns)
    parts = [
        "You maintain a durable memory summary of a conversation with a user.",
        f"Write in {language} (keep any proper names, numbers and technical terms as-is).",
        "Include: the topic, the user's goals and preferences, decisions made, salient facts, and open items.",
        "Keep it compact and factual; no preamble, no headings.",
    ]
    if stamp:
        parts.append(f"The conversation happened on: {stamp}.")
    if running:
        parts.append(f"Existing summary:\n{running}")
    parts.append(f"Latest messages:\n{transcript}")
    prompt = "\n\n".join(parts)
    msgs = [{"role": "system", "content": prompt}]
    chunks = list(provider.chat_stream(msgs, None))
    return "".join(c["content"] for c in chunks if c["type"] == "text").strip()


def _conversation_stamp(db, conv, feed: list[dict]) -> tuple[str, str]:
    """Human-readable conversation date + raw epoch for metadata.

    Uses the last message time, falling back to the thread's updated/created
    time so Apex always knows when the conversation happened.
    """
    import time as _time

    ts = max((m.get("created_at") or 0) for m in feed)
    if not ts:
        ts = conv.get("updated_at") or conv.get("created_at") or _time.time()
    from datetime import datetime

    stamp = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    return stamp, float(ts)


def summarize_conversation(
    *,
    user_id: str,
    conversation_id: str,
    db,
    memory,
    provider_mgr,
    provider_name: str,
    rt,
    response_language: str = "en",
    min_messages: int = 4,
    window: int = 60,
) -> dict:
    """Summarize a thread into memory or return plain {status: reason}.

    Returns {"status": "summarized"} on success, or a descriptive skip reason
    (``skipped``/``no_new``/``empty``/``no_memory``/``not_owner``) so callers and
    tests can distinguish a genuine no-op from a failure.
    """
    if memory is None or db is None:
        return {"status": "no_memory"}

    conv = db.get_conversation(conversation_id)
    if not conv or conv.get("user_id") != user_id:
        return {"status": "not_owner"}

    existing = memory.conversation_summary(user_id, conversation_id)
    checkpoint = int((existing or {}).get("meta", {}).get("last_message_id") or 0)

    feed = _feed(db, conversation_id, after_id=checkpoint)
    if not feed:
        return {"status": "no_new" if existing else "empty"}
    if not _meaningful(feed):
        return {"status": "skipped"}

    # First-time summaries still need enough substance to be worth remembering.
    if not existing and len(feed) < min_messages:
        return {"status": "skipped"}

    stamp, epoch_ts = _conversation_stamp(db, conv, feed)

    provider = None
    try:
        provider = _build_provider(provider_mgr, provider_name, rt)
        running = (existing or {}).get("text") or ""
        last_id = 0
        wins = _chunk(feed, max(1, int(window)))
        for win in wins:
            running = _call_provider(provider, running, win, response_language, stamp)
            if win:
                last_id = max((m.get("id") or 0) for m in win)
        if not running:
            return {"status": "skipped"}
        memory.replace_conversation_summary(
            user_id,
            conversation_id,
            f"[{stamp}] {running}",
            meta={
                "title": str(conv.get("title") or ""),
                "last_message_id": last_id,
                "response_language": response_language,
                # When the conversation took place (used with created_at above).
                "conversation_at": epoch_ts,
                "conversation_date": stamp,
            },
        )
        return {"status": "summarized"}
    except Exception:
        return {"status": "failure"}
    finally:
        if provider is not None:
            try:
                provider.close()
            except Exception:
                pass