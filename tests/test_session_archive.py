"""Local archive privacy and session boundary checks."""

from pathlib import Path

import pytest

from max_core.session_archive import SessionArchive


def test_archive_search_is_literal_bounded_and_keeps_session_boundaries(tmp_path: Path) -> None:
    archive = SessionArchive(tmp_path)
    first = archive.session_id
    archive.append("Discuss 100% planning", "Plan ready")
    archive.new_session()
    archive.append("Other topic", "No match")
    assert archive.session_id != first
    assert archive.search("100%")[0]["session_id"] == first
    assert archive.search("100%")[0]["excerpt"] == "Discuss 100% planning"
    assert archive.search("100_") == []
    assert archive.search("missing") == []
    with pytest.raises(ValueError):
        archive.search(" ")
    assert archive.path.stat().st_mode & 0o077 == 0


def test_archive_refuses_symlink(tmp_path: Path) -> None:
    (tmp_path / "sessions.sqlite3").symlink_to(tmp_path / "target")
    with pytest.raises(ValueError, match="symlink"):
        SessionArchive(tmp_path)
