"""Static analysis and security scanning for candidate changes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Finding:
    rule: str
    file: str
    line: int
    message: str
    severity: str  # error | warning | info

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "file": self.file,
            "line": self.line,
            "message": self.message,
            "severity": self.severity,
        }


# (rule_id, regex, severity, message)
PY_RULES: list[tuple[str, re.Pattern, str, str]] = [
    ("bare-except", re.compile(r"^\s*except\s*:"), "error", "bare 'except:' swallows SystemExit/KeyboardInterrupt"),
    ("eval-call", re.compile(r"\beval\s*\("), "error", "the eval builtin executes arbitrary code on untrusted input"),
    ("exec-call", re.compile(r"\bexec\s*\("), "error", "the exec builtin executes arbitrary code on untrusted input"),
    ("shell-true", re.compile(r"shell\s*=\s*True"), "error", "enabling shell mode allows command injection"),
    ("os-system", re.compile(r"\bos\.system\s*\("), "error", "the os.system builtin enables shell injection"),
    ("unsafe-yaml-load", re.compile(r"(?<!safe_)yaml\.load\s*\("), "error", "unsafe yaml loading can execute code (use the safe loader)"),
    ("pickle-load", re.compile(r"pickle\.loads?\s*\("), "warning", "pickle deserialization can execute code"),
    ("md5-hash", re.compile(r"hashlib\.md5\s*\("), "warning", "MD5 is not collision resistant"),
    ("random-token", re.compile(r"(?<!secrets\.)random\.(choice|randint|random)\s*\("), "warning", "random is not suitable for secrets"),
    ("tls-no-verify", re.compile(r"verify\s*=\s*False"), "error", "TLS verification disabled"),
    ("debug-true", re.compile(r"debug\s*=\s*True"), "warning", "debug mode left enabled"),
    ("unfinished-work", re.compile(r"(?i)\btod[o]\b|\bfixm[e]\b|\bxx[x]\b"), "info", "unfinished work marker"),
    ("urlopen-no-timeout", re.compile(r"urlopen\s*\((?![^\n]*timeout)"), "error", "urlopen without a timeout can hang forever"),
]

SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("generic-long-secret", re.compile(r"(?i)\b(api[_-]?key|secret|token|password)\b\s*[=:]\s*[\"'][A-Za-z0-9_\-]{24,}[\"']")),
]


def scan_python_file(path: Path, repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [Finding("unreadable", str(path), 0, str(exc), "warning")]
    rel = str(path.relative_to(repo_root)) if path.is_absolute() and repo_root in path.parents else str(path)
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        for rule, pattern, severity, message in PY_RULES:
            if pattern.search(line):
                findings.append(Finding(rule, rel, lineno, message, severity))
    return findings


def scan_repo(repo_root: Path, subdirs: tuple[str, ...] = ("src",)) -> list[Finding]:
    findings: list[Finding] = []
    for sub in subdirs:
        base = repo_root / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            findings.extend(scan_python_file(path, repo_root))
    return findings


def scan_added_lines(diff_text: str) -> list[Finding]:
    """Scan added lines of a unified diff for secrets and dangerous patterns."""
    findings: list[Finding] = []
    current_file = ""
    lineno = 0
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:].strip()
            lineno = 0
            continue
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            lineno = int(match.group(1)) if match else 0
            continue
        if line.startswith("+") and not line.startswith("+++"):
            content = line[1:]
            for rule, pattern in SECRET_PATTERNS:
                if pattern.search(content):
                    findings.append(
                        Finding(f"secret:{rule}", current_file, lineno, "possible secret committed in added line", "error")
                    )
            lineno += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        elif not line.startswith(("\\", "diff ", "index ", "new file", "old mode", "deleted")):
            lineno += 1
    return findings


def changed_paths_and_stats(diff_text: str) -> tuple[list[str], int, int]:
    paths: list[str] = []
    added = removed = 0
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:].strip()
            if path not in paths:
                paths.append(path)
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return paths, added, removed
