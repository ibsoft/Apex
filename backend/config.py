"""Central configuration for the APEX assistant backend.

Every value can be overridden with environment variables (see .env.example).
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


class Config:
    # --- Server -------------------------------------------------------------
    HOST = os.getenv("HOST", "0.0.0.0")
    PORT = _int("PORT", 5001)
    SECRET_KEY = os.getenv(
        "SECRET_KEY", "dev-change-me-" + os.urandom(8).hex()
    )
    BASE_URL = os.getenv("BASE_URL", f"http://localhost:{PORT}")
    FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
    DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
    DB_PATH = Path(os.getenv("DB_PATH", DATA_DIR / "apex.db"))
    CHROMA_DIR = str(DATA_DIR / "chroma")

    # --- OpenAI OAuth (Sign in with ChatGPT / OpenAI) ----------------------
    # Register an OAuth app at https://platform.openai.com -> Apps -> OAuth.
    OPENAI_CLIENT_ID = os.getenv("OPENAI_CLIENT_ID", "")
    OPENAI_CLIENT_SECRET = os.getenv("OPENAI_CLIENT_SECRET", "")
    OPENAI_REDIRECT_URI = os.getenv(
        "OPENAI_REDIRECT_URI", f"{BASE_URL}/api/auth/callback"
    )
    # Scopes used for the consent screen. `offline_access` yields a refresh
    # token so the server can mint fresh access tokens without the user.
    OPENAI_OAUTH_SCOPE = os.getenv(
        "OPENAI_OAUTH_SCOPE", "openid profile email offline_access"
    )
    OPENAI_AUTHORIZE_URL = os.getenv(
        "OPENAI_AUTHORIZE_URL", "https://auth.openai.com/oauth/authorize"
    )
    OPENAI_TOKEN_URL = os.getenv(
        "OPENAI_TOKEN_URL", "https://auth.openai.com/oauth/token"
    )
    OPENAI_JWKS_URL = os.getenv(
        "OPENAI_JWKS_URL",
        "https://auth.openai.com/.well-known/jwks.json",
    )
    OPENAI_ISSUER = os.getenv("OPENAI_ISSUER", "https://auth.openai.com")

    # --- Platform API ------------------------------------------------------
    # Server-side key. If the OAuth app is configured for "API on behalf of
    # users" (fine-grained tokens), set USE_OAUTH_ACCESS_KEY=1 and the
    # user's OAuth access token is used against the API instead.
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    USE_OAUTH_ACCESS_KEY = _bool("USE_OAUTH_ACCESS_KEY", False)

    # Agent defaults
    DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "gpt-4o-mini")
    # Model used when the user logged in with a ChatGPT subscription token.
    CHATGPT_MODEL = os.getenv("CHATGPT_MODEL", "gpt-5-codex")
    # Tools that need special permission (only enabled via env).
    ENABLE_RUN_PYTHON = _bool("ENABLE_RUN_PYTHON", False)
    MAX_TOOL_STEPS = _int("MAX_TOOL_STEPS", 12)

    # --- Voice assistant ---------------------------------------------------
    WAKE_WORD = os.getenv("WAKE_WORD", "apex")
    # Keep listening for follow-ups (without repeating the wake word) for this
    # many seconds after an utterance. 0 disables follow-up mode.
    FOLLOW_UP_SECONDS = _int("FOLLOW_UP_SECONDS", 30)
    VOICE = os.getenv("VOICE", "")  # e.g. "Google UK English Female" (empty = default)

    # --- Agent engine ------------------------------------------------------
    # One of: responses | agents_sdk | langgraph   (build one, run all)
    AGENT_ENGINE = os.getenv("AGENT_ENGINE", "responses").strip().lower()

    # --- Local models ------------------------------------------------------
    # Default backend at boot; can be changed at runtime via /api/settings.
    PROVIDER_DEFAULT = os.getenv("PROVIDER_DEFAULT", "openai")
    OLLAMA_BASE_URL = os.getenv(
        "OLLAMA_BASE_URL", "http://localhost:11434/v1"
    )
    OLLAMA_EMBED_MODEL = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
    TORCH_EXTRA = {
        "device_map": os.getenv("TORCH_DEVICE", "auto"),      # cuda:0 | mps | auto
        "max_new_tokens": _int("TORCH_MAX_TOKENS", 512),
        "do_sample": _bool("TORCH_SAMPLE", True),
        "top_p": float(os.getenv("TORCH_TOP_P", "0.95")),
    }

    # --- Kimi (Moonshot AI - OpenAI-compatible API) ------------------------
    KIMI_API_KEY = os.getenv("KIMI_API_KEY", "")
    KIMI_BASE_URL = os.getenv("KIMI_BASE_URL", "https://api.moonshot.ai/v1")
    KIMI_MODEL = os.getenv("KIMI_MODEL", "kimi-k2-instruct")
    KIMI_MODELS = [
        m.strip()
        for m in os.getenv(
            "KIMI_MODELS",
            "kimi-k2-instruct,kimi-k2-thinking,kimi-k2-turbo-preview,"
            "kimi-k2-0905-preview,kimi-k1.5,moonshot-v1-32k,moonshot-v1-128k",
        ).split(",")
        if m.strip()
    ]

    # --- Memory (vector store) --------------------------------------------
    MEMORY_ENABLED = _bool("MEMORY_ENABLED", True)
    EMBEDDING_BACKEND = os.getenv("EMBEDDING_BACKEND", "auto")  # auto|openai|hash
    EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
    MEMORY_RECALL_DEFAULT = _int("MEMORY_RECALL_DEFAULT", 5)
    MEMORY_SUMMARIZE = _bool("MEMORY_SUMMARIZE", True)

    # --- Dev mode ---------------------------------------------------------
    # Skips OpenAI OAuth: the UI signs in as a fixed local user so the whole
    # stack (chat, memory, settings) runs against local/KO providers.
    # DEV_AUTO_LOGIN=<name> signs in as that name; DEV_MODE=true auto-enables
    # only while OAuth is not configured.
    DEV_MODE = _bool("DEV_MODE", False)
    DEV_AUTO_LOGIN = os.getenv("DEV_AUTO_LOGIN", "").strip()

    @property
    def dev_auto_login(self) -> bool:
        return bool(self.DEV_AUTO_LOGIN) or (
            self.DEV_MODE and not self.oauth_configured and not self.OPENAI_API_KEY
        )

    @property
    def agent_engines_available(self) -> list:
        engines = ["responses"]
        try:
            import agents  # noqa: F401

            engines.append("agents_sdk")
        except ImportError:
            pass
        try:
            import langgraph  # noqa: F401

            engines.append("langgraph")
        except ImportError:
            pass
        return engines

    @property
    def oauth_configured(self) -> bool:
        return bool(self.OPENAI_CLIENT_ID)


config = Config()