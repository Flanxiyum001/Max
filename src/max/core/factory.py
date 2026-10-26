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

    policy = PermissionPolicy.from_config(config)
    audit = AuditLog(config.data_dir / "audit" / "audit.jsonl")
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
