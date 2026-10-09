"""Skills: reusable capability packs (system-prompt + tool subset).

A skill is a markdown file in ``skills/definitions/`` with YAML frontmatter:

    ---
    name: research
    description: Web research with live sources and citations.
    tools: web_search, web_fetch, calculate, remember, recall
    model: gpt-4o-mini            # optional model override
    ---
    <freeform system-prompt for this skill>

Users can drop new .md files here - pickup is on next request. The frontend
surfaces ``/api/skills`` so any skill (built-in or user-made) is one click away.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from config import config


@dataclass
class Skill:
    name: str
    description: str
    system_prompt: str
    tools: list[str] = field(default_factory=list)   # names; empty = all active
    model: str = ""
    # When true the engine passes tool_choice="required" on the FIRST model call
    # of a turn, so a skill that promises to actually run things cannot instead
    # answer from memory. Only meaningful for skills whose whole job is acting.
    require_tool: bool = False
    # Tool names this skill must NEVER be offered, even if tools: ALL. Used to
    # take the headless run_shell away from skills whose promise is that the
    # user watches every command in a terminal window.
    exclude_tools: list[str] = field(default_factory=list)
    builtin: bool = True
    source: str = ""


MISSING_SKILL_TEMPLATE = (
    "You are {name}, a focused specialist persona. {name} {description}. "
)


def _tool_list(value) -> list[str]:
    """Frontmatter tool lists accept a comma-separated string or a YAML list."""
    if not value:
        return []
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    if isinstance(value, (list, tuple)):
        return [str(t).strip() for t in value if str(t).strip()]
    return []


def _expand_config_vars(value: str) -> str:
    """Replace $VAR / ${VAR} in a skill frontmatter value with config attributes."""
    if not value or "$" not in value:
        return value

    def repl(match: re.Match) -> str:
        var = match.group(1) or match.group(2)
        return str(getattr(config, var, ""))

    return re.sub(r"\$\{(\w+)\}|\$(\w+)", repl, value)


class SkillManager:
    def __init__(self, definitions_dir: Path | None = None):
        self._dir = Path(definitions_dir) if definitions_dir else Path(config.DATA_DIR) / "skills"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._builtin_dir = Path(__file__).parent / "definitions"
        self._cache: dict[str, Skill] | None = None

    # ---- discovery ----------------------------------------------------------
    def _load_dir(self, directory: Path) -> list[Skill]:
        skills = []
        for path in sorted(directory.glob("*.md")):
            skill = self._parse(path)
            if skill:
                skills.append(skill)
        return skills

    def _parse(self, path: Path) -> Skill | None:
        raw = path.read_text(encoding="utf-8")
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", raw, re.S)
        if not m:
            return None
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except Exception:
            return None
        body = m.group(2).strip()
        name = (meta.get("name") or path.stem).strip()
        tools = meta.get("tools") or []
        if isinstance(tools, str):
            tools = [t.strip() for t in tools.split(",") if t.strip()]
        return Skill(
            name=name,
            description=str(meta.get("description") or "").strip(),
            system_prompt=body,
            tools=[t for t in tools if t != "ALL"] if tools else [],
            model=_expand_config_vars(str(meta.get("model") or "")),
            require_tool=str(meta.get("require_tool") or "").strip().lower()
            in {"1", "true", "yes", "on"},
            exclude_tools=_tool_list(meta.get("exclude_tools")),
            builtin=path.parent == self._builtin_dir,
            source=str(path),
        )

    def refresh(self):
        self._cache = None

    def _all(self) -> list[Skill]:
        if self._cache is None:
            skills = self._load_dir(self._builtin_dir) + self._load_dir(self._dir)
            # user-defined shadows built-in of the same name
            merged: dict[str, Skill] = {}
            for s in skills:
                merged[s.name] = s
            self._cache = merged
        return list(self._cache.values())

    def all(self) -> list[Skill]:
        return sorted(self._all(), key=lambda s: (not s.builtin, s.name))

    def get(self, name: str) -> Skill | None:
        skills = {s.name: s for s in self._all()}
        return skills.get(name)

    def delete(self, name: str) -> bool:
        """Delete a user-defined skill file. Built-in skills cannot be removed."""
        skill = self.get(name)
        if skill is None or skill.builtin:
            return False
        path = Path(skill.source)
        if path.exists() and path.is_file():
            path.unlink()
            self.refresh()
            return True
        return False

    def select(self, name: str) -> Skill:
        skill = self.get(name)
        if skill:
            return skill
        return Skill(
            name=name or "general",
            description="General purpose assistant skill.",
            system_prompt=MISSING_SKILL_TEMPLATE.format(name=name or "APEX"),
            tools=[],
            builtin=False,
        )

    # ---- prompt construction ------------------------------------------------
    def build_system_prompt(
        self,
        skill_name: str,
        *,
        memory_block: str = "",
        voice_mode: bool = False,
        extra: str = "",
        user_name: str = "",
        response_language: str = "en",
    ) -> str:
        skill = self.select(skill_name)
        base = skill.system_prompt.strip()
        language = {"en": "English", "el": "Greek"}.get(response_language, "English")
        parts = [
            "You are APEX, an autonomous multimodal AI assistant.",
            f"You are helpful, concise and precise. Always respond in {language} until the user changes the Default response language setting in the UI. Do not switch response languages based on the user's input language. If asked for a translation, provide the requested translated content but keep your explanation and surrounding response in {language}.",
            f"## Skill: {skill.name}\n{base}",
            # General, and after the skill body so it outranks any skill prompt:
            # the model must never turn a credential file into chat text.
            "Never reveal secrets and never expose environment files. Do not "
            "read, open, print, `cat`, quote, summarise or return the contents "
            "of any `.env` file (the backend's or a skill pack's), and never "
            "paste a password, key or token into chat. If a credential is "
            "missing or wrong, name the variable and the file path only, and "
            "tell the operator to edit that file themselves.",
        ]
        if user_name:
            parts.append(f"You are assisting {user_name}.")
        if memory_block:
            parts.append(memory_block)
        if skill.tools:
            parts.append(f"Available tools for this skill: {', '.join(skill.tools)}.")
        if extra:
            parts.append(extra)
        if not voice_mode:
            # The chat panel renders this syntax (KaTeX + a small SVG geometry
            # language). A model that does not know the delimiters will answer
            # with plain prose or an ASCII sketch; telling it once here covers
            # every skill. Skipped in voice mode, where the reply is spoken and
            # a formula or diagram would be read out as raw TeX.
            parts.append(
                "The chat panel renders LaTeX math and simple SVG geometry on "
                "its own. Write inline math as $...$ and display math as "
                "$$...$$. For a diagram, emit a fenced ```geometry block with "
                "one directive per line in a 0..100 coordinate box: "
                "line x1 y1 x2 y2; rect x y w h; circle cx cy r; "
                "polygon/polyline x1 y1 x2 y2 ...; point x y [label]; "
                "text x y label; angle vx vy ax ay bx by [r]. Use geometry only "
                "when a picture explains it better than words."
            )
        if voice_mode:
            # Last on purpose: this is the final word, so it outranks the skill
            # body (the shell skill says "return output verbatim").
            parts.append(
                "You are chatting by voice and your reply is spoken aloud by "
                "text-to-speech, word for word. So the reply itself must be the "
                "spoken answer: 1-3 short, natural sentences of plain prose that "
                "say what the result means. Never put raw command or tool output "
                "in the reply - no tables, no code blocks, no `Filesystem ...` "
                "lines, no per-line listings, no logs, no file paths, no exit "
                "codes. Do not open with 'here is the output' and do not end "
                "with filler like 'let me know if you need anything else'. If a "
                "command returned a lot, say the one or two numbers that matter. "
                "This overrides any instruction to return output verbatim, for "
                "every command and every tool."
            )
        return "\n\n".join(p for p in parts if p)


_manager: SkillManager | None = None


def get_skill_manager() -> SkillManager:
    global _manager
    if _manager is None:
        _manager = SkillManager()
    return _manager


# --------------------------------------------------------------------------- #
# Skill router
# --------------------------------------------------------------------------- #
_ROUTER_CACHE: dict[str, str] = {}
_ROUTER_CACHE_SIZE = 64


def _normalize_skill_name(name: str) -> str:
    return name.strip().strip("[]\"'").lower()


# Strong keyword safety net: only used to correct obvious model mistakes.
_ROUTER_KEYWORDS: dict[str, list[str]] = {
    "code": [
        "python", "javascript", "typescript", "java", "c++", "cpp", "csharp",
        "go", "rust", "php", "ruby", "swift", "kotlin", "code", "function",
        "script", "programming", "program", "debug", "bug", "api", "sql",
        "html", "css", "react", "node", "django", "flask", "algorithm",
        "dataframe", "numpy", "pandas", "matplotlib", "regex", "json parsing",
    ],
    "obsidian": [
        "obsidian", "vault", "daily note", "daily notes", "wiki-link",
        "wikilink", "backlink", "markdown note", "my notes", "note titled",
    ],
    "translator": ["translate", "translation", "in greek", "in english", "in spanish", "in french"],
    "FILE_SEARCH": ["find file", "find files", "search file", "search files", "locate file"],
    "EDITOR": ["word document", "excel file", "create a report", "generate a report", "docx", "xlsx"],
    "shell": ["ping", "nmap", "ss -", "ip addr", "journalctl", "systemctl", "run shell"],
    "VAPT": ["vapt", "pentest", "penetration test", "pen test", "security assessment", "owasp", "scan target", "vulnerability assessment"],
}


def _keyword_override(user_text: str, model_choice: str) -> str | None:
    """Return a skill name if the message contains strong keywords that clearly
    override a likely model mistake, otherwise None."""
    lower = user_text.lower()

    # Code requests must never be mis-routed to note-taking or translation.
    if model_choice not in ("code",):
        code_kw = [k for k in _ROUTER_KEYWORDS["code"] if k in lower]
        # Require at least one code keyword and no strong Obsidian context.
        if code_kw and not any(k in lower for k in _ROUTER_KEYWORDS["obsidian"]):
            return "code"

    # Obsidian requests must never be mis-routed to code or research.
    if model_choice != "obsidian":
        if any(k in lower for k in _ROUTER_KEYWORDS["obsidian"]):
            return "obsidian"

    # Translator requests are usually unambiguous.
    if model_choice != "translator":
        if any(k in lower for k in _ROUTER_KEYWORDS["translator"]):
            return "translator"

    return None


# --- deterministic host-state routing -------------------------------------
# The LLM router is a soft suggestion and it does get it wrong: "check our
# network connection" was routed to `general`, which has no reason to touch a
# tool, so the model answered from memory with a fabricated ping. Anything that
# is a question about the LIVE state of this host must land on the shell skill,
# which forces a real command in a visible terminal.
_HOST_STATE_PATTERNS = (
    # connectivity / internet
    r"\binternet\b", r"\bonline\b", r"\boffline\b", r"\bconnectivity\b",
    r"\bwi-?fi\b", r"\bbandwidth\b", r"\bping\b", r"\bdns\b",
    r"\bip address\b", r"\brouter\b", r"\bgateway\b", r"\bvpn\b",
    r"\bethernet\b", r"\bnetwork card\b", r"\bnetwork (status|interface|adapter|diagnos\w*|problem\w*|issue\w*|config\w*|connection)\b",
    r"\b(listening|open|exposed|used) ports?\b",
    r"\bports?\b\s+(are|is)?\s*(being\s+)?(listen\w*|open)",
    r"\bfirewall\b", r"\blatency\b", r"\bconnection speed\b",
    r"\bspeed ?test\b", r"\bdown(up)?load\b",
    r"\b(host|server|laptop|machine|pc) (status|health|uptime)\b",
    # storage / cpu / memory / processes
    # "disk"/"storage" are host nouns in this app, and the codeish guard keeps
    # them out of programming turns. Matching the noun rather than the word
    # "usage" is also what survives "disk usagge".
    r"\bdisks?\b", r"\bstorage\b",
    r"\bdisk (usage|space|health|free|full)\b", r"\bdf -h\b", r"\blsblk\b",
    r"\bfilesystem\b", r"\bhow much (memory|ram|disk|space)\b",
    r"\b(memory|ram) (usage|free|left|used|consumption|available)\b",
    r"\b(cpu) (usage|load|cores?|info\w*|consumption|temperature)\b",
    r"\bfree -h\b", r"\buptime\b",
    r"\bprocess(es)? (list|running|using|consum\w*|count)\b",
    r"\bhow many process\b", r"\b(ps aux|top|htop)\b",
    # services / logs / system
    r"\bsystemd\b", r"\bsystemctl\b", r"\bservice(s)? (status|running|failed|failing)\b",
    r"\bjournalctl\b", r"\bsystem (log|status|info|health)\w*\b",
    r"\bkernel\b", r"\buname\b", r"\bhostname\b", r"\blsmod\b",
    r"\bdmesg\b", r"\bbattery\b", r"\btemperature\b",
    r"\bopen (a )?(new )?terminal\b", r"\b(terminal|shell) (command|prompt)\b",
    # an address or a network tool on the command line is host work
    r"\b\d{1,3}(?:\.\d{1,3}){3}\b", r"\bnmap\b", r"\bport scan\b",
    r"\btraceroute\b", r"\btracepath\b", r"\bnslookup\b", r"\bdig\b",
    r"\bifconfig\b", r"\bnetstat\b", r"\biptables\b", r"\bping\b",
)
_HOST_STATE_RE = re.compile("|".join(_HOST_STATE_PATTERNS), re.IGNORECASE)

# ...unless the message is really about writing code, where a terminal run is
# optional and hijacking the turn would be worse than the bug.
_CODEISH_RE = re.compile(
    r"(?i)\b(def |class |function|method|lambda|python|javascript|typescript|"
    r"refactor|unit test|pytest|regex|sql|query|database schema|api endpoint|"
    r"dockerfile|makefile|git commit|npm |pip install|stack trace)\b"
)

HOST_STATE_SKILL = "shell"

# People type "conenction" and "interne". Exact matching missed the real
# message that broke this ("check our network conenction for interne"), so long
# keywords are also matched with a small Damerau-Levenshtein distance: one
# transposition or two edits still counts. Only keywords of 6+ letters are fuzzy
# matched, otherwise short words collide with ordinary English.
_FUZZY_KEYWORDS = (
    "internet", "connection", "network", "networks", "terminal", "terminals",
    "bandwidth", "wireless", "ethernet", "interface", "interfaces",
    "filesystem", "processor", "temperature", "hostname", "diagnostics",
    "listening", "throughput", "localhost",
)
_FUZZY_MIN_LEN = 6
_FUZZY_MAX_DIST = 2


def _damerau(a: str, b: str, limit: int) -> int:
    """Damerau-Levenshtein distance, abandoned early once it exceeds limit."""
    la, lb = len(a), len(b)
    if abs(la - lb) > limit:
        return limit + 1
    prev2: list[int] = []
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        best = cur[0]
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            val = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if (i > 1 and j > 1 and a[i - 1] == b[j - 2]
                    and a[i - 2] == b[j - 1]):
                val = min(val, prev2[j - 2] + 1)
            cur[j] = val
            best = min(best, val)
        if best > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[lb]


def _fuzzy_host_hit(text: str) -> str | None:
    normalized = _strip_accents(text).lower()
    for token in re.findall(r"[a-z]{%d,}" % _FUZZY_MIN_LEN, normalized):
        for kw in _FUZZY_KEYWORDS:
            if _damerau(token, kw, _FUZZY_MAX_DIST) <= _FUZZY_MAX_DIST:
                return token
    return None


def _strip_accents(text: str) -> str:
    import unicodedata

    return "".join(
        c for c in unicodedata.normalize("NFD", text or "")
        if unicodedata.category(c) != "Mn"
    )



#: Matched against accent-stripped, lowercased text, so every alternative is
#: written unaccented. A pattern holding an accented character can never match
#: the stripped input, which is how a Greek memory request quietly stops being
#: recognised.
_MEMORY_STORE_RE = re.compile(
    # English. The object is what makes this a fact being stored rather than an
    # ordinary sentence that happens to start with a verb: "remember this",
    # "note that my number", "save my address", "remember I like cakes".
    r"\b(?:remember|memori[sz]e|note|save|store|keep)\b"
    r"(?:\s+\w+){0,3}\s+\b(?:this|that|these|those|it|my|our|=|:)\b"
    r"|\b(?:remember|memori[sz]e)\s+(?:i|you|we|they|he|she)\s+(?:like|love|hate|"
    r"prefer|want|need|am|is|are|was|have|has|drive|live|work|use|own|call|number|"
    r"name|birthday|email)\b"
    r"|\b(?:remember|memori[sz]e|note|save|store|keep)\s+(?:my|our)\b"
    # Greek, unaccented, with suffixes: "θυμήσου", "θυμάσαι", "θυμησε" and the
    # imperative endings all share the stem, so the stem carries the match.
    r"|\bθυμ[αηεο]\w*\b|\bθυμ\b"
    r"|\bσημειωσ[εε]*\b|\bκρατα\b",
    re.IGNORECASE,
)

#: "save the file to disk" is a shell task, not something being remembered.
#: Checked against the same span the verb matched, so it only disqualifies the
#: store reading when the object is really a file.
_MEMORY_STORE_OBJECT_RE = re.compile(
    r"\b(?:file|files|disk|folder|directory|document|script|repo|repository|"
    r"code|image|picture|photo|table|spreadsheet|workbook|presentation|"
    r"αρχειο|αρχεια|φακελο|φακελος)\b",
    re.IGNORECASE,
)


def is_memory_store(text: str) -> bool:
    """Whether the message asks for a fact to be remembered."""
    if _CODEISH_RE.search(text or ""):
        return False
    flat = _strip_accents(text or "")
    match = _MEMORY_STORE_RE.search(flat)
    if not match:
        return False
    return not _MEMORY_STORE_OBJECT_RE.search(flat[match.start():])


def force_host_skill(user_text: str, skills: list[Skill]) -> str | None:
    """Return HOST_STATE_SKILL for live-host questions, else None.

    Deliberately keyword-based: this runs before the LLM router precisely
    because the LLM router cannot be trusted with this decision.
    """
    if not any(s.name == HOST_STATE_SKILL for s in skills):
        return None
    text = user_text or ""
    if not text.strip() or _CODEISH_RE.search(text):
        return None
    if _HOST_STATE_RE.search(text):
        return HOST_STATE_SKILL
    return HOST_STATE_SKILL if _fuzzy_host_hit(text) else None


def force_visio_skill(user_text: str, skills: list[Skill]) -> str | None:
    if not any(s.name == "VISIO" for s in skills) or _CODEISH_RE.search(user_text or ""):
        return None
    text = _strip_accents(user_text).lower()
    # "show me what you see" is the same request as "what do you see" with an
    # imperative in front, and it was not matched: the verb in front of "what
    # you see" is not the one that had been written down. It is anchored to the
    # start of the utterance, unlike the "what do you see" form which reads
    # fine mid-sentence, because "how do I write a program to show me what you
    # see" is a question *about* the feature and has to reach the agent.
    #
    # Greek needs the final sigma spelled both ways: `_strip_accents` removes
    # diacritics but does not fold ς to σ, so "τι βλεπεις" as typed reaches this
    # pattern as "τι βλεπεις" (final sigma) and never as the folded form. The
    # pre-existing "τι βλεπεις" alternative has been broken by that for as long
    # as it has been there; both spellings are now listed.
    if re.search(r"^(?:please\s+)?(?:show|see|display)\s+(?:me\s+)?(?:what|how)\s+(?:you|we)\s+(?:see|are\s+seeing)\b"
                 r"|\bwhat (?:do|can) you see(?: now)?\b|\b(?:look|see) through (?:the |my )?camera\b"
                 r"|\b(?:camera|webcam) (?:snapshot|attached|connected|available)\b"
                 r"|\b(?:take|capture) (?:(?:a|one|two|three|four|five|six|seven|eight|nine|ten|[0-9]+) )?(?:snapshots?|photos?)\b"
                 r"|τι βλεπει[σς]|τι βλεπετε|δειξ(?:ε|τε)(?: μου)? τι βλεπει[σς]"
                 r"|κοιτα (?:απο |με )?την καμερα", text):
        return "VISIO"
    return None


_SIP_CALL_RE = re.compile(
    # English: an explicit request to place a call, and "call me" specifically,
    # which is by far the most common phrasing.
    r"\b(?:please\s+)?(?:call|phone|ring|dial)\s+(?:me|us)\b"
    r"|\b(?:please\s+)?(?:call|phone|ring|dial)\s+(?:the\s+)?(?:operator|user|owner)\b"
    r"|\b(?:call|phone|ring|dial)\s+(?:up\s+)?(?:\+?[0-9][0-9\s().-]{5,})\b"
    r"|\b(?:make|place|start|send)\s+(?:me\s+)?(?:a\s+|the\s+)?(?:phone\s+)?call\b"
    r"|\btelephone\s+me\b"
    # Greek. Both sigma spellings are listed deliberately: the final sigma is a
    # separate character and matching only one makes half the phrasings fail
    # silently, which is the same trap the voice command normaliser documents.
    r"|\bτηλεφωνη[σς]ε\s+με\b"
    r"|\bκαλ(?:ε(?:σε)?)?[άσ]?\s+με\b"
    r"|\bμου\s+τηλεφων[άα]ς\b"
    # "καλέσε τον 210…" / "κάλε τον 210…". The article is accusative (τον)
    # here, not the neuter το, and the verb carries an optional -ε / -εσε.
    # Only the digits are required, which is what stops this matching "καλός".
    r"|\bκαλ(?:εσε)?(?:ε)?\s+(?:τον|το)?\s*(?:[0-9]|\+)",
    re.IGNORECASE,
)


def force_sip_skill(user_text: str, skills: list[Skill]) -> str | None:
    """Route an explicit request to place a call straight to the SIP skill.

    Deterministic, and required rather than merely nice: the general skill does
    not list ``sip_call`` in its tools, so a "call me" that fell through to the
    classifier would land on a skill that cannot place calls and the operator
    would be told it is impossible.

    Deliberately narrow. ``call`` alone is far too common a word — a function
    call, a phone call *about* something, "call the police" as advice — so only
    phrasings that clearly ask for a call to be placed route here.
    """
    if not any(s.name == "SIP" for s in skills):
        return None
    text = user_text or ""
    if not text.strip() or _CODEISH_RE.search(text):
        return None
    if _SIP_CALL_RE.search(_strip_accents(text)):
        return "SIP"
    return None


# An email capability is recognised from a skill's NAME or DESCRIPTION only -
# never its prompt body, which mentions mail tooling for other reasons (the VAPT
# skill lists `smtp-user-enum`). The email skill is user-made, so its name is
# discovered rather than hard-coded; a box without one routes as before.
_EMAIL_HINT_RE = re.compile(
    r"\b(?:e-?mail|mail|inbox|mailbox|pop3|imap"
    r"|ηλεκτρονικ\w*|ταχυδρομ\w*|μ[εέ]ιλ|ιμ[εέ]ιλ)", re.I)
_EMAIL_ADDR_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _email_skill_name(skills: list[Skill]) -> str | None:
    for s in skills:
        if _EMAIL_HINT_RE.search(f"{s.name} {s.description}"):
            return s.name
    return None


def force_email_skill(user_text: str, skills: list[Skill]) -> str | None:
    """Route a mail request to whichever loaded skill provides email.

    Deterministic because the email skill usually acts through ``run_shell``,
    and the host-state router would otherwise defeat the combined request: "run
    df -h and email me the result" matches the live-host patterns and is forced
    to the ``shell`` skill, which owns the terminal but cannot send mail. It
    runs ahead of the host guard so the mail intent wins. It only fires when an
    email-capable skill is actually loaded, so a box that has none is
    unaffected.
    """
    text = user_text or ""
    if not text.strip() or _CODEISH_RE.search(text):
        return None
    target = _email_skill_name(skills)
    if target is None:
        return None
    if _EMAIL_ADDR_RE.search(text) or _EMAIL_HINT_RE.search(text):
        return target
    return None


def route_skill(
    user_text: str,
    skills: list[Skill],
    provider,
    fallback: str = "general",
) -> str:
    """Pick the best specialist skill for a user message.

    Uses the configured model provider with a short classification prompt.
    The result is cached briefly to avoid repeated routing of identical
    messages. Returns ``fallback`` (default ``general``) on error or when no
    specialized skill matches.
    """
    if not skills:
        return fallback

    # Storing a fact is the general skill's job, and it has to be settled before
    # anything else: a memory request that mentions a phone number, an address
    # or a file is text the classifier reads as belonging to a specialist
    # (SIP, VISIO, FILE_SEARCH), and each of those skills lacks `remember`. The
    # operator gets a call plan, or a camera snapshot, instead of a memory.
    if is_memory_store(user_text):
        return fallback

    # Deterministic first: live-host questions always go to the skill that
    # actually runs commands, no matter what the classifier decides (or caches).
    # Email runs ahead of the host guard so a "run <cmd> and email me" request
    # reaches the skill that can actually send, not the terminal-only shell.
    forced = (force_visio_skill(user_text, skills)
              or force_sip_skill(user_text, skills)
              or force_email_skill(user_text, skills)
              or force_host_skill(user_text, skills))
    if forced:
        return forced

    cached = _ROUTER_CACHE.get(user_text)
    if cached is not None:
        return cached

    candidates = [s for s in skills if s.name != fallback]
    if not candidates:
        return fallback

    lines = [f"- {s.name}: {s.description}" for s in candidates]
    prompt = (
        "You are APEX's skill router. Choose exactly ONE skill from the list "
        "below that best matches the user's last message.\n\n"
        + "\n".join(lines)
        + "\n\nRouting rules:\n"
        "- If the request is about writing, fixing, explaining, debugging or "
        "running code, scripts, algorithms, APIs or databases, reply 'code'.\n"
        "- If the request is about web research, sources, citations or current "
        "events, reply 'research'.\n"
        "- If the request is about translating text, reply 'translator'.\n"
        "- If the request is about Obsidian notes, markdown files, wiki-links, "
        "tags or daily notes, reply 'obsidian'.\n"
        "- If the request is about shell commands, system administration or "
        "network operations, reply 'shell'.\n"
        "- If the request is about creating a new APEX skill, reply "
        "'skill_creator'.\n"
        "- If the request is about finding files on this computer, reply "
        "'FILE_SEARCH'.\n"
        "- If the request is about creating Word, Excel, report or document "
        "files, reply 'EDITOR'.\n"
        "- If the request is to place a phone call, telephone or ring someone, "
        "reply 'SIP'.\n"
        # Memory is not a specialist skill, so it has no name to route to and
        # falls through to the fallback. Without this the classifier reads
        # "remember this my phone number is 6977456030" as a request about a
        # phone number, routes it to SIP, and hands the operator a call plan
        # when they asked for a fact to be stored.
        "- If the request is to remember, note, save or store a fact about the "
        f"user, reply '{fallback}'. Storing something is never a phone call, "
        "even when the fact is a number, a name or an address.\n"
        f"- If none of the specialist skills clearly fit, reply '{fallback}'.\n\n"
        "Reply with ONLY the skill name, no explanation, no punctuation.\n\n"
        f"User message: {user_text}\nSkill:"
    )
    messages = [
        {"role": "system", "content": "You are a skill router."},
        {"role": "user", "content": prompt},
    ]

    try:
        text_parts: list[str] = []
        for chunk in provider.chat_stream(messages, tools=None):
            ctype = chunk.get("type")
            if ctype == "text":
                text_parts.append(chunk.get("content", ""))
            elif ctype == "error":
                return fallback
        reply = _normalize_skill_name("".join(text_parts))
        # Accept an exact match or a skill name appearing as a token.
        tokens = set(reply.split())
        model_choice = None
        for s in candidates:
            norm = _normalize_skill_name(s.name)
            if norm == reply or norm in tokens:
                model_choice = s.name
                break

        # Safety net for obvious mismatches reported by users.
        override = _keyword_override(user_text, model_choice or fallback)
        result = override if override is not None else (model_choice or fallback)

        _ROUTER_CACHE[user_text] = result
        if len(_ROUTER_CACHE) > _ROUTER_CACHE_SIZE:
            _ROUTER_CACHE.pop(next(iter(_ROUTER_CACHE)), None)
        return result
    except Exception:
        # Routing is best-effort; never block the chat on a router failure.
        pass
    return fallback