"""SOUL.md: the operator-authored persona appended to every system prompt.

The text lives in the `soul` runtime setting (edited from the settings tab and
persisted like any other setting). It is trusted operator configuration — it is
meant to be followed, unlike the auto-injected memory recall or the client-side
`window_context`, which are user *data*.
"""

from config import config

SOUL_HEADER = "## SOUL.md (operator-authored persona — follow it)"

_PREAMBLE = (
    "The operator wrote the following standing instructions. They define your "
    "personality, tone and preferences, and apply to every reply. Follow them "
    "unless the operator explicitly overrides them in the current conversation."
)


def normalize_soul(value) -> str:
    """Clamp and clean raw setting input so the block is always safe to embed."""
    if not isinstance(value, str):
        return ""
    # A pasted file must not smuggle in CRLF or a wall of blank lines.
    text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    limit = max(0, int(config.SOUL_MAX_CHARS))
    if limit and len(text) > limit:
        text = text[:limit].rstrip()
    return text


def soul_prompt_block(value) -> str:
    """Return the prompt block for a SOUL.md value, or "" when unset."""
    text = normalize_soul(value)
    if not text:
        return ""
    return f"{SOUL_HEADER}\n{_PREAMBLE}\n\n{text}"
