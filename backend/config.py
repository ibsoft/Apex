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
    # Public backend address used for ALL hosted file/media links and OAuth callbacks.
    BASE_URL = os.getenv("BASE_URL", f"http://localhost:{PORT}").strip().rstrip("/")
    FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
    DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
    DB_PATH = Path(os.getenv("DB_PATH", DATA_DIR / "apex.db"))
    CHROMA_DIR = str(DATA_DIR / "chroma")
    # Colon-separated search roots; OS permissions are always respected.
    FILE_SEARCH_ROOTS = os.getenv("FILE_SEARCH_ROOTS", "/")

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
    # When the active skill is "general", ask the model to pick the best
    # specialist skill for the user message and route the turn through it.
    AUTO_ROUTE_FROM_GENERAL = _bool("AUTO_ROUTE_FROM_GENERAL", True)
    # Model used when the user logged in with a ChatGPT subscription token.
    CHATGPT_MODEL = os.getenv("CHATGPT_MODEL", "gpt-5-codex")
    # Model used by the "code" skill when no runtime/user model is selected.
    CODE_MODEL = os.getenv("CODE_MODEL", CHATGPT_MODEL)
    # Tools that need special permission (only enabled via env).
    ENABLE_RUN_PYTHON = _bool("ENABLE_RUN_PYTHON", False)
    ENABLE_RUN_SHELL = _bool("ENABLE_RUN_SHELL", False)
    RUN_SHELL_TIMEOUT = _int("RUN_SHELL_TIMEOUT", 60)
    MAX_TOOL_STEPS = _int("MAX_TOOL_STEPS", 12)

    # --- Web search --------------------------------------------------------
    # These are configurable so the search tool does not rely on hardcoded
    # endpoints. Defaults use DuckDuckGo's HTML form endpoint.
    WEB_SEARCH_ENGINE = os.getenv("WEB_SEARCH_ENGINE", "duckduckgo").strip().lower()
    WEB_SEARCH_DDG_URL = os.getenv("WEB_SEARCH_DDG_URL", "https://html.duckduckgo.com/html/")
    WEB_SEARCH_DDG_REGION = os.getenv("WEB_SEARCH_DDG_REGION", "us-en")
    WEB_SEARCH_TIMEOUT = _int("WEB_SEARCH_TIMEOUT", 20)
    WEB_SEARCH_USER_AGENT = os.getenv(
        "WEB_SEARCH_USER_AGENT",
        "Mozilla/5.0 (X11; Linux x86_64) APEX-assistant/1.0",
    )

    # --- Voice assistant ---------------------------------------------------
    WAKE_WORD = os.getenv("WAKE_WORD", "apex")
    RESPONSE_LANGUAGE = os.getenv("RESPONSE_LANGUAGE", "en").strip().lower()
    # Keep listening for follow-ups (without repeating the wake word) for this
    # many seconds after an utterance. 0 disables follow-up mode.
    FOLLOW_UP_SECONDS = _int("FOLLOW_UP_SECONDS", 30)
    VOICE = os.getenv("VOICE", "Google UK English Female")

    # --- Agent engine ------------------------------------------------------
    # One of: responses | agents_sdk | langgraph   (build one, run all)
    AGENT_ENGINE = os.getenv("AGENT_ENGINE", "responses").strip().lower()

    # --- Local models ------------------------------------------------------
    # Default backend at boot; can be changed at runtime via /api/settings.
    CODEX_HOME = Path(os.getenv("APEX_CODEX_HOME", BASE_DIR / "data" / "codex"))
    CODEX_BINARY = os.getenv("CODEX_BINARY", "codex")
    CODEX_MODEL = os.getenv("CODEX_MODEL", "")
    PROVIDER_DEFAULT = os.getenv("PROVIDER_DEFAULT", "openai")
    OLLAMA_BASE_URL = os.getenv(
        "OLLAMA_BASE_URL", "http://localhost:11434/v1"
    )
    OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
    TORCH_MODEL = os.getenv("TORCH_MODEL", "Qwen/Qwen2.5-7B-Instruct")
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

    # --- Obsidian (local Markdown vault) -----------------------------------
    # Path to the folder containing your Obsidian .md notes, e.g. /home/user/Obsidian
    OBSIDIAN_VAULT_PATH = os.getenv("OBSIDIAN_VAULT_PATH", "")
    # Optional daily-notes folder inside the vault (empty = vault root).
    OBSIDIAN_DAILY_NOTES_FOLDER = os.getenv("OBSIDIAN_DAILY_NOTES_FOLDER", "")
    # strftime format for the daily note filename. Default: 2026-09-21.md
    OBSIDIAN_DAILY_NOTES_FORMAT = os.getenv("OBSIDIAN_DAILY_NOTES_FORMAT", "%Y-%m-%d")

    # --- EDITOR skill (Word / Excel generation) -----------------------------
    EDITOR_ENABLED = _bool("EDITOR_ENABLED", True)
    # Generated files are cleaned up after this many seconds.
    EDITOR_FILE_TTL_SECONDS = _int("EDITOR_FILE_TTL_SECONDS", 3600)

    # --- Image browser (optional local gallery) -----------------------------
    # Absolute path to a directory of images APEX can browse locally by voice.
    # Leave empty to disable local browsing; internet image search still works.
    IMAGES_DIR = os.getenv("IMAGES_DIR", "")

    # --- VAPT skill (authorized vulnerability assessment) -------------------
    # The VAPT skill runs a privileged command runner on the host. It is OFF
    # unless explicitly enabled (set VAPT_ENABLED=true in backend/.env).
    VAPT_ENABLED = _bool("VAPT_ENABLED", False)
    # Root folder where each assessment target gets its own project directory:
    #   $VAPT_PROJECTS/<target>/{artifacts,scripts,reports}
    VAPT_PROJECTS = Path(os.getenv("VAPT_PROJECTS", str(Path.home() / "VAPT")))
    # Maximum number of commands a scripted batch may run simultaneously.
    VAPT_MAX_PARALLEL = _int("VAPT_MAX_PARALLEL", 3)
    # Per-command timeout in seconds for vapt_run / vapt_script.
    VAPT_CMD_TIMEOUT = _int("VAPT_CMD_TIMEOUT", 300)
    # Report download links are cleaned up after this many seconds.
    VAPT_REPORT_TTL_SECONDS = _int("VAPT_REPORT_TTL_SECONDS", 3600)
    # Unsaved sudo passwords are kept (single-use window) for this many seconds.
    VAPT_SUDO_SINGLE_USE_SECONDS = _int("VAPT_SUDO_SINGLE_USE_SECONDS", 120)
    # "Save for this session" sudo password lifetime in minutes.
    # 0 = held until the user logs out or the server restarts (memory only).
    VAPT_SUDO_TTL_MINUTES = _int("VAPT_SUDO_TTL_MINUTES", 0)

    # --- CODE skill (developer projects / git / GitHub) ---------------------
    # Root folder where each coding project lives:
    #   $CODE_PROJECTS/<project>  (a git repository)
    CODE_PROJECTS = Path(os.getenv("CODE_PROJECTS", str(Path.home() / "Development" / "Projects")))
    # Default branch used by code_start / code_push.
    CODE_BRANCH = os.getenv("CODE_BRANCH", "main")
    # Per-user GitHub PAT storage (chmod 600). Inside $ROOT/.apex (gitignored).
    CODE_GITHUB_DIR = Path(
        os.getenv("CODE_GITHUB_DIR", str(Path(BASE_DIR).parent / ".apex" / "github_tokens"))
    )

    # --- Autonomous mode ----------------------------------------------------
    # When enabled, APEX may initiate interaction, propose actions and run
    # lightweight self-improvement checks while the user is idle.
    AUTONOMOUS_MODE = _bool("AUTONOMOUS_MODE", False)
    # 1 = serious, 100 = maximum jokes.
    HUMOR_LEVEL = _int("HUMOR_LEVEL", 30)
    # 1 = sincere, 100 = maximum sarcasm.
    SARCASM_LEVEL = _int("SARCASM_LEVEL", 20)
    # 0 = no autonomous voice interactions, 100 = full daily budget.
    AUTONOMOUS_VOICE_BUDGET = _int("AUTONOMOUS_VOICE_BUDGET", 50)

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