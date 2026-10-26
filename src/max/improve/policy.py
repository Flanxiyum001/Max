"""Trusted policy loading and integrity verification.

The policy is data owned by the operator. The pipeline loads it from a fixed
path, verifies its SHA-256 sidecar, and derives every promote/reject decision
from it — the proposing model never supplies the policy.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

import yaml

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[3] / "config" / "policy.yaml"


class PolicyError(Exception):
    pass


class PolicyIntegrityError(PolicyError):
    pass


@dataclass
class Policy:
    policy_version: int = 1
    auto_promote_categories: set[str] = field(default_factory=lambda: {"skill", "docs"})
    sensitive_categories: set[str] = field(default_factory=set)
    protected_paths: list[str] = field(default_factory=list)
    deny_line_patterns: list[str] = field(default_factory=list)
    max_changed_lines: int = 400
    max_changed_files: int = 8
    require_zero_failures: bool = True
    source_path: Path | None = None
    digest: str = ""

    # ------------------------------------------------------------ loading
    @classmethod
    def load(cls, path: Path | None = None, verify: bool = True) -> "Policy":
        path = Path(path or DEFAULT_POLICY_PATH)
        if not path.is_file():
            raise PolicyError(f"policy file not found: {path}")
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()

        if verify:
            sidecar = path.with_suffix(".sha256")
            if sidecar.is_file():
                expected = sidecar.read_text(encoding="utf-8").strip().split()[0]
                if expected != digest:
                    raise PolicyIntegrityError(
                        "policy integrity check failed: config/policy.yaml does not match "
                        "config/policy.sha256. If the change was intentional and reviewed, "
                        "run `max improve trust-policy` to re-pin it."
                    )

        data = yaml.safe_load(raw.decode("utf-8")) or {}
        if not isinstance(data, dict):
            raise PolicyError("policy file must be a YAML mapping")

        policy = cls(
            policy_version=int(data.get("policy_version", 1)),
            auto_promote_categories=set(data.get("auto_promote_categories") or []),
            sensitive_categories=set(data.get("sensitive_categories") or []),
            protected_paths=list(data.get("protected_paths") or []),
            deny_line_patterns=list(data.get("deny_line_patterns") or []),
            max_changed_lines=int(data.get("max_changed_lines", 400)),
            max_changed_files=int(data.get("max_changed_files", 8)),
            require_zero_failures=bool(data.get("require_zero_failures", True)),
            source_path=path,
            digest=digest,
        )
        return policy

    # ---------------------------------------------------------- decisions
    def touches_protected(self, paths: list[str]) -> list[str]:
        hits = []
        for path in paths:
            for pattern in self.protected_paths:
                if fnmatch(path, pattern) or path == pattern.rstrip("*") or path.startswith(pattern.rstrip("*")):
                    hits.append(path)
                    break
        return sorted(set(hits))

    def is_sensitive(self, category: str) -> bool:
        return category in self.sensitive_categories

    def decide(
        self,
        *,
        category: str,
        checks_ok: bool,
        changed_paths: list[str],
        changed_lines: int,
        existing_tests_modified: bool,
    ) -> tuple[str, list[str]]:
        """Return (action, reasons) where action is auto|human|reject."""
        reasons: list[str] = []
        protected = self.touches_protected(changed_paths)
        if protected:
            return "reject", [f"touches protected path(s): {', '.join(protected)}"]
        if not checks_ok:
            return "reject", ["one or more checks failed"]
        if self.is_sensitive(category):
            return "human", [f"category '{category}' is sensitive and always requires human approval"]
        if existing_tests_modified:
            return "human", ["existing test files were modified (requires human review)"]
        if changed_lines > self.max_changed_lines:
            return "human", [f"change size {changed_lines} > max_changed_lines {self.max_changed_lines}"]
        if len(changed_paths) > self.max_changed_files:
            return "human", [f"files changed {len(changed_paths)} > max_changed_files {self.max_changed_files}"]
        if category not in self.auto_promote_categories:
            return "human", [f"category '{category}' is not in auto_promote_categories"]
        reasons.append(f"category '{category}' is auto-promotable and all checks passed")
        return "auto", reasons

    # ----------------------------------------------------------- helpers
    @staticmethod
    def matches_any(text: str, patterns: list[str]) -> str | None:
        for pattern in patterns:
            if re.search(pattern, text):
                return pattern
        return None

    def to_dict(self) -> dict:
        return {
            "policy_version": self.policy_version,
            "auto_promote_categories": sorted(self.auto_promote_categories),
            "sensitive_categories": sorted(self.sensitive_categories),
            "protected_paths": self.protected_paths,
            "max_changed_lines": self.max_changed_lines,
            "max_changed_files": self.max_changed_files,
            "require_zero_failures": self.require_zero_failures,
            "digest": self.digest,
        }


def write_integrity(path: Path | None = None) -> str:
    """(Re)pin the policy hash. Operator action only — see docs/SECURITY.md."""
    path = Path(path or DEFAULT_POLICY_PATH)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    sidecar = path.with_suffix(".sha256")
    sidecar.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    return digest
