"""Channel allowlist store: overlay semantics, persistence, audit."""

import json

from max.core.tools import AuditLog
from max.discord import ChannelStore, DiscordConfig


def make_config(tmp_path, **kw):
    base = dict(
        allowed_channels={10},
        ignored_channels={99},
        admin_users={5},
        allowlist_file=tmp_path / "allowlist.json",
    )
    base.update(kw)
    return DiscordConfig(**base)


def make(tmp_path, **kw):
    audit = AuditLog(tmp_path / "audit.jsonl")
    config = make_config(tmp_path, **kw)
    return ChannelStore(config, audit), audit, config


def test_base_allowlist_and_gating(tmp_path):
    store, _, _ = make(tmp_path)
    assert store.is_allowed(10, guild_id=7) is True
    assert store.is_allowed(11, guild_id=7) is False      # not listed
    assert store.is_allowed(10, guild_id=None) is False   # no guild context


def test_ignored_channel_wins_over_everything(tmp_path):
    store, audit, _ = make(tmp_path, allowed_channels={10}, ignored_channels={10})
    assert store.is_allowed(10, guild_id=7) is False
    # admin cannot `allow` an ignored channel
    assert store.add(10, actor_id=5) is False
    events = audit.events("discord_config_change")
    assert events and events[-1]["outcome"] == "denied"


def test_guild_allowlist_blocks_even_listed_channels(tmp_path):
    store, _, _ = make(tmp_path, allowed_guilds={7})
    assert store.is_allowed(10, guild_id=7) is True
    assert store.is_allowed(10, guild_id=8) is False


def test_admin_add_remove_persists_across_reload(tmp_path):
    store, audit, config = make(tmp_path)
    assert store.add(20, actor_id=5, guild_id=7) is True
    assert store.is_allowed(20, guild_id=7) is True
    assert store.remove(10, actor_id=5, guild_id=7) is True
    assert store.is_allowed(10, guild_id=7) is False

    reloaded = ChannelStore(config, audit)
    assert reloaded.is_allowed(20, guild_id=7) is True
    assert reloaded.is_allowed(10, guild_id=7) is False
    assert 20 in reloaded.effective_allowlist()
    assert 10 not in reloaded.effective_allowlist()

    events = audit.events("discord_config_change")
    actions = [(e.get("action"), e.get("channel")) for e in events]
    assert ("allow", 20) in actions and ("deny", 10) in actions
    # actor recorded
    assert all(e.get("actor") == 5 for e in events if e.get("action"))


def test_add_is_idempotent_and_file_is_valid_json(tmp_path):
    store, _, _ = make(tmp_path)
    store.add(30, actor_id=5)
    store.add(30, actor_id=5)
    data = json.loads((tmp_path / "allowlist.json").read_text())
    assert list(data["added"]) == ["30"]


def test_corrupt_state_file_fails_open_to_env_baseline(tmp_path):
    (tmp_path / "allowlist.json").write_text("{not json", encoding="utf-8")
    store, _, _ = make(tmp_path)
    assert store.is_allowed(10, guild_id=7) is True   # env baseline still works
    assert store.is_allowed(999, guild_id=7) is False  # and nothing is granted

def test_wake_state_persists_and_is_erased_by_channel_denial(tmp_path):
    store, audit, cfg = make(tmp_path)
    assert store.set_awake(10, 7, 5, True)
    assert store.is_awake(10, 7)
    reloaded = ChannelStore(cfg, audit)
    assert reloaded.is_awake(10, 7)
    assert not reloaded.is_awake(10, 999)
    assert not reloaded.set_awake(11, 7, 5, True)

    assert reloaded.remove(10, 5, guild_id=7)
    assert not reloaded.is_awake(10, 7)
    assert reloaded.add(10, 5, guild_id=7)
    assert not reloaded.is_awake(10, 7)
    assert not ChannelStore(cfg, audit).is_awake(10, 7)


def test_older_allowlist_json_without_awake_data_starts_asleep(tmp_path):
    (tmp_path / "allowlist.json").write_text(
        '{"added": {}, "removed": {}}', encoding="utf-8"
    )
    store, _, _ = make(tmp_path)
    assert store.is_allowed(10, 7)
    assert not store.is_awake(10, 7)
