"""Per-conversation steer inbox.

A turn is one HTTP request that streams from the model, and the browser used to
freeze the composer until it finished. The operator can now keep typing: a plain
message is queued client-side and sent as its own turn afterwards, while a
``/steer`` instruction is meant for the generation that is *already running*.
This module is the server half of that second path.

The engine that knows how to consume steers marks the conversation active
(``begin``) for the length of the turn and clears it on the way out (``end``),
so ``push`` can report whether a consuming turn is actually running. The client
uses that answer only as a hint; the authoritative fallback is its own queue,
which still holds every steer until a ``steer_applied`` event confirms it was
folded into the running generation.

The inbox is deliberately dumb: it stores text keyed by conversation id and
knows nothing about ownership. The route checks the session against the
conversation before calling ``push``, exactly as ``/api/chat`` does.
"""
from __future__ import annotations

import threading


class SteerInbox:
    """Thread-safe map of conversation id -> pending steer instructions."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: set[str] = set()
        self._items: dict[str, list[str]] = {}

    def begin(self, conv_id: str) -> None:
        """Mark a conversation as having a turn that will consume steers."""
        if not conv_id:
            return
        with self._lock:
            self._active.add(conv_id)

    def end(self, conv_id: str) -> list[str]:
        """Clear the active flag and return (and drop) anything never drained.

        The leftovers are only for tests and diagnostics: the client still holds
        every steer until it sees ``steer_applied``, so a steer the turn did not
        consume is sent as an ordinary message afterwards and nothing is lost.
        """
        if not conv_id:
            return []
        with self._lock:
            self._active.discard(conv_id)
            return self._items.pop(conv_id, [])

    def push(self, conv_id: str, text: str) -> bool:
        """Queue an instruction; return whether a consuming turn is running."""
        text = (text or "").strip()
        if not conv_id or not text:
            return False
        with self._lock:
            self._items.setdefault(conv_id, []).append(text)
            return conv_id in self._active

    def drain(self, conv_id: str) -> list[str]:
        """Pop everything queued for a conversation (FIFO)."""
        if not conv_id:
            return []
        with self._lock:
            return self._items.pop(conv_id, [])

    def is_active(self, conv_id: str) -> bool:
        if not conv_id:
            return False
        with self._lock:
            return conv_id in self._active


#: Single process-wide inbox. Gunicorn runs one worker (see AGENTS.md), and even
#: with more, a steer that lands in another worker's inbox simply never gets the
#: ``steer_applied`` confirmation and is re-sent as a normal message.
INBOX = SteerInbox()


__all__ = ["SteerInbox", "INBOX"]
