"""Central configuration for the APEX assistant backend.

Every value can be overridden with environment variables (see .env.example).
"""
import os
import secrets
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


# DATA_DIR is needed at module level, not only as a Config attribute: the secret
# key is derived from a file inside it, and class bodies do not create closures
# for the methods defined inside them.
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))


def _secret_key() -> str:
    """Resolve the Flask session secret.

    A per-boot random key silently logs everyone out on every restart, so an
    unset SECRET_KEY is persisted next to the database instead. SYSTEM_LOGIN
    depends on this: rotating it would invalidate every login session.
    """
    env = os.getenv("SECRET_KEY", "").strip()
    if env:
        return env
    if _bool("DEV_MODE", False):
        return "dev-change-me-" + os.urandom(8).hex()
    key_file = DATA_DIR / "session.key"
    try:
        if key_file.exists():
            existing = key_file.read_text(encoding="utf-8").strip()
            if existing:
                return existing
        key_file.parent.mkdir(parents=True, exist_ok=True)
        generated = secrets.token_urlsafe(48)
        key_file.write_text(generated, encoding="utf-8")
        key_file.chmod(0o600)
        return generated
    except OSError:
        # Cannot persist: fall back to a process-lifetime key rather than a
        # hard-coded one. Sessions reset on restart, but nothing is forgeable.
        return "ephemeral-" + os.urandom(16).hex()


class Config:
    # --- Server -------------------------------------------------------------
    HOST = os.getenv("HOST", "0.0.0.0")
    PORT = _int("PORT", 5001)
    # A per-boot random key silently logs everyone out on every restart, so an
    # unset SECRET_KEY is persisted next to the database instead (see
    # _secret_key). SYSTEM_LOGIN depends on this: rotating it would log
    # everybody out.
    SECRET_KEY = _secret_key()
    # Only set when APEX is actually served over HTTPS; a Secure cookie sent over
    # plain HTTP is silently dropped, which would log the user out every reload.
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "").strip().lower() in {"1", "true", "yes", "on"}
    SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "apex_session")
    # Off, unlike Flask's default. Refreshing the cookie on every response means
    # a response another tab had in flight re-sends the whole session, so a
    # sign-out in one tab is undone and takes a second attempt to stick.
    SESSION_REFRESH_EACH_REQUEST = _bool("SESSION_REFRESH_EACH_REQUEST", False)
    SESSION_LIFETIME_HOURS = _int("SESSION_LIFETIME_HOURS", 720)
    # Only trust X-Forwarded-For when APEX really sits behind a reverse proxy.
    # Trusting it unconditionally would let any client spoof the address the
    # login throttle keys on.
    TRUST_PROXY = _bool("TRUST_PROXY", False)
    # Additional CORS origins, comma separated. Empty in a normal single-user
    # install, because the allowlist should stay exactly as small as it needs
    # to be.
    EXTRA_ALLOWED_ORIGINS = os.getenv("EXTRA_ALLOWED_ORIGINS", "")
    # --- System-user login (PAM) --------------------------------------------
    # APEX has no user table: who may use it is decided by /etc/passwd and
    # /etc/shadow, so desktop and web share one account lifecycle and one
    # password policy. The password is only ever handled by libpam.
    SYSTEM_LOGIN_ENABLED = os.getenv("SYSTEM_LOGIN_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    # PAM service name. "login" matches what the console and sshd use, so
    # password policy, lockout and expiry behave identically.
    SYSTEM_LOGIN_PAM_SERVICE = os.getenv("SYSTEM_LOGIN_PAM_SERVICE", "login")
    # Throttle per user+address. N attempts per window, then locked out.
    SYSTEM_LOGIN_MAX_ATTEMPTS = _int("SYSTEM_LOGIN_MAX_ATTEMPTS", 5)
    SYSTEM_LOGIN_WINDOW_SECONDS = _int("SYSTEM_LOGIN_WINDOW_SECONDS", 300)
    SYSTEM_LOGIN_LOCKOUT_SECONDS = _int("SYSTEM_LOGIN_LOCKOUT_SECONDS", 300)
    SYSTEM_LOGIN_AUDIT_LOG = os.getenv("SYSTEM_LOGIN_AUDIT_LOG", str(DATA_DIR / "auth-audit.log"))
    # Optional: comma-separated group names. Empty means "any login-capable user".
    # The login screen lists the machine's human accounts. That is a desktop
    # convention (SDDM/GDM do it), but it does disclose account names to anyone
    # who can reach the page, so it can be turned off to show a plain field.
    SYSTEM_LOGIN_SHOW_USERS = _bool("SYSTEM_LOGIN_SHOW_USERS", True)
    SYSTEM_LOGIN_GROUPS = tuple(
        g.strip() for g in os.getenv("SYSTEM_LOGIN_GROUPS", "").split(",") if g.strip()
    )
    # While system login is on, OpenAI OAuth is a way *around* the machine's
    # account list: anyone with an OpenAI account could sign in and get a session
    # no local user has. It stays off unless it is deliberately turned on, in
    # which case the login screen offers it as a second button.
    SYSTEM_LOGIN_ALLOW_OAUTH = _bool("SYSTEM_LOGIN_ALLOW_OAUTH", False)
    # Re-authentication to leave the lock screen, and idle auto-lock.
    LOCK_SCREEN_ENABLED = os.getenv("LOCK_SCREEN_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    LOCK_IDLE_SECONDS = _int("LOCK_IDLE_SECONDS", 0)  # 0 = only on explicit command
    # --- CSRF ---------------------------------------------------------------
    CSRF_ENABLED = os.getenv("CSRF_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    # Public backend address used for ALL hosted file/media links and OAuth callbacks.
    BASE_URL = os.getenv("BASE_URL", f"http://localhost:{PORT}").strip().rstrip("/")
    FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")
    DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
    DB_PATH = Path(os.getenv("DB_PATH", DATA_DIR / "apex.db"))
    CHROMA_DIR = str(DATA_DIR / "chroma")
    # The dotenv file load_dotenv() reads at import time (top of this module).
    # Named here so tools that write env entries (set_env) and the tests that
    # redirect them have one agreed target instead of re-deriving the path.
    ENV_FILE = str(BASE_DIR / ".env")
    # Colon-separated search roots; OS permissions are always respected.
    FILE_SEARCH_ROOTS = os.getenv("FILE_SEARCH_ROOTS", "/")

    # --- File manager (Explorer window) -------------------------------------
    # Reuses FILE_SEARCH_ROOTS above. Generated zips/single-file links expire
    # after FM_FILE_TTL_SECONDS; finished copy/move jobs idle for
    # FM_JOB_TTL_SECONDS before being dropped; uploads cap at this many files.
    FM_FILE_TTL_SECONDS = _int("FM_FILE_TTL_SECONDS", 3600)
    FM_JOB_TTL_SECONDS = _int("FM_JOB_TTL_SECONDS", 3600)
    FM_UPLOAD_MAX_FILES = _int("FM_UPLOAD_MAX_FILES", 1000)

    # --- Notepad -------------------------------------------------------------
    # Blank saves to ~/Documents/APEX Notepad; each signed-in user is isolated.
    NOTEPAD_DOCUMENTS_DIR = os.getenv("NOTEPAD_DOCUMENTS_DIR", "")

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
    # Model APEX switches to for a single turn when the user asks it to think
    # hard ("think hard: ...", or the Think hard switch in Settings). Only
    # consulted when THINK_HARD_MODEL_ENABLED is true and a model is set; the
    # turn is answered once by this model, with no second pass.
    THINK_HARD_MODEL = os.getenv("THINK_HARD_MODEL", "").strip()
    THINK_HARD_MODEL_ENABLED = _bool("THINK_HARD_MODEL_ENABLED", False)
    # Local-command reasoning fallback. The browser's command parsers are regexes
    # and only match the phrases they were written for; when one misses, the
    # utterance used to reach the agent, which has no tool that can un-minimize a
    # window, so "restore all terminals" produced a reply and no change. This
    # asks a model to read the missed utterance against the list of actions the
    # browser supports and the state of the screen. OFF means a miss goes to the
    # agent, which is exactly the old behaviour.
    COMMAND_ROUTER_ENABLED = _bool("COMMAND_ROUTER_ENABLED", True)
    # Optional small model for the above. Empty uses the provider's default model.
    COMMAND_ROUTER_MODEL = os.getenv("COMMAND_ROUTER_MODEL", "").strip()
    # Tools that need special permission (only enabled via env).
    ENABLE_RUN_PYTHON = _bool("ENABLE_RUN_PYTHON", False)
    ENABLE_RUN_SHELL = _bool("ENABLE_RUN_SHELL", False)
    RUN_SHELL_TIMEOUT = _int("RUN_SHELL_TIMEOUT", 60)
    MAX_TOOL_STEPS = _int("MAX_TOOL_STEPS", 12)

    # --- Scheduled tasks ----------------------------------------------------
    # Cron-backed jobs the agent creates and runs on its own (see
    # tools/tasks.py). TASKS_ENABLED is the master switch: when it is false the
    # task tools are never registered, the /api/tasks routes answer 403 and the
    # runner never starts, so a deployment can have none of it.
    TASKS_ENABLED = _bool("TASKS_ENABLED", True)
    # How often the runner thread asks the database which tasks are due. Short
    # enough that "in 2 minutes" fires when the operator expects, long enough
    # that an idle backend is not asking SQLite questions in a tight loop.
    TASKS_TICK_SECONDS = _int("TASKS_TICK_SECONDS", 20)
    # How many task turns may run at the same time process-wide. One by default:
    # each run is a full agent turn with tools, and a laptop waking from sleep
    # with a week of backlog must not fire all of them at once.
    TASKS_MAX_CONCURRENT = _int("TASKS_MAX_CONCURRENT", 1)
    # Hard ceiling on one task turn. A provider that never answers would
    # otherwise hold a concurrency slot for ever and the task would sit in
    # `running` with nobody left to notice.
    TASKS_TIMEOUT_SECONDS = _int("TASKS_TIMEOUT_SECONDS", 600)
    # Turns of the task's own conversation replayed as context, so a run can see
    # what the previous ones found.
    TASKS_HISTORY_WINDOW = _int("TASKS_HISTORY_WINDOW", 20)
    TASKS_MAX_PER_USER = _int("TASKS_MAX_PER_USER", 50)
    # Give a task one run on startup if its moment passed while the backend was
    # down, instead of silently skipping it. Only ever once per gap.
    TASKS_CATCH_UP = _bool("TASKS_CATCH_UP", True)

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

    # --- SOUL.md ----------------------------------------------------------
    # Operator-authored persona (settings tab). Appended to every system prompt.
    # Kept bounded because it is sent with every single request.
    SOUL_MAX_CHARS = _int("SOUL_MAX_CHARS", 8000)

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
    # Summarize a conversation into long-term memory when the user leaves it
    # (new thread / switching conversations), so Apex can recall it when asked.
    MEMORY_CONVERSATION_SUMMARIZE = _bool("MEMORY_CONVERSATION_SUMMARIZE", True)
    # Minimum user messages before a first-time thread summary is worth creating.
    MEMORY_CONVERSATION_MIN_MESSAGES = _int("MEMORY_CONVERSATION_MIN_MESSAGES", 4)
    # Messages per summarizer window; larger threads are summarized in rolling chunks.
    MEMORY_CONVERSATION_SUMMARIZE_WINDOW = _int("MEMORY_CONVERSATION_SUMMARIZE_WINDOW", 60)

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

    # --- VISIO skill (camera + vision model) ---------------------------------
    # Off by default: this is the only feature in the repo that opens a
    # physical device pointed at the room the operator is sitting in. It must be
    # something a deployment deliberately turns on.
    VISIO_ENABLED = _bool("VISIO_ENABLED", False)
    # Explicit image-capable model; the main chat model is independent.
    VISIO_MODEL = os.getenv("VISIO_MODEL", "").strip()
    VISIO_PROVIDER = os.getenv("VISIO_PROVIDER", "ollama").strip()
    VISIO_CAMERA = os.getenv("VISIO_CAMERA", "").strip()
    # The earlier draft options below are retained for compatibility only.
    # The snapshot tool does not consume them: it uses VISIO_CAMERA, keeps
    # frames in memory, and does not implement face recognition.
    # /dev/video* to try, in order. A numeric or glob-ish value is fine; the
    # probe also reports whatever else it finds, so an empty value still works
    # and just scans.
    VISIO_DEVICE = os.getenv("VISIO_DEVICE", "").strip()
    VISIO_WIDTH = _int("VISIO_WIDTH", 1280)
    VISIO_HEIGHT = _int("VISIO_HEIGHT", 960)
    # How long a captured frame stays on disk. The camera light is only ever on
    # for a turn that asked for a photo, but the file survives that, so it is
    # deleted on this TTL unless the operator keeps it.
    VISIO_SNAPSHOT_TTL_SECONDS = _int("VISIO_SNAPSHOT_TTL_SECONDS", 3600)
    # Max frames kept per user, oldest swept first, so a chatty session cannot
    # fill the disk between TTL runs.
    VISIO_MAX_SNAPSHOTS = _int("VISIO_MAX_SNAPSHOTS", 50)
    # Face recognition is separate from seeing: it needs local model files and
    # it stores biometric data, so it is off even when VISIO_ENABLED is on.
    VISIO_FACES_ENABLED = _bool("VISIO_FACES_ENABLED", False)
    # Cosine distance at or below which a face is reported as a confident match.
    # ArcFace on a clean frontal crop sits near 0.3-0.5 for the same person and
    # 1.0+ for a stranger, so the default is deliberately conservative.
    VISIO_FACE_THRESHOLD = float(os.getenv("VISIO_FACE_THRESHOLD", "0.45"))
    # Above the confident threshold but still close, the agent says "possibly"
    # instead of naming someone. A stranger must never be announced as a known
    # person just because the embedding drifted.
    VISIO_FACE_POSSIBLE_THRESHOLD = float(os.getenv("VISIO_FACE_POSSIBLE_THRESHOLD", "0.65"))

    # --- SIP skill (outbound phone calls) ------------------------------------
    # Off by default: this dials a real phone on a real account. A deployment
    # turns it on in the settings tab, which is also what writes the SIP_*
    # values into backend/.env (SIP_ENV_FILE).
    SIP_ENABLED = _bool("SIP_ENABLED", False)
    SIP_SERVER = os.getenv("SIP_SERVER", "").strip()
    SIP_USER = os.getenv("SIP_USER", "").strip()
    SIP_PASSWORD = os.getenv("SIP_PASSWORD", "")
    # udp | tcp | tls. Anything else is rejected on save, not silently fixed.
    SIP_TRANSPORT = os.getenv("SIP_TRANSPORT", "udp").strip().lower()
    # Empty means the server's default port.
    SIP_PORT = os.getenv("SIP_PORT", "").strip()
    SIP_DISPLAY_NAME = os.getenv("SIP_DISPLAY_NAME", "APEX").strip() or "APEX"
    # Empty means SIP_DOMAIN falls back to SIP_SERVER.
    SIP_DOMAIN = os.getenv("SIP_DOMAIN", "").strip()
    # Optional destination for scheduled "call me" notifications.
    SIP_NOTIFY_TO = os.getenv("SIP_NOTIFY_TO", "").strip()
    # Empty means no outbound proxy.
    SIP_OUTBOUND_PROXY = os.getenv("SIP_OUTBOUND_PROXY", "").strip()
    # Hard ceiling on one call, seconds. The tool can ask for less.
    SIP_MAX_DURATION_SECONDS = _int("SIP_MAX_DURATION_SECONDS", 600)
    # How long a `listen` turn waits for the person to start speaking.
    SIP_LISTEN_TIMEOUT_SECONDS = _int("SIP_LISTEN_TIMEOUT_SECONDS", 20)
    # SIP speech defaults to local eSpeak. Edge TTS is an opt-in online engine.
    SIP_TTS_ENGINE = os.getenv("SIP_TTS_ENGINE", "espeak").strip().lower()
    SIP_TTS_VOICE = os.getenv("SIP_TTS_VOICE", "").strip()
    # faster-whisper lives in its own virtualenv so it does not bloat the
    # backend's dependencies. Created by: pip install faster-whisper
    SIP_WHISPER_PYTHON = os.getenv(
        "SIP_WHISPER_PYTHON", str(Path(BASE_DIR).parent / ".venv" / "sip-whisper" / "bin" / "python")
    )
    SIP_WHISPER_MODEL = os.getenv("SIP_WHISPER_MODEL", "tiny").strip() or "tiny"
    # Where the settings tab writes SIP_* so a shell-started process and a
    # systemd-started one read the same values. Default is the env file
    # python-dotenv already loads for this module.
    SIP_ENV_FILE = os.getenv("SIP_ENV_FILE", str(BASE_DIR / ".env"))

    # --- On-screen shell output ---------------------------------------------
    # run_shell on_screen=true writes the captured output here for viewing and
    # download in a desktop window; files are cleaned up after this many seconds.
    SHELL_OUT_TTL_SECONDS = _int("SHELL_OUT_TTL_SECONDS", 3600)
    # Host-wide PTY terminals ("open terminal" window). A terminal is a shell on
    # the host, so it is only available when run_shell itself is enabled.
    TERMINAL_IDLE_SECONDS = _int("TERMINAL_IDLE_SECONDS", 300)

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
    #
    # Neither may switch itself on while system login is enabled. This is a
    # dev convenience and system login is a security control: if both are set,
    # the auto-login wins and silently re-authenticates every empty session as
    # a fixed user, which means the sign-in screen is bypassed, "sign out"
    # cannot log anyone out (the next request signs them straight back in), and
    # the lock screen asks PAM about the dev user rather than the human at the
    # keyboard. The opt-out exists only for a deliberate local test run.
    DEV_MODE = _bool("DEV_MODE", False)
    DEV_AUTO_LOGIN = os.getenv("DEV_AUTO_LOGIN", "").strip()
    DEV_AUTO_LOGIN_WITH_SYSTEM_LOGIN = _bool("DEV_AUTO_LOGIN_WITH_SYSTEM_LOGIN", False)

    @property
    def dev_auto_login(self) -> bool:
        wants = bool(self.DEV_AUTO_LOGIN) or (
            self.DEV_MODE and not self.oauth_configured and not self.OPENAI_API_KEY
        )
        if wants and self.SYSTEM_LOGIN_ENABLED and not self.DEV_AUTO_LOGIN_WITH_SYSTEM_LOGIN:
            return False
        return wants

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