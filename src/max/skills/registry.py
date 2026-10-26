"""Agent Skills registry: discovery, validation, staging, provenance, rollback.

Follows the Agent Skills standard (github.com/agentskills/agentskills):
each skill is a directory containing ``SKILL.md`` with YAML frontmatter and
the fields ``name``, ``description`` (required), ``license``, ``compatibility``,
``metadata`` (string->string), ``allowed-tools`` (space separated).

MAX adds on top of the standard:
* sha256 provenance for every version,
* candidate staging with evaluation history,
* promotion only after a passing evaluation and zero security flags,
* rollback to the last known-good archived version,
* security flagging of skills that contain prompt-injection language or
  request shell/network capabilities (flagged skills cannot be loaded).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..core.tools import INJECTION_PATTERNS

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
CURL_PIPE_SH = re.compile(r"(?i)\b(curl|wget)\b[^\n|;]*\|\s*(ba|z|k)?sh\b")
SECRET_ASSIGN = re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{16,}")
EXEC_TOOLS = {"bash", "shell", "run_command", "exec", "subprocess"}


class SkillError(Exception):
    """Raised for validation, staging, promotion and rollback failures."""


@dataclass
class SkillRecord:
    name: str
    path: str
    description: str = ""
    version: str = "0.0.0"
    license: str = ""
    compatibility: str = ""
    allowed_tools: str = ""
    metadata: dict = field(default_factory=dict)
    sha256: str = ""
    status: str = "unknown"
    errors: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    body: str = ""

    @property
    def valid(self) -> bool:
        return not self.errors

    @property
    def safe(self) -> bool:
        return not self.flags

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "description": self.description,
            "version": self.version,
            "license": self.license,
            "compatibility": self.compatibility,
            "allowed_tools": self.allowed_tools,
            "metadata": self.metadata,
            "sha256": self.sha256,
            "status": self.status,
            "errors": self.errors,
            "flags": self.flags,
        }


def _split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---"):
        raise SkillError("SKILL.md must start with YAML frontmatter (---)")
    lines = text.splitlines()
    if lines[0].strip() != "---":
        raise SkillError("SKILL.md must start with YAML frontmatter (---)")
    for index in range(1, len(lines)):
        if lines[index].strip() in {"---", "..."}:
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
    raise SkillError("unterminated YAML frontmatter")


def security_flags(body: str, allowed_tools: str) -> list[str]:
    flags: list[str] = []
    for pattern in INJECTION_PATTERNS:
        if pattern.search(body):
            flags.append("prompt-injection-language")
            break
    if CURL_PIPE_SH.search(body):
        flags.append("pipe-to-shell")
    if SECRET_ASSIGN.search(body):
        flags.append("hardcoded-secret")
    declared = {
        tool.strip().split("(", 1)[0].lower()
        for tool in (allowed_tools or "").split()
        if tool.strip()
    }
    if declared & EXEC_TOOLS:
        flags.append("requests-exec-tool")
    return sorted(set(flags))


def validate_skill_dir(path: Path) -> SkillRecord:
    path = Path(path)
    record = SkillRecord(name=path.name, path=str(path))

    skill_file = path / "SKILL.md"
    if not skill_file.is_file():
        record.errors.append("missing SKILL.md")
        return record

    raw = skill_file.read_text(encoding="utf-8", errors="replace")
    record.sha256 = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    try:
        frontmatter_text, body = _split_frontmatter(raw)
    except SkillError as exc:
        record.errors.append(str(exc))
        return record
    record.body = body

    try:
        meta = yaml.safe_load(frontmatter_text) if frontmatter_text.strip() else None
    except yaml.YAMLError as exc:
        record.errors.append(f"invalid YAML frontmatter: {exc}")
        return record
    if not isinstance(meta, dict):
        record.errors.append("frontmatter must be a YAML mapping")
        return record

    # --- name -------------------------------------------------------------
    name = meta.get("name")
    if not isinstance(name, str) or not name.strip():
        record.errors.append("missing required field 'name'")
    else:
        name = name.strip()
        if not (1 <= len(name) <= 64):
            record.errors.append("'name' must be 1-64 characters")
        elif not NAME_RE.match(name):
            record.errors.append(
                "'name' must be lowercase letters/numbers, hyphens not leading, "
                "trailing or doubled"
            )
        elif name != path.name:
            record.errors.append(f"'name' ({name}) must match directory name ({path.name})")
        record.name = name

    # --- description ------------------------------------------------------
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        record.errors.append("missing required field 'description'")
    elif not (1 <= len(description.strip()) <= 1024):
        record.errors.append("'description' must be 1-1024 characters")
    else:
        record.description = description.strip()

    # --- optional standard fields ----------------------------------------
    for key, attr, limit in (
        ("license", "license", None),
        ("compatibility", "compatibility", 500),
        ("allowed-tools", "allowed_tools", None),
    ):
        value = meta.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            record.errors.append(f"'{key}' must be a string")
            continue
        if limit and len(value) > limit:
            record.errors.append(f"'{key}' must be {limit} characters or fewer")
            continue
        setattr(record, attr, value)

    metadata = meta.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            record.errors.append("'metadata' must be a mapping")
        else:
            bad = [k for k, v in metadata.items() if not isinstance(k, str) or not isinstance(v, (str, int, float, bool))]
            if bad:
                record.errors.append(f"'metadata' values must be scalars (bad keys: {bad[:3]})")
            record.metadata = {str(k): str(v) for k, v in metadata.items()}

    unknown = set(meta) - {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
    if unknown:
        record.errors.append(f"unknown frontmatter fields: {sorted(unknown)}")

    version = record.metadata.get("version", "0.0.0")
    record.version = str(version)

    if not record.errors:
        record.flags = security_flags(body, record.allowed_tools)
        record.status = "flagged" if record.flags else "ok"
    return record


class SkillRegistry:
    def __init__(
        self,
        active_dirs: list[Path],
        staging_dir: Path,
        state_path: Path,
        versions_dir: Path,
    ):
        self.active_dirs = [Path(p) for p in active_dirs]
        self.staging_dir = Path(staging_dir)
        self.state_path = Path(state_path)
        self.versions_dir = Path(versions_dir)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------- state
    def _load_state(self) -> dict:
        if not self.state_path.is_file():
            return {"skills": {}, "staging": {}}
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except ValueError:
            return {"skills": {}, "staging": {}}
        state.setdefault("skills", {})
        state.setdefault("staging", {})
        return state

    def _save_state(self, state: dict) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.state_path)

    # ---------------------------------------------------------- discovery
    def discover(self) -> list[SkillRecord]:
        state = self._load_state()
        records: list[SkillRecord] = []
        for root in self.active_dirs:
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                if not child.is_dir() or child.name.startswith((".", "_")):
                    continue
                record = validate_skill_dir(child)
                entry = state["skills"].get(record.name, {})
                current = entry.get("current")
                if record.errors:
                    record.status = "invalid"
                elif record.flags:
                    record.status = "flagged"
                elif current is None:
                    record.status = "untracked"
                elif current.get("sha256") != record.sha256:
                    record.status = "modified"  # provenance drift
                else:
                    record.status = "active"
                records.append(record)
        return records

    def get(self, name: str) -> SkillRecord | None:
        for record in self.discover():
            if record.name == name:
                return record
        return None

    def index_block(self) -> str:
        usable = [r for r in self.discover() if r.valid and r.safe]
        if not usable:
            return ""
        lines = ["<available_skills>"]
        lines += [f"- {r.name}: {r.description[:200]}" for r in usable]
        lines.append("</available_skills>")
        return "\n".join(lines)

    # ------------------------------------------------------------- loading
    def load(self, name: str) -> str:
        record = self.get(name)
        if record is None:
            raise SkillError(f"skill '{name}' not found")
        if record.errors:
            raise SkillError(f"skill '{name}' is invalid: {'; '.join(record.errors)}")
        if record.flags:
            raise SkillError(f"skill '{name}' is blocked by security flags: {', '.join(record.flags)}")
        state = self._load_state()
        current = state["skills"].get(name, {}).get("current")
        if current and current.get("sha256") and current["sha256"] != record.sha256:
            raise SkillError(
                f"skill '{name}' was modified outside the registry "
                "(provenance drift); re-promote or roll back to a known-good version"
            )
        return record.body

    # ------------------------------------------------------------ staging
    def stage(self, source: Path) -> SkillRecord:
        source = Path(source)
        record = validate_skill_dir(source)
        if record.errors:
            raise SkillError(f"candidate rejected: {'; '.join(record.errors)}")
        target = self.staging_dir / record.name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
        record = validate_skill_dir(target)

        state = self._load_state()
        state["staging"][record.name] = {
            "name": record.name,
            "version": record.version,
            "sha256": record.sha256,
            "path": str(target),
            "status": "flagged" if record.flags else "staged",
            "flags": record.flags,
            "errors": record.errors,
            "evals": [],
            "staged_at": _now(),
        }
        self._save_state(state)
        record.status = state["staging"][record.name]["status"]
        return record

    def stage_text(self, name: str, frontmatter: dict, body: str) -> SkillRecord:
        target = self.staging_dir / name
        target.mkdir(parents=True, exist_ok=True)
        fm = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
        (target / "SKILL.md").write_text(f"---\n{fm}---\n\n{body}\n", encoding="utf-8")
        return self.stage(target)

    def staging_entry(self, name: str) -> dict | None:
        return self._load_state()["staging"].get(name)

    # --------------------------------------------------------- evaluation
    def evaluate(self, name: str, results: dict) -> dict:
        state = self._load_state()
        entry = state["staging"].get(name)
        if entry is None:
            raise SkillError(f"skill '{name}' is not staged")
        passed = bool(results.get("passed", False))
        record = validate_skill_dir(Path(entry["path"]))
        entry["evals"].append(
            {
                "ts": _now(),
                "passed": passed,
                "checks": results.get("checks", []),
                "notes": results.get("notes", ""),
                "sha256_at_eval": record.sha256,
            }
        )
        if record.errors or record.flags:
            entry["status"] = "rejected"
        else:
            entry["status"] = "good" if passed else "rejected"
        self._save_state(state)
        return entry

    # ------------------------------------------------------------ promote
    def promote(self, name: str) -> SkillRecord:
        state = self._load_state()
        entry = state["staging"].get(name)
        if entry is None:
            raise SkillError(f"skill '{name}' is not staged")
        staged_path = Path(entry["path"])
        record = validate_skill_dir(staged_path)
        if record.errors:
            raise SkillError(f"staged skill is invalid: {'; '.join(record.errors)}")
        if record.flags:
            raise SkillError(f"staged skill blocked by security flags: {', '.join(record.flags)}")
        if entry.get("status") != "good":
            raise SkillError(
                f"skill '{name}' has no passing evaluation (status={entry.get('status')}); "
                "run an evaluation before promoting"
            )
        if record.sha256 != entry["sha256"]:
            raise SkillError("staged skill changed since it was evaluated; re-run the evaluation")

        active_root = self._skill_root(name)
        entry_state = state["skills"].setdefault(name, {"current": None, "history": []})

        # Archive the currently active version before replacing it.
        if entry_state.get("current"):
            current = entry_state["current"]
            archive = self._archive(name, current["version"], active_root)
            if archive:
                current["archive"] = str(archive)
            current["status"] = "superseded"
            current["superseded_at"] = _now()
            entry_state["history"].append(current)

        if active_root.exists():
            shutil.rmtree(active_root)
        shutil.copytree(staged_path, active_root)
        shutil.rmtree(staged_path)

        entry_state["current"] = {
            "name": name,
            "version": record.version,
            "sha256": record.sha256,
            "status": "active",
            "promoted_at": _now(),
            "source": "staging",
        }
        state["staging"].pop(name, None)
        self._save_state(state)
        promoted = validate_skill_dir(active_root)
        promoted.status = "active"
        return promoted

    # ----------------------------------------------------------- rollback
    def rollback(self, name: str) -> SkillRecord:
        state = self._load_state()
        entry_state = state["skills"].get(name)
        if not entry_state or not entry_state.get("current"):
            raise SkillError(f"skill '{name}' has no active version")
        history = entry_state.get("history", [])
        current_sha = entry_state["current"].get("sha256")
        archived = [
            h
            for h in history
            if h.get("status") == "superseded"
            and h.get("sha256") != current_sha
            and h.get("archive")
            and Path(h["archive"]).is_dir()
        ]
        if not archived:
            raise SkillError(f"skill '{name}' has no archived known-good version to restore")

        target = archived[-1]
        active_root = self._skill_root(name)

        # Keep the version we are rolling away from, so we can roll forward again.
        bad_archive = self._archive(name, entry_state["current"]["version"], active_root)
        current = dict(entry_state["current"])
        current["status"] = "rolled_back"
        current["rolled_back_at"] = _now()
        if bad_archive:
            current["archive"] = str(bad_archive)
        entry_state["history"].append(current)

        if active_root.exists():
            shutil.rmtree(active_root)
        shutil.copytree(target["archive"], active_root)

        restored = validate_skill_dir(active_root)
        if restored.errors or restored.flags:
            raise SkillError(
                f"rollback target failed validation: {restored.errors or restored.flags}"
            )
        restored.status = "active"
        entry_state["current"] = {
            "name": name,
            "version": restored.version,
            "sha256": restored.sha256,
            "status": "active",
            "promoted_at": target.get("promoted_at", ""),
            "restored_at": _now(),
            "rollback_of": current["version"],
        }
        self._save_state(state)
        return restored

    # ------------------------------------------------------------- views
    def history(self, name: str) -> dict:
        state = self._load_state()
        return {
            "current": state["skills"].get(name, {}).get("current"),
            "history": state["skills"].get(name, {}).get("history", []),
            "staging": state["staging"].get(name),
        }

    # ----------------------------------------------------------- helpers
    def _skill_root(self, name: str) -> Path:
        # Active skills live in the first configured active directory.
        root = self.active_dirs[0]
        root.mkdir(parents=True, exist_ok=True)
        return root / name

    def _archive(self, name: str, version: str, source: Path) -> Path | None:
        if not source.is_dir():
            return None
        dest = self.versions_dir / name / f"{version}-{int(time.time())}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        n = 1
        while dest.exists():
            dest = self.versions_dir / name / f"{version}-{int(time.time())}-{n}"
            n += 1
        shutil.copytree(source, dest)
        return dest


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
