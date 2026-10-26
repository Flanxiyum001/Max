"""Discord configuration: env parsing, privacy defaults, admin logic."""

from max.discord import DiscordConfig


def make(tmp_path, monkeypatch, **env):
    for key in list(__import__("os").environ):
        if key.startswith("MAX_DISCORD_"):
            monkeypatch.delenv(key, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cfg = DiscordConfig.from_env(repo_root=tmp_path)
    return cfg


def test_defaults_are_off_and_safe(tmp_path, monkeypatch):
    cfg = make(tmp_path, monkeypatch)
    assert cfg.enable is False
    assert cfg.token == ""
    assert cfg.dm_mode == "off"
    assert cfg.allowed_channels == set()
    assert cfg.response_mode == "all"
    assert cfg.command_prefix == "!max"


def test_invalid_values_fall_back_to_safe_modes(tmp_path, monkeypatch):
    cfg = make(
        tmp_path, monkeypatch,
        MAX_DISCORD_DM_MODE="everyone-please",
        MAX_DISCORD_RESPONSE_MODE="yolo",
    )
    assert cfg.dm_mode == "off"       # invalid dm mode → closed
    assert cfg.response_mode == "all"  # invalid → documented default


def test_id_parsing_handles_mentions_and_spaces(tmp_path, monkeypatch):
    cfg = make(
        tmp_path, monkeypatch,
        MAX_DISCORD_ALLOWED_CHANNELS="111, <#222>, 333",
        MAX_DISCORD_ADMINS="<@!444>",
        MAX_DISCORD_ADMIN_ROLES="555",
        MAX_DISCORD_GUILD_IDS="777",
    )
    assert cfg.allowed_channels == {111, 222, 333}
    assert cfg.admin_users == {444}
    assert cfg.admin_roles == {555}
    assert cfg.allowed_guilds == {777}


def test_token_never_appears_in_redacted_output(tmp_path, monkeypatch):
    cfg = make(tmp_path, monkeypatch, MAX_DISCORD_TOKEN="super-secret-bot-token")
    red = cfg.redacted()
    assert red["token_set"] is True
    assert "super-secret-bot-token" not in repr(red)
    assert "token" not in [k for k, v in red.items() if v == "super-secret-bot-token"]


def test_admin_check_by_user_or_role(tmp_path, monkeypatch):
    cfg = make(tmp_path, monkeypatch, MAX_DISCORD_ADMINS="1", MAX_DISCORD_ADMIN_ROLES="9")
    assert cfg.is_admin(1) is True
    assert cfg.is_admin(2, role_ids={9}) is True
    assert cfg.is_admin(2, role_ids={10}) is False
    # role check only applies when admin_roles configured
    cfg2 = make(tmp_path, monkeypatch, MAX_DISCORD_ADMINS="1")
    assert cfg2.is_admin(2, role_ids={9}) is False


def test_guild_allowlist(tmp_path, monkeypatch):
    cfg = make(tmp_path, monkeypatch, MAX_DISCORD_GUILD_IDS="7")
    assert cfg.guild_allowed(7) is True
    assert cfg.guild_allowed(8) is False
    assert cfg.guild_allowed(None) is False
    open_cfg = make(tmp_path, monkeypatch)
    assert open_cfg.guild_allowed(123) is True  # any guild, channels still gated