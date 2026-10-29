"""Assembly helpers: build a fully wired MAX agent from configuration."""

from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..providers import build_provider
from .agent import Agent
from .builtins import register_builtin_tools
from .memory import MemoryStore
from .session import SessionStore
from .tools import AuditLog, PermissionPolicy, ToolContext, ToolRegistry, ToolExecutor


def build_agent(
    config: Config | None = None,
    provider=None,
    skill_registry=None,
) -> Agent:
    config = config or Config.from_env()
    config.ensure_dirs()

    provider = provider or build_provider(config)

    registry = ToolRegistry()
    register_builtin_tools(registry)

    audit = AuditLog(config.data_dir / "audit" / "audit.jsonl")
    _register_optional_tools(config, registry, audit=audit)
    _register_audit_sensitive_tools(config, registry, audit)

    policy = PermissionPolicy.from_config(config)
    if config.desktop_enabled:
        # Explicit operator opt-in: the three local-control tools leave the
        # default-deny 'exec' category. MAX_TOOLS_DENY still wins (checked
        # first), so `MAX_TOOLS_DENY=desktop_exec` revokes it immediately.
        from ..desktop import DESKTOP_TOOLS

        policy.allow.update(DESKTOP_TOOLS)
    memory = MemoryStore(config.data_dir / "MEMORY.md")
    sessions = SessionStore(config.data_dir)

    context = ToolContext(
        workspace_dir=config.workspace_dir,
        read_roots=[p.resolve() for p in config.skills_dirs] + [config.repo_root],
        memory=memory,
        registry=skill_registry,
    )
    executor = ToolExecutor(registry, policy, audit, context)

    if skill_registry is None and config.enable_skills:
        from ..skills.registry import SkillRegistry

        skill_registry = SkillRegistry(
            active_dirs=config.skills_dirs,
            staging_dir=config.staging_dir,
            state_path=config.registry_state_path,
            versions_dir=config.versions_dir,
        )
        context.registry = skill_registry

    return Agent(
        config=config,
        provider=provider,
        registry=registry,
        policy=policy,
        executor=executor,
        sessions=sessions,
        audit=audit,
        memory=memory,
        skill_registry=skill_registry,
        enable_skills=config.enable_skills,
    )


def workspace_path(config: Config) -> Path:
    config.ensure_dirs()
    return config.workspace_dir


def _register_optional_tools(config: Config, registry: ToolRegistry, audit=None) -> None:
    """Register optional capability tools. Every one of them is gated by the
    permission policy *and* by its own configuration switch, so a missing
    optional dependency degrades to an actionable error instead of a crash."""
    from ..browser import BrowserProvider, register_browser_tools
    from ..codesearch import register_symbol_tools
    from ..desktop import DesktopProvider, desktop_server_config, register_desktop_tools
    from ..mcp.backend import MCPServerManager, parse_server_configs, register_mcp_manager_tools
    from ..worker import CommandPolicy, TaskManager
    from ..worker.tools import register_worker_tools

    worker_policy = CommandPolicy(
        timeout_s=config.worker_timeout_s,
        allow_network=config.worker_allow_network,
    )
    manager = TaskManager(
        allowed_repos=list(config.worker_repos) or ([config.repo_root] if config.worker_enabled else []),
        workspace_dir=config.workspace_dir,
        policy=worker_policy,
        audit=audit,
    )
    register_worker_tools(registry, manager)

    if config.codesearch_enabled:
        register_symbol_tools(registry, config.workspace_dir)
    server_configs = parse_server_configs(config.mcp_servers)
    if config.desktop_enabled:
        # The built-in desktop server is registered from code so a single
        # switch is enough; an operator-supplied entry with id "desktop"
        # (already in MAX_MCP_SERVERS) wins instead.
        if not any(c.id == "desktop" for c in server_configs):
            server_configs = [*server_configs, desktop_server_config(config)]
    mcp_manager = MCPServerManager(server_configs, audit=audit)
    register_mcp_manager_tools(registry, mcp_manager)

    desktop = DesktopProvider(mcp_manager, enabled=config.desktop_enabled, audit=audit)
    register_desktop_tools(registry, desktop)

    browser = BrowserProvider(
        mcp_manager,
        enabled=config.browser_enabled,
        allow_hosts=tuple(config.browser_allow_hosts),
        audit=audit,
    )
    register_browser_tools(registry, browser)


def _register_audit_sensitive_tools(config: Config, registry: ToolRegistry, audit) -> None:
    """Tools that need the audit log at call time (speech diagnostics)."""
    import json

    from ..core.tools import ToolContext, ToolSpec
    from ..speech import SttConfig, SttService

    service = SttService(
        SttConfig(
            backend=config.stt_backend,
            model_size=config.stt_model_size,
            compute_type=config.stt_compute_type,
            device=config.stt_device,
            language=config.stt_language or None,
            timeout_s=config.stt_timeout_s,
            max_duration_s=config.stt_max_duration_s,
            model_dir=config.stt_model_dir,
            local_files_only=config.stt_local_files_only,
        ),
        audit=audit,
    )

    def _speech_status(args: dict, ctx: ToolContext) -> str:
        return json.dumps(service.status(), indent=2)

    registry.register(
        ToolSpec(
            name="speech_status",
            description="Report the configured speech-to-text backend and whether it is usable (no secrets).",
            parameters={"type": "object", "properties": {}},
            category="meta",
            handler=_speech_status,
        )
    )
