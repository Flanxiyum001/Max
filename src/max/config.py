"""Configuration for MAX.

All values come from environment variables, optionally seeded from a `.env`
file in the repository root. Secrets are read from the environment only and
are never logged, printed, or written to reports.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# src/max/config.py -> parents[2] == repository root
REPO_ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file. Existing environment variables win."""
    loaded: dict[str, str] = {}
    if not path.is_file():
        return loaded
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            loaded[key] = value
    return loaded


def _apply_dotenv(repo_root: Path) -> None:
    for name in (".env", ".env.local"):
        for key, value in load_dotenv(repo_root / name).items():
            if key not in os.environ:
                os.environ[key] = value


def _split(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _bool(value: str, default: bool) -> bool:
    if value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Config:
    """Resolved MAX configuration (no secret values are stored in reprs)."""

    provider: str = "mock"
    base_url: str = ""
    model: str = ""
    api_key_env: str = "FREELLM_API_KEY"
    timeout_s: float = 60.0
    retries: int = 1

    max_iters: int = 8
    tools_allow: list[str] = field(default_factory=list)
    tools_deny: list[str] = field(default_factory=list)

    data_dir: Path = REPO_ROOT / "var"
    skills_dirs: list[Path] = field(default_factory=lambda: [REPO_ROOT / "skills"])
    workspace_dir: Path = REPO_ROOT / "workspace"
    enable_skills: bool = True

    auto_promote: list[str] = field(default_factory=lambda: ["skill", "docs"])

    openclaw_url: str = ""
    openclaw_token: str = ""  # secret-ish: never included in redacted output

    repo_root: Path = REPO_ROOT

    # ------------------------------------------------------------------ api
    @property
    def api_key(self) -> str | None:
        """API key from the environment only (never persisted by MAX)."""
        if self.api_key_env:
            value = os.environ.get(self.api_key_env)
            if value:
                return value
        return os.environ.get("MAX_API_KEY") or None

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def registry_state_path(self) -> Path:
        return self.data_dir / "registry" / "registry.json"

    @property
    def versions_dir(self) -> Path:
        return self.data_dir / "registry" / "versions"

    def redacted(self) -> dict:
        """Config snapshot that is safe to log or print."""
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "api_key_env": self.api_key_env,
            "api_key_set": bool(self.api_key),
            "timeout_s": self.timeout_s,
            "retries": self.retries,
            "max_iters": self.max_iters,
            "tools_allow": self.tools_allow,
            "tools_deny": self.tools_deny,
            "data_dir": str(self.data_dir),
            "skills_dirs": [str(p) for p in self.skills_dirs],
            "workspace_dir": str(self.workspace_dir),
            "enable_skills": self.enable_skills,
            "auto_promote": self.auto_promote,
            "openclaw_url": self.openclaw_url,
            "openclaw_token_set": bool(self.openclaw_token),
        }

    # ------------------------------------------------------------ factory
    @classmethod
    def from_env(cls, repo_root: Path | None = None, apply_dotenv: bool = True) -> "Config":
        root = (repo_root or REPO_ROOT).resolve()
        if apply_dotenv:
            _apply_dotenv(root)

        def env(name: str, default: str = "") -> str:
            return os.environ.get(name, default).strip()

        def env_path(name: str, default: str) -> Path:
            raw = env(name, default)
            p = Path(raw)
            return p if p.is_absolute() else (root / p)

        skills_raw = env("MAX_SKILLS_DIRS", "skills")
        skills_dirs = []
        for item in skills_raw.split(":"):
            item = item.strip()
            if not item:
                continue
            p = Path(item)
            skills_dirs.append(p if p.is_absolute() else root / p)
        if not skills_dirs:
            skills_dirs = [root / "skills"]

        return cls(
            provider=env("MAX_PROVIDER", "mock").lower() or "mock",
            base_url=env("MAX_BASE_URL"),
            model=env("MAX_MODEL"),
            api_key_env=env("MAX_API_KEY_ENV", "FREELLM_API_KEY"),
            timeout_s=float(env("MAX_TIMEOUT_S", "60") or 60),
            retries=int(env("MAX_RETRIES", "1") or 1),
            max_iters=int(env("MAX_MAX_ITERS", "8") or 8),
            tools_allow=_split(env("MAX_TOOLS_ALLOW")),
            tools_deny=_split(env("MAX_TOOLS_DENY")),
            data_dir=env_path("MAX_DATA_DIR", "var"),
            skills_dirs=skills_dirs,
            workspace_dir=env_path("MAX_WORKSPACE", "workspace"),
            enable_skills=_bool(env("MAX_ENABLE_SKILLS"), True),
            auto_promote=_split(env("MAX_AUTOPROMOTE", "skill,docs")) or ["skill", "docs"],
            openclaw_url=env("MAX_OPENCLAW_URL"),
            openclaw_token=env("MAX_OPENCLAW_TOKEN"),
            repo_root=root,
        )

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.data_dir / "sessions",
            self.data_dir / "audit",
            self.data_dir / "registry",
            self.staging_dir,
            self.versions_dir,
            self.workspace_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
