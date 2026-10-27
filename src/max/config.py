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

    # ------------------------------------------- optional integrations
    # Coding worker (isolated git worktree + bounded command execution).
    worker_enabled: bool = False
    worker_repos: list[Path] = field(default_factory=list)
    worker_timeout_s: float = 120.0
    worker_allow_network: bool = False

    # Syntax-aware repository intelligence (tree-sitter when installed).
    codesearch_enabled: bool = True

    # MCP servers: JSON array, parsed by max.mcp.backend.parse_server_configs.
    mcp_servers: str = ""

    # Browser research (optional Playwright MCP provider), off by default.
    browser_enabled: bool = False
    browser_allow_hosts: list[str] = field(default_factory=list)

    # Speech-to-text (mock by default; faster-whisper is an optional extra).
    stt_backend: str = "mock"
    stt_model_size: str = "tiny"
    stt_compute_type: str = "int8"
    stt_device: str = "cpu"
    stt_language: str = ""
    stt_timeout_s: float = 30.0
    stt_max_duration_s: float = 300.0
    stt_model_dir: str = ""
    stt_local_files_only: bool = True

    # Supervised multi-agent orchestration (optional; disabled => single-agent MAX).
    orch_enabled: bool = True
    orch_auto: bool = True  # dynamic delegation decision in conversation
    orch_max_workers: int = 2  # configured ceiling; hard cap is 3
    orch_depth: int = 1  # child delegation levels
    orch_task_timeout_s: float = 180.0
    orch_tool_budget: int = 120  # tool calls shared by all workers in one plan
    orch_exec: bool = False  # guarded test/command runner for coder+tester
    orch_keep_workspaces: bool = False  # keep task worktrees for inspection
    orch_coder_backend: str = "max"  # max | openhands
    orch_openhands_model: str = ""
    orch_openhands_base_url: str = ""
    orch_openhands_key_env: str = ""
    orch_openhands_iters: int = 8

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
            "worker": {
                "enabled": self.worker_enabled,
                "repos": [str(p) for p in self.worker_repos],
                "timeout_s": self.worker_timeout_s,
                "allow_network": self.worker_allow_network,
            },
            "codesearch_enabled": self.codesearch_enabled,
            "mcp_servers_configured": bool(self.mcp_servers.strip()),
            "browser": {"enabled": self.browser_enabled, "allow_hosts": self.browser_allow_hosts},
            "orchestration": {
                "enabled": self.orch_enabled,
                "auto_delegate": self.orch_auto,
                "max_workers": self.orch_max_workers,
                "depth": self.orch_depth,
                "task_timeout_s": self.orch_task_timeout_s,
                "tool_budget": self.orch_tool_budget,
                "exec_enabled": self.orch_exec,
                "keep_workspaces": self.orch_keep_workspaces,
                "coder_backend": self.orch_coder_backend,
                "openhands": {
                    "model": self.orch_openhands_model,
                    "base_url": self.orch_openhands_base_url,
                    "key_env": self.orch_openhands_key_env,
                    "key_set": bool(
                        self.orch_openhands_key_env
                        and __import__("os").environ.get(self.orch_openhands_key_env)
                    ),
                },
            },
            "stt": {
                "backend": self.stt_backend,
                "model_size": self.stt_model_size,
                "device": self.stt_device,
                "timeout_s": self.stt_timeout_s,
                "local_files_only": self.stt_local_files_only,
            },
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

        # The pre-v1 Railway service uses AI_* variables. Respect explicit
        # MAX_* settings first, while keeping a fresh install offline.
        legacy_base_url = env("AI_BASE_URL")
        legacy_model = env("AI_MODEL")
        legacy_ready = bool(legacy_base_url and legacy_model)
        provider_name = env("MAX_PROVIDER") or ("openai" if legacy_ready else "mock")
        key_env = env("MAX_API_KEY_ENV") or ("AI_API_KEY" if legacy_ready else "FREELLM_API_KEY")

        return cls(
            provider=provider_name.lower(),
            base_url=env("MAX_BASE_URL") or legacy_base_url,
            model=env("MAX_MODEL") or legacy_model,
            api_key_env=key_env,
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
            worker_enabled=_bool(env("MAX_WORKER_ENABLE"), False),
            worker_repos=[
                (Path(item) if Path(item).is_absolute() else root / item)
                for item in _split(env("MAX_WORKER_REPOS"))
            ],
            worker_timeout_s=float(env("MAX_WORKER_TIMEOUT_S", "120") or 120),
            worker_allow_network=_bool(env("MAX_WORKER_NETWORK"), False),
            codesearch_enabled=_bool(env("MAX_CODESEARCH_ENABLE"), True),
            mcp_servers=env("MAX_MCP_SERVERS"),
            browser_enabled=_bool(env("MAX_BROWSER_ENABLE"), False),
            browser_allow_hosts=_split(env("MAX_BROWSER_ALLOW_HOSTS")),
            stt_backend=env("MAX_STT_BACKEND", "mock").lower() or "mock",
            stt_model_size=env("MAX_STT_MODEL_SIZE", "tiny") or "tiny",
            stt_compute_type=env("MAX_STT_COMPUTE_TYPE", "int8") or "int8",
            stt_device=env("MAX_STT_DEVICE", "cpu") or "cpu",
            stt_language=env("MAX_STT_LANGUAGE"),
            stt_timeout_s=float(env("MAX_STT_TIMEOUT_S", "30") or 30),
            stt_max_duration_s=float(env("MAX_STT_MAX_DURATION_S", "300") or 300),
            stt_model_dir=env("MAX_STT_MODEL_DIR"),
            stt_local_files_only=_bool(env("MAX_STT_LOCAL_FILES_ONLY"), True),
            orch_enabled=_bool(env("MAX_ORCH_ENABLE"), True),
            orch_auto=_bool(env("MAX_ORCH_AUTO"), True),
            orch_max_workers=min(max(int(env("MAX_ORCH_MAX_WORKERS", "2") or 2), 1), 3),
            orch_depth=max(int(env("MAX_ORCH_DEPTH", "1") or 1), 0),
            orch_task_timeout_s=float(env("MAX_ORCH_TASK_TIMEOUT_S", "180") or 180),
            orch_tool_budget=max(int(env("MAX_ORCH_TOOL_BUDGET", "120") or 120), 1),
            orch_exec=_bool(env("MAX_ORCH_EXEC"), False),
            orch_keep_workspaces=_bool(env("MAX_ORCH_KEEP_WORKSPACES"), False),
            orch_coder_backend=(env("MAX_ORCH_CODER_BACKEND", "max") or "max").lower(),
            orch_openhands_model=env("MAX_ORCH_OH_MODEL"),
            orch_openhands_base_url=env("MAX_ORCH_OH_BASE_URL"),
            orch_openhands_key_env=env("MAX_ORCH_OH_KEY_ENV"),
            orch_openhands_iters=int(env("MAX_ORCH_OH_ITERS", "8") or 8),
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
