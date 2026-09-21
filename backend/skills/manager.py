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