"""The evaluation suite itself: green, reproducible, and gate-aware."""

from pathlib import Path

from max.eval import Harness

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_suite_has_expected_scenarios():
    harness = Harness(repo_root=REPO_ROOT)
    ids = [p.stem for p in harness.discover()]
    assert len(ids) == 14, ids
    # required coverage from the Stage 2 brief
    joined = " ".join(ids)
    for topic in (
        "conversation",
        "context-retention",
        "persistent-storage",
        "skill-discovery",
        "skill-loading",
        "tool-execution",
        "mcp-integration",
        "code-regression",
        "provider-failure",
        "restart-recovery",
        "unauthorized-tool-rejection",
        "prompt-injection",
        "improvement-promoted",
    ):
        assert topic in joined, f"missing scenario topic: {topic}"


def test_full_suite_is_green(tmp_path):
    harness = Harness(repo_root=REPO_ROOT, work_root=tmp_path / "work")
    summary = harness.run_all(label="pytest")
    failures = [
        (r["id"], r["error"], [c for c in r["checks"] if not c["passed"]])
        for r in summary["results"]
        if r["passed"] is False
    ]
    assert failures == [], failures
    assert summary["failed"] == 0
    assert summary["passed"] == 14
    assert summary["pass_rate"] == 1.0


def test_suite_is_reproducible(tmp_path):
    harness = Harness(repo_root=REPO_ROOT, work_root=tmp_path / "work")
    first = harness.run_all(label="rep1")
    second = harness.run_all(label="rep2")
    assert first["pass_rate"] == second["pass_rate"]
    assert [r["id"] for r in first["results"]] == [r["id"] for r in second["results"]]
    assert [r["passed"] for r in first["results"]] == [r["passed"] for r in second["results"]]
    assert Path(first["out_path"]).is_file()
    assert Path(second["out_path"]).is_file()


def test_subsystem_gate_skips_cleanly(tmp_path, monkeypatch):
    """With skills disabled the skill scenarios skip instead of failing."""
    monkeypatch.setenv("MAX_ENABLE_SKILLS", "0")
    monkeypatch.setenv("MAX_ENABLE_IMPROVE", "0")
    monkeypatch.setenv("MAX_ENABLE_MCP", "0")
    harness = Harness(repo_root=REPO_ROOT, work_root=tmp_path / "work")
    summary = harness.run_all(label="gated")
    assert summary["skipped"] == 6
    assert summary["executed"] == 8
    assert summary["failed"] == 0
    skipped = {r["id"] for r in summary["results"] if r.get("skipped")}
    assert "09-skill-discovery" in skipped
    assert "12-mcp-integration" in skipped
    assert "14-improvement-promoted" in skipped
    assert "01-conversation-basic" not in skipped
