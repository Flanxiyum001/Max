"""Shared fixtures: isolated config, agent factory, skill + repo helpers."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """A fully isolated MAX configuration rooted in tmp_path."""
    from max.config import Config

    monkeypatch.setenv("MAX_PROVIDER", "mock")
    monkeypatch.setenv("MAX_DATA_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("MAX_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("MAX_SKILLS_DIRS", str(tmp_path / "skills"))
    monkeypatch.setenv("MAX_ENABLE_SKILLS", "1")
    monkeypatch.setenv("MAX_TOOLS_ALLOW", "")
    monkeypatch.setenv("MAX_TOOLS_DENY", "")
    monkeypatch.delenv("MAX_API_KEY", raising=False)

    config = Config.from_env(repo_root=tmp_path, apply_dotenv=False)
    config.ensure_dirs()
    config.skills_dirs[0].mkdir(parents=True, exist_ok=True)
    return config


@pytest.fixture
def agent(tmp_config):
    from max.core.factory import build_agent
    from max.providers import MockProvider

    return build_agent(tmp_config, provider=MockProvider())


def write_skill(
    root: Path,
    name: str,
    body: str = "Do the thing.",
    frontmatter: dict | None = None,
    version: str = "1.0",
) -> Path:
    """Create a valid skill directory under ``root``."""
    import yaml

    root = Path(root)
    target = root / name
    target.mkdir(parents=True, exist_ok=True)
    meta = frontmatter or {
        "name": name,
        "description": "Test skill for the MAX suite.",
        "metadata": {"version": version},
    }
    fm = yaml.safe_dump(meta, sort_keys=False)
    (target / "SKILL.md").write_text(f"---\n{fm}---\n\n{body}\n", encoding="utf-8")
    return target


@pytest.fixture
def skill_root(tmp_path):
    return tmp_path / "skills"


def git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout.strip()


@pytest.fixture
def fixture_repo(tmp_path):
    """A tiny but real git repository with one passing test."""
    repo = tmp_path / "app"
    repo.mkdir()
    (repo / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "test_app.py").write_text(
        "from app import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("# fixture app\n", encoding="utf-8")
    git("init", "-q", cwd=repo)
    git("config", "user.email", "max@example.invalid", cwd=repo)
    git("config", "user.name", "MAX Tests", cwd=repo)
    git("config", "commit.gpgsign", "false", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "baseline", cwd=repo)
    return repo


def write_proposal(path: Path, data: dict) -> Path:
    import yaml

    path = Path(path)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def default_pytest_cmd() -> list[str]:
    return [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
