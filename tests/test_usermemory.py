"""User-scoped memory: isolation, caps, secret refusal, deletion scope."""

import pytest

from max.core.usermemory import MAX_ENTRIES, MAX_VALUE_CHARS, MemoryError_, UserMemoryStore


@pytest.fixture
def store(tmp_path):
    return UserMemoryStore(tmp_path, scope="user")


def test_set_show_forget_clear_lifecycle(store):
    store.set(1, "timezone", "Europe/Berlin")
    store.set(1, "editor", "neovim")
    assert "timezone: Europe/Berlin" in store.show(1)
    assert store.entries(1) == {"timezone": "Europe/Berlin", "editor": "neovim"}
    assert "forgot 'editor'" in store.forget(1, "editor")
    assert "editor" not in store.entries(1)
    assert "deleted" in store.clear(1)
    assert store.show(1) == "(no saved memories)"


def test_users_are_fully_isolated(store):
    store.set(1, "project", "MAX")
    store.set(2, "project", "other-repo")
    assert store.entries(1)["project"] == "MAX"
    assert store.entries(2)["project"] == "other-repo"
    # forgetting user 2's key leaves user 1 untouched
    store.forget(2, "project")
    assert store.entries(1)["project"] == "MAX"


def test_secret_values_are_refused(store):
    for value in (
        "sk-abcdefghijklmnop123456",
        "password: hunter2hunter2",
        "api_key=AKIAABCDEFGH123456",
        "-----BEGIN RSA PRIVATE KEY-----\nabc",
    ):
        with pytest.raises(MemoryError_, match="credential|secret"):
            store.set(1, "k", value)
    assert store.entries(1) == {}


def test_invalid_keys_and_empty_values_refused(store):
    with pytest.raises(MemoryError_):
        store.set(1, "Bad Key!", "value")
    with pytest.raises(MemoryError_):
        store.set(1, "key", "   ")
    with pytest.raises(MemoryError_):
        store.set(1, "key", "x" * (MAX_VALUE_CHARS + 1))


def test_entry_and_size_caps(store):
    for i in range(MAX_ENTRIES):
        store.set(1, f"key{i}", "v")
    with pytest.raises(MemoryError_, match="memory limit"):
        store.set(1, "one-more", "v")


def test_forget_unknown_key_and_clear_missing(store):
    with pytest.raises(MemoryError_):
        store.forget(1, "nope")
    assert "nothing to delete" in store.clear(4242)


def test_meta_keys_are_not_user_visible(store):
    store.set(7, "k", "v")
    assert all(not key.startswith("_") for key in store.entries(7))
    assert "k: v" in store.show(7)
    assert "_updated" not in store.show(7)


def test_project_scope_is_separate_directory(tmp_path):
    user = UserMemoryStore(tmp_path, scope="user")
    project = UserMemoryStore(tmp_path, scope="project")
    user.set(1, "k", "personal")
    project.set(1, "k", "project-fact")
    assert user.entries(1)["k"] == "personal"
    assert project.entries(1)["k"] == "project-fact"


def test_corrupt_file_reads_as_empty(store, tmp_path):
    store.set(1, "k", "v")
    path = store._path(1)
    path.write_text("{broken", encoding="utf-8")
    assert store.entries(1) == {}