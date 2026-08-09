from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from importlib.resources import files
from pathlib import Path
from typing import Any

from .cli_settings import app_home_dir
from .environment import find_project_root


SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
RUNTIME_FILES = (
    "skills/index.md",
    "skills/toe-dac-control/SKILL.md",
    "skills/agent-browser/SKILL.md",
    "skills/alex-serp/SKILL.md",
    "persona/active.json",
    "persona/blue/system.md",
    "persona/green/system.md",
)


@dataclass(frozen=True)
class LoadedSkill:
    name: str
    description: str
    body: str
    requires: tuple[str, ...]
    phases: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillIndexEntry:
    name: str
    description: str
    path: str
    requires: tuple[str, ...]
    phases: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimePromptSnapshot:
    persona_slot: str
    persona_revision: int
    persona: str
    skill_index: str = ""
    available_skills: tuple[SkillIndexEntry, ...] = ()
    skills: tuple[LoadedSkill, ...] = ()
    skills_root: str = ""

    @classmethod
    def empty(cls) -> "RuntimePromptSnapshot":
        return cls("none", 0, "")

    def activate(self, names: list[str] | tuple[str, ...]) -> "RuntimePromptSnapshot":
        """Read only the explicitly requested skills and return a new snapshot."""
        requested = list(dict.fromkeys(names))
        active = {skill.name: skill for skill in self.skills}
        catalog = {entry.name: entry for entry in self.available_skills}
        root = Path(self.skills_root).resolve() if self.skills_root else None
        for name in requested:
            if name in active:
                continue
            entry = catalog.get(name)
            if entry is None:
                raise ValueError(f"unknown or disabled skill: {name}")
            if root is None:
                raise ValueError("runtime snapshot has no skills root")
            path = (root / entry.path).resolve()
            if not path.is_relative_to(root):
                raise ValueError(f"runtime resource escapes its root: {entry.path}")
            frontmatter, body = _parse_skill(path.read_text(encoding="utf-8"))
            if frontmatter.get("name") != name:
                raise ValueError(f"skill index/frontmatter name mismatch: {name}")
            active[name] = LoadedSkill(name, entry.description, body, entry.requires, entry.phases)
        ordered = tuple(active[entry.name] for entry in self.available_skills if entry.name in active)
        return replace(self, skills=ordered)

    def render(self, phase_prompt: str, phase: str | None = None) -> str:
        sections: list[str] = []
        if self.persona:
            sections.append(
                f"## Global Persona\nslot={self.persona_slot} revision={self.persona_revision}\n\n"
                f"{self.persona.strip()}"
            )
        if self.skill_index:
            sections.append(
                "## Skill Index\n\n"
                "Skills are progressive. When an indexed skill is relevant, call `load_skill` "
                "before concluding that its capability is unavailable. After loading it, use any "
                "newly exposed skill tool as needed. Do not assume an unloaded skill's instructions "
                "or capabilities, and do not ask the human for data that an applicable indexed skill "
                "can obtain.\n\n"
                f"{self.skill_index.strip()}"
            )
        scoped_skills = tuple(
            skill for skill in self.skills
            if not phase or not skill.phases or phase in skill.phases
        )
        if scoped_skills:
            rendered_skills = []
            for skill in scoped_skills:
                requirements = ", ".join(skill.requires) if skill.requires else "none"
                rendered_skills.append(
                    f"### Skill: {skill.name}\n"
                    f"Required executable capabilities: {requirements}\n\n{skill.body.strip()}"
                )
            sections.append("## Skills Loaded On Demand\n\n" + "\n\n".join(rendered_skills))
        sections.append(f"## Current Phase Instructions\n\n{phase_prompt.strip()}")
        return "\n\n".join(sections)


def _runtime_resource_text(relative_path: str) -> str:
    packaged = files("toe_dac").joinpath("resources", "runtime", *relative_path.split("/"))
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8")
    source = find_project_root() / "runtime" / relative_path
    if source.is_file():
        return source.read_text(encoding="utf-8")
    raise FileNotFoundError(f"missing bundled runtime resource: {relative_path}")


def initialize_runtime_content(root: str | Path | None = None) -> list[Path]:
    directory = Path(root).expanduser() if root else app_home_dir()
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    created: list[Path] = []
    for relative_path in RUNTIME_FILES:
        destination = directory / relative_path
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(_runtime_resource_text(relative_path), encoding="utf-8")
        created.append(destination)
    return created


class RuntimeContentLoader:
    def __init__(self, root: str | Path | None = None):
        self.root = (Path(root).expanduser() if root else app_home_dir()).resolve()

    def load(self) -> RuntimePromptSnapshot:
        persona_slot, persona_revision, persona = self._load_persona()
        skill_index, entries = self._load_skill_index()
        return RuntimePromptSnapshot(
            persona_slot=persona_slot,
            persona_revision=persona_revision,
            persona=persona,
            skill_index=skill_index,
            available_skills=tuple(entries),
            skills_root=str((self.root / "skills").resolve()),
        )

    def _load_persona(self) -> tuple[str, int, str]:
        control_path = self.root / "persona" / "active.json"
        control = self._read_json(control_path)
        active = str(control.get("active", ""))
        if active not in {"blue", "green"}:
            raise ValueError(f"invalid active persona slot: {active!r}")
        prompt_path = self._safe_path(self.root / "persona", f"{active}/system.md")
        if not prompt_path.is_file():
            raise FileNotFoundError(prompt_path)
        return active, int(control.get("revision", 0)), prompt_path.read_text(encoding="utf-8")

    def _load_skill_index(self) -> tuple[str, list[SkillIndexEntry]]:
        skills_root = self.root / "skills"
        index_content = (skills_root / "index.md").read_text(encoding="utf-8")
        index = _parse_skill_index(index_content)
        if index.get("load_policy") != "progressive":
            raise ValueError("skills index load_policy must be progressive")
        entries = [item for item in index.get("skills", []) if item.get("enabled", False)]
        entries.sort(key=lambda item: (int(item.get("order", 0)), str(item.get("name", ""))))
        catalog: list[SkillIndexEntry] = []
        seen: set[str] = set()
        for entry in entries:
            name = str(entry.get("name", ""))
            if not SKILL_NAME.fullmatch(name) or name in seen:
                raise ValueError(f"invalid or duplicate skill name: {name!r}")
            seen.add(name)
            path = self._safe_path(skills_root, str(entry.get("path", "")))
            if not path.is_file():
                raise FileNotFoundError(path)
            catalog.append(SkillIndexEntry(
                name=name,
                description=str(entry.get("description", "")),
                path=str(entry.get("path", "")),
                requires=tuple(str(value) for value in entry.get("requires", [])),
                phases=tuple(str(value) for value in entry.get("phases", [])),
            ))
        return index_content, catalog

    def _safe_path(self, root: Path, relative_path: str) -> Path:
        if not relative_path:
            raise ValueError("runtime resource path is empty")
        resolved_root = root.resolve()
        resolved = (resolved_root / relative_path).resolve()
        if not resolved.is_relative_to(resolved_root):
            raise ValueError(f"runtime resource escapes its root: {relative_path}")
        return resolved

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        if not path.is_file():
            raise FileNotFoundError(path)
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"runtime JSON must be an object: {path}")
        return value


def _parse_skill(content: str) -> tuple[dict[str, str], str]:
    frontmatter, body = _split_frontmatter(content, "SKILL.md")
    if not frontmatter.get("name") or not frontmatter.get("description"):
        raise ValueError("SKILL.md frontmatter requires name and description")
    return frontmatter, body


def _parse_skill_index(content: str) -> dict[str, Any]:
    frontmatter, body = _split_frontmatter(content, "skills/index.md")
    skills: list[dict[str, Any]] = []
    for match in re.finditer(
        r"(?ms)^##\s+([a-z0-9][a-z0-9-]{0,63})\s*$\n(.*?)(?=^##\s+|\Z)",
        body,
    ):
        name, block = match.groups()
        fields: dict[str, str] = {}
        for line in block.splitlines():
            line = line.strip()
            if not line.startswith("- "):
                continue
            key, separator, value = line[2:].partition(":")
            if separator:
                fields[key.strip().casefold()] = value.strip()
        if not fields.get("path"):
            raise ValueError(f"skills/index.md entry {name} requires Path")
        requires = [] if fields.get("requires", "none").casefold() == "none" else [
            value.strip() for value in fields["requires"].split(",") if value.strip()
        ]
        phases = [] if fields.get("phases", "all").casefold() == "all" else [
            value.strip() for value in fields["phases"].split(",") if value.strip()
        ]
        skills.append({
            "name": name,
            "path": fields["path"],
            "description": fields.get("description", ""),
            "enabled": fields.get("enabled", "false").casefold() == "true",
            "order": int(fields.get("order", "0")),
            "requires": requires,
            "phases": phases,
        })
    if not skills:
        raise ValueError("skills/index.md must contain at least one Skill entry")
    return {**frontmatter, "skills": skills}


def _split_frontmatter(content: str, label: str) -> tuple[dict[str, str], str]:
    if not content.startswith("---\n"):
        raise ValueError(f"{label} must start with YAML frontmatter")
    end = content.find("\n---\n", 4)
    if end < 0:
        raise ValueError(f"{label} frontmatter is not terminated")
    frontmatter: dict[str, str] = {}
    for line in content[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise ValueError(f"invalid {label} frontmatter line: {line}")
        frontmatter[key.strip()] = value.strip().strip('"\'')
    return frontmatter, content[end + 5:].strip()
