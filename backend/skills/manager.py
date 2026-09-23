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
    builtin: bool = True
    source: str = ""


MISSING_SKILL_TEMPLATE = (
    "You are {name}, a focused specialist persona. {name} {description}. "
)


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
        ]
        if user_name:
            parts.append(f"You are assisting {user_name}.")
        if memory_block:
            parts.append(memory_block)
        if voice_mode:
            parts.append(
                "You are chatting by voice. Keep answers short, natural and "
                "conversational (2-4 sentences unless asked for detail)."
            )
        if skill.tools:
            parts.append(f"Available tools for this skill: {', '.join(skill.tools)}.")
        if extra:
            parts.append(extra)
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