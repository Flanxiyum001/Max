"""Self-improvement pipeline: isolation, gates, policy, promote, rollback."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import git, write_proposal
from max.improve import (
    ImproveError,
    Policy,
    PolicyIntegrityError,
    Proposal,
    list_reports,
    load_report,
    rollback,
    run,
    write_integrity,
)

# ------------------------------------------------------------- patch helpers

BASELINE_FILES = {"app.py", "test_app.py", "README.md"}


def make_patch(repo: Path, edits: dict[str, str]) -> str:
    """Produce a unified diff against HEAD for ``edits``, then reset the repo."""
    for path, content in edits.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    git("add", "-A", cwd=repo)
    patch = git("diff", "--cached", "HEAD", cwd=repo)
    git("reset", "-q", "--hard", "HEAD", cwd=repo)
    for path in edits:
        if path not in BASELINE_FILES and (repo / path).exists():
            (repo / path).unlink()
    return patch


def proposal(pid: str, patch: str, category: str = "docs", title: str = "t") -> Proposal:
    return Proposal(id=pid, title=title, category=category, patch=patch, rationale="test proposal")


# ------------------------------------------------------------------ basics
def test_baseline_suite_runs_on_fixture(fixture_repo):
    from max.improve.pipeline import run_test_suite

    result = run_test_suite(fixture_repo)
    assert result["ok"] is True
    assert result["passed"] == 1
    assert result["failed"] == 0


def test_proposal_yaml_loading(tmp_path):
    path = write_proposal(
        tmp_path / "p.yaml",
        {"id": "p1", "category": "docs", "title": "T", "patch": "--- a/x\n+++ b/x\n", "rationale": "r"},
    )
    loaded = Proposal.load(path)
    assert loaded.id == "p1" and loaded.category == "docs" and loaded.patch.startswith("---")


def test_missing_git_repo_raises(fixture_repo, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ImproveError, match="needs git"):
        run(proposal("x", "--- a/README.md\n+++ b/README.md\n"), plain)


# --------------------------------------------------------- happy paths
def test_docs_change_auto_promotes_and_commits(fixture_repo):
    patch = make_patch(fixture_repo, {"README.md": "# fixture app\nDocumented by MAX.\n"})
    report = run(proposal("docs-1", patch, category="docs", title="document"), fixture_repo)
    assert report["verdict"] == "promoted"
    assert report["status"] == "promoted"
    assert report["commit"]
    assert "Documented by MAX." in (fixture_repo / "README.md").read_text()
    subject = git("log", "-1", "--pretty=%s", cwd=fixture_repo)
    assert subject.startswith("max(improve): docs-1")
    # report is persisted
    assert load_report("docs-1", fixture_repo)["status"] == "promoted"
    assert [r["id"] for r in list_reports(fixture_repo)] == ["docs-1"]


def test_rollback_restores_file_and_reverts_commit(fixture_repo):
    patch = make_patch(fixture_repo, {"README.md": "# fixture app\nTemporary change.\n"})
    report = run(proposal("docs-2", patch, category="docs"), fixture_repo)
    assert report["status"] == "promoted"
    assert "Temporary change." in (fixture_repo / "README.md").read_text()

    result = rollback("docs-2", fixture_repo)
    assert result["status"] == "rolled_back"
    assert result["rollback"]["method"] == "git-revert"
    assert "Temporary change." not in (fixture_repo / "README.md").read_text()
    with pytest.raises(ImproveError, match="already rolled back"):
        rollback("docs-2", fixture_repo)


def test_code_change_waits_for_human_approval_then_promotes(fixture_repo):
    patch = make_patch(
        fixture_repo,
        {
            "app.py": "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n",
            "test_mul.py": "from app import mul\n\n\ndef test_mul():\n    assert mul(3, 4) == 12\n",
        },
    )
    report = run(proposal("code-1", patch, category="code", title="add mul"), fixture_repo)
    assert report["verdict"] == "awaiting_approval"
    assert report["status"] == "pending_approval"
    assert any("not in auto_promote_categories" in r for r in report["reasons"])
    assert not (fixture_repo / "test_mul.py").exists()  # nothing applied yet

    approved = __import__("max.improve", fromlist=["approve"]).approve("code-1", fixture_repo)
    assert approved["status"] == "promoted"
    assert approved["approved"] is True
    assert (fixture_repo / "test_mul.py").exists()
    assert "def mul" in (fixture_repo / "app.py").read_text()


# ------------------------------------------------------------- rejection
def test_failing_change_is_rejected(fixture_repo):
    patch = make_patch(fixture_repo, {"app.py": "def add(a, b):\n    return a - b\n"})
    report = run(proposal("bad-1", patch, category="code"), fixture_repo)
    assert report["verdict"] == "rejected"
    assert report["candidate"]["failed"] >= 1
    check = next(c for c in report["checks"] if c["name"] == "no-test-regression")
    assert check["passed"] is False
    # working tree untouched
    assert "return a + b" in (fixture_repo / "app.py").read_text()


def test_failing_change_is_rolled_back_after_forced_promotion(fixture_repo):
    """post-promote test failure restores the originals automatically."""
    patch = make_patch(fixture_repo, {"app.py": "def add(a, b):\n    return a - b\n"})
    report = run(
        proposal("bad-2", patch, category="docs"),  # docs would auto-promote...
        fixture_repo,
    )
    # ...but regression gate rejects it before any application
    assert report["status"] == "rejected"
    assert "return a + b" in (fixture_repo / "app.py").read_text()


def test_non_applying_patch_rejected(fixture_repo):
    report = run(
        proposal("bad-3", "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -99 +99 @@\n+nope\n"),
        fixture_repo,
    )
    assert report["verdict"] == "rejected"
    assert any(c["name"] == "patch-applies" and not c["passed"] for c in report["checks"])


def test_denied_pattern_rejected(fixture_repo):
    patch = make_patch(fixture_repo, {"app.py": "def add(a, b):\n    return a + b\n\n\nbad = eval('1+1')\n"})
    report = run(proposal("bad-4", patch, category="code"), fixture_repo)
    assert report["status"] == "rejected"
    check = next(c for c in report["checks"] if c["name"] == "static-analysis")
    assert check["passed"] is False


def test_secret_in_diff_rejected(fixture_repo):
    patch = make_patch(
        fixture_repo,
        {"app.py": 'def add(a, b):\n    return a + b\n\n\nKEY = "sk-abcdefghijklmnop123456"\n'},
    )
    report = run(proposal("bad-5", patch, category="code"), fixture_repo)
    assert report["status"] == "rejected"
    check = next(c for c in report["checks"] if c["name"] == "secret-scan")
    assert check["passed"] is False


# --------------------------------------------------------------- policy
def test_protected_path_cannot_be_modified(fixture_repo):
    patch = make_patch(fixture_repo, {"config/policy.yaml": "policy_version: 999\n"})
    report = run(proposal("evil-1", patch, category="config"), fixture_repo)
    assert report["status"] == "rejected"
    check = next(c for c in report["checks"] if c["name"] == "protected-paths")
    assert check["passed"] is False
    assert any("protected path" in r for r in report["reasons"])


def test_sensitive_category_never_auto_promotes_even_if_configured(fixture_repo):
    policy = Policy.load()
    policy.auto_promote_categories = set(policy.auto_promote_categories) | {"credentials"}
    patch = make_patch(fixture_repo, {"README.md": "# fixture app\ncredential rotation note\n"})
    report = run(proposal("sens-1", patch, category="credentials"), fixture_repo, policy=policy)
    assert report["status"] == "pending_approval"
    assert any("sensitive" in r for r in report["reasons"])


def test_existing_test_modification_requires_human(fixture_repo):
    # a *passing* change that still touches an existing test file
    patch = make_patch(
        fixture_repo,
        {
            "test_app.py": (
                "from app import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n\n\n"
                "def test_add_more():\n    assert add(1, 1) == 2\n"
            )
        },
    )
    report = run(proposal("t-1", patch, category="code"), fixture_repo)
    assert report["status"] == "pending_approval"
    assert any("existing test files" in r for r in report["reasons"])


def test_policy_integrity_pin_detects_tampering(tmp_path):
    import shutil

    policy_src = Path(__file__).resolve().parents[1] / "config" / "policy.yaml"
    copy = tmp_path / "policy.yaml"
    shutil.copy(policy_src, copy)
    write_integrity(copy)
    Policy.load(copy)  # verifies fine

    copy.write_text(copy.read_text() + "\nauto_promote_categories: [everything]\n", encoding="utf-8")
    with pytest.raises(PolicyIntegrityError, match="integrity"):
        Policy.load(copy)

    # re-pinning is an explicit operator action, after which it loads again
    write_integrity(copy)
    assert Policy.load(copy).policy_version == 1


def test_policy_report_records_digest(fixture_repo):
    patch = make_patch(fixture_repo, {"README.md": "# fixture app\nx\n"})
    report = run(proposal("docs-3", patch, category="docs"), fixture_repo)
    assert report["policy"]["digest"]
    assert report["baseline"]["passed"] == 1


# ----------------------------------------------------------- static scan
def test_repo_scan_runs_clean_on_max_source():
    from max.improve import scan_repo

    repo_root = Path(__file__).resolve().parents[1]
    findings = scan_repo(repo_root, subdirs=("src",))
    blocking = [f for f in findings if f.severity == "error"]
    assert blocking == [], f"blocking findings: {blocking}"
