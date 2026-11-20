"""Pure-stdlib local desktop configuration loader for MAX.

Reads configuration with precedence ``env > config_file > .env > defaults``.
Missing API keys or malformed optional config files degrade cleanly without raising.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

_DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
_DEFAULT_OPENAI_ENDPOINT = "https://api.openai.com/v1/chat/completions"
_DEFAULT_OPENAI_INPUT_USD_PER_MILLION = 0.15
_DEFAULT_OPENAI_OUTPUT_USD_PER_MILLION = 0.60

_DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5"
_DEFAULT_ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1/messages"
_DEFAULT_ANTHROPIC_INPUT_USD_PER_MILLION = 3.00
_DEFAULT_ANTHROPIC_OUTPUT_USD_PER_MILLION = 15.00

_DEFAULT_MAX_COST_USD = 0.05
_DEFAULT_VOICE_LOCALE = "en-GB"
_DEFAULT_VOICE_PRESENTATION = "feminine"

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INLINE_COMMENT_RE = re.compile(r"\s+#")
_LOCALE_RE = re.compile(r"^[a-z]{2,3}(?:-[A-Z]{2})?$")

_TRUTHY_STRINGS = frozenset({"1", "true", "yes", "on"})
_FALSY_STRINGS = frozenset({"0", "false", "no", "off"})

_DOUBLE_QUOTE_ESCAPES: dict[str, str] = {
    '"': '"',
    "\\": "\\",
    "n": "\n",
    "r": "\r",
    "t": "\t",
}


@dataclass(frozen=True, slots=True, repr=False)
class ProviderConfig:
    """Configuration for a single cloud LLM provider (secrets excluded from repr)."""

    provider: Literal["openai", "anthropic"]
    model_id: str
    endpoint: str
    api_key: str | None = field(default=None, repr=False)
    input_usd_per_million: float = 0.0
    output_usd_per_million: float = 0.0
    max_cost_usd: float = _DEFAULT_MAX_COST_USD


@dataclass(frozen=True, slots=True, repr=False)
class DesktopConfig:
    """Resolved local desktop configuration (secrets and DB URL excluded from repr)."""

    database_url: str
    is_sqlite_local: bool
    workspace_root: Path
    notes_dir: Path
    active_provider: ProviderConfig | None
    openai: ProviderConfig | None
    anthropic: ProviderConfig | None
    voice_locale: str = _DEFAULT_VOICE_LOCALE
    voice_presentation: str = _DEFAULT_VOICE_PRESENTATION
    tts_enabled_default: bool = False


def _parse_double_quoted(raw_val: str) -> str:
    chars: list[str] = []
    idx = 1
    length = len(raw_val)
    while idx < length:
        ch = raw_val[idx]
        if ch == '"':
            return "".join(chars)
        if ch == "\\" and idx + 1 < length:
            nxt = raw_val[idx + 1]
            if nxt in _DOUBLE_QUOTE_ESCAPES:
                chars.append(_DOUBLE_QUOTE_ESCAPES[nxt])
            else:
                chars.append("\\")
                chars.append(nxt)
            idx += 2
            continue
        chars.append(ch)
        idx += 1
    return raw_val[1:].rstrip()


def _parse_single_quoted(raw_val: str) -> str:
    end_idx = raw_val.find("'", 1)
    if end_idx != -1:
        return raw_val[1:end_idx]
    return raw_val[1:].rstrip()


def _parse_dotenv_value(raw_val: str) -> str:
    val = raw_val.lstrip()
    if not val:
        return ""
    if val.startswith('"'):
        return _parse_double_quoted(val)
    if val.startswith("'"):
        return _parse_single_quoted(val)
    if val.startswith("#"):
        return ""
    return _INLINE_COMMENT_RE.split(val, maxsplit=1)[0].strip()


def load_dotenv_into_mapping(dotenv_path: Path) -> dict[str, str]:
    """Parse a ``.env`` file into a string mapping; missing/unreadable files return ``{}``."""
    try:
        path = Path(dotenv_path)
        if not path.is_file():
            return {}
        content = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return {}

    result: dict[str, str] = {}
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export") and len(stripped) > 6 and stripped[6].isspace():
            stripped = stripped[6:].lstrip()
        if "=" not in stripped:
            continue
        raw_key, raw_val = stripped.split("=", 1)
        key = raw_key.strip()
        if not _ENV_KEY_RE.fullmatch(key):
            continue
        result[key] = _parse_dotenv_value(raw_val)
    return result


def _load_json_config(config_path: Path) -> dict[str, Any]:
    try:
        if not config_path.is_file():
            return {}
        raw = config_path.read_text(encoding="utf-8-sig")
        parsed: object = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def _walk_json(json_cfg: Mapping[str, Any], path: tuple[str, ...]) -> object:
    current: object = json_cfg
    for segment in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(segment)
    return current


def _lookup_env_only_str(
    keys: tuple[str, ...],
    env_map: Mapping[str, str],
    dotenv_map: Mapping[str, str],
) -> str | None:
    for key in keys:
        val = env_map.get(key)
        if isinstance(val, str):
            trimmed = val.strip()
            if trimmed:
                return trimmed
    for key in keys:
        val = dotenv_map.get(key)
        if isinstance(val, str):
            trimmed = val.strip()
            if trimmed:
                return trimmed
    return None


def _lookup_str(
    env_keys: tuple[str, ...],
    json_paths: tuple[tuple[str, ...], ...],
    *,
    env_map: Mapping[str, str],
    json_cfg: Mapping[str, Any],
    dotenv_map: Mapping[str, str],
) -> str | None:
    for key in env_keys:
        val = env_map.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    for path in json_paths:
        leaf = _walk_json(json_cfg, path)
        if isinstance(leaf, str) and leaf.strip():
            return leaf.strip()
    for key in env_keys:
        val = dotenv_map.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _lookup_api_key(
    env_keys: tuple[str, ...],
    json_paths: tuple[tuple[str, ...], ...],
    *,
    env_map: Mapping[str, str],
    json_cfg: Mapping[str, Any],
    dotenv_map: Mapping[str, str],
) -> str | None:
    def _clean_secret(candidate: object) -> str | None:
        if not isinstance(candidate, str):
            return None
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in candidate):
            return None
        trimmed = candidate.strip()
        return trimmed if trimmed else None

    for key in env_keys:
        if key in env_map:
            raw = env_map.get(key)
            if isinstance(raw, str) and raw != "":
                return _clean_secret(raw)
    for path in json_paths:
        leaf = _walk_json(json_cfg, path)
        if isinstance(leaf, str) and leaf != "":
            return _clean_secret(leaf)
    for key in env_keys:
        if key in dotenv_map:
            raw = dotenv_map.get(key)
            if isinstance(raw, str) and raw != "":
                return _clean_secret(raw)
    return None


def _parse_non_negative_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            num = float(text)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(num) or num < 0.0:
        return None
    return num


def _lookup_float(
    env_keys: tuple[str, ...],
    json_paths: tuple[tuple[str, ...], ...],
    default: float,
    *,
    env_map: Mapping[str, str],
    json_cfg: Mapping[str, Any],
    dotenv_map: Mapping[str, str],
) -> float:
    for key in env_keys:
        raw = env_map.get(key)
        if isinstance(raw, str) and raw.strip():
            parsed = _parse_non_negative_float(raw)
            return parsed if parsed is not None else default
    for path in json_paths:
        leaf = _walk_json(json_cfg, path)
        if leaf is not None and not (isinstance(leaf, str) and not leaf.strip()):
            parsed = _parse_non_negative_float(leaf)
            return parsed if parsed is not None else default
    for key in env_keys:
        raw = dotenv_map.get(key)
        if isinstance(raw, str) and raw.strip():
            parsed = _parse_non_negative_float(raw)
            return parsed if parsed is not None else default
    return default


def _parse_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUTHY_STRINGS:
            return True
        if lowered in _FALSY_STRINGS:
            return False
    return None


def _lookup_bool(
    env_keys: tuple[str, ...],
    json_paths: tuple[tuple[str, ...], ...],
    default: bool,
    *,
    env_map: Mapping[str, str],
    json_cfg: Mapping[str, Any],
    dotenv_map: Mapping[str, str],
) -> bool:
    for key in env_keys:
        raw = env_map.get(key)
        if isinstance(raw, str) and raw.strip():
            parsed = _parse_bool(raw)
            return parsed if parsed is not None else default
    for path in json_paths:
        leaf = _walk_json(json_cfg, path)
        if leaf is not None and not (isinstance(leaf, str) and not leaf.strip()):
            parsed = _parse_bool(leaf)
            return parsed if parsed is not None else default
    for key in env_keys:
        raw = dotenv_map.get(key)
        if isinstance(raw, str) and raw.strip():
            parsed = _parse_bool(raw)
            return parsed if parsed is not None else default
    return default


def _is_windows() -> bool:
    return os.name == "nt" or sys.platform == "win32"


def _default_data_dir(
    env_map: Mapping[str, str],
    dotenv_map: Mapping[str, str],
) -> Path:
    if _is_windows():
        local_app_data = _lookup_env_only_str(("LOCALAPPDATA",), env_map, dotenv_map)
        if local_app_data is not None:
            return Path(local_app_data).expanduser() / "MAX"
        return Path.home() / "AppData" / "Local" / "MAX"
    xdg_data_home = _lookup_env_only_str(("XDG_DATA_HOME",), env_map, dotenv_map)
    if xdg_data_home is not None:
        xdg_path = Path(xdg_data_home).expanduser()
        if xdg_path.is_absolute():
            return xdg_path / "max"
    return Path.home() / ".local" / "share" / "max"


def _db_from_mapping(
    source: Mapping[str, str],
) -> tuple[str, bool] | None:
    for key in ("MAX_DATABASE_URL", "MAX_DESKTOP_DB_URL"):
        val = source.get(key)
        if isinstance(val, str) and val.strip():
            cleaned = val.strip()
            return cleaned, cleaned.startswith("sqlite")
    local_path = source.get("MAX_LOCAL_DB_PATH")
    if isinstance(local_path, str) and local_path.strip():
        db_path = Path(local_path.strip()).expanduser().resolve()
        return f"sqlite+pysqlite:///{db_path.as_posix()}", True
    return None


def _db_from_json(json_cfg: Mapping[str, Any]) -> tuple[str, bool] | None:
    for key in ("database_url", "desktop_db_url"):
        val = json_cfg.get(key)
        if isinstance(val, str) and val.strip():
            cleaned = val.strip()
            return cleaned, cleaned.startswith("sqlite")
    local_path = json_cfg.get("local_db_path")
    if isinstance(local_path, str) and local_path.strip():
        db_path = Path(local_path.strip()).expanduser().resolve()
        return f"sqlite+pysqlite:///{db_path.as_posix()}", True
    return None


def _resolve_database_url(
    resolved_data_dir: Path,
    *,
    env_map: Mapping[str, str],
    json_cfg: Mapping[str, Any],
    dotenv_map: Mapping[str, str],
) -> tuple[str, bool]:
    for resolved in (
        _db_from_mapping(env_map),
        _db_from_json(json_cfg),
        _db_from_mapping(dotenv_map),
    ):
        if resolved is not None:
            return resolved

    default_db_path = (resolved_data_dir / "max_desktop.sqlite3").resolve()
    return f"sqlite+pysqlite:///{default_db_path.as_posix()}", True


def _build_provider(
    provider: Literal["openai", "anthropic"],
    *,
    env_map: Mapping[str, str],
    json_cfg: Mapping[str, Any],
    dotenv_map: Mapping[str, str],
) -> ProviderConfig | None:
    if provider == "openai":
        api_key = _lookup_api_key(
            ("OPENAI_API_KEY",),
            (("providers", "openai", "api_key"), ("openai", "api_key"), ("openai_api_key",)),
            env_map=env_map,
            json_cfg=json_cfg,
            dotenv_map=dotenv_map,
        )
        if api_key is None:
            return None
        model_id = (
            _lookup_str(
                ("MAX_OPENAI_MODEL", "OPENAI_MODEL"),
                (
                    ("providers", "openai", "model_id"),
                    ("providers", "openai", "model"),
                    ("openai", "model_id"),
                    ("openai", "model"),
                    ("openai_model",),
                ),
                env_map=env_map,
                json_cfg=json_cfg,
                dotenv_map=dotenv_map,
            )
            or _DEFAULT_OPENAI_MODEL
        )
        endpoint = (
            _lookup_str(
                ("MAX_OPENAI_ENDPOINT", "OPENAI_ENDPOINT"),
                (
                    ("providers", "openai", "endpoint"),
                    ("openai", "endpoint"),
                    ("openai_endpoint",),
                ),
                env_map=env_map,
                json_cfg=json_cfg,
                dotenv_map=dotenv_map,
            )
            or _DEFAULT_OPENAI_ENDPOINT
        )
        input_rate = _lookup_float(
            ("MAX_OPENAI_INPUT_USD_PER_MILLION",),
            (
                ("providers", "openai", "input_usd_per_million"),
                ("openai", "input_usd_per_million"),
            ),
            _DEFAULT_OPENAI_INPUT_USD_PER_MILLION,
            env_map=env_map,
            json_cfg=json_cfg,
            dotenv_map=dotenv_map,
        )
        output_rate = _lookup_float(
            ("MAX_OPENAI_OUTPUT_USD_PER_MILLION",),
            (
                ("providers", "openai", "output_usd_per_million"),
                ("openai", "output_usd_per_million"),
            ),
            _DEFAULT_OPENAI_OUTPUT_USD_PER_MILLION,
            env_map=env_map,
            json_cfg=json_cfg,
            dotenv_map=dotenv_map,
        )
        max_cost = _lookup_float(
            ("MAX_OPENAI_MAX_COST_USD", "MAX_COST_USD"),
            (
                ("providers", "openai", "max_cost_usd"),
                ("openai", "max_cost_usd"),
                ("max_cost_usd",),
            ),
            _DEFAULT_MAX_COST_USD,
            env_map=env_map,
            json_cfg=json_cfg,
            dotenv_map=dotenv_map,
        )
        return ProviderConfig(
            provider="openai",
            model_id=model_id,
            endpoint=endpoint,
            api_key=api_key,
            input_usd_per_million=input_rate,
            output_usd_per_million=output_rate,
            max_cost_usd=max_cost,
        )

    api_key = _lookup_api_key(
        ("ANTHROPIC_API_KEY",),
        (
            ("providers", "anthropic", "api_key"),
            ("anthropic", "api_key"),
            ("anthropic_api_key",),
        ),
        env_map=env_map,
        json_cfg=json_cfg,
        dotenv_map=dotenv_map,
    )
    if api_key is None:
        return None
    model_id = (
        _lookup_str(
            ("MAX_ANTHROPIC_MODEL", "ANTHROPIC_MODEL"),
            (
                ("providers", "anthropic", "model_id"),
                ("providers", "anthropic", "model"),
                ("anthropic", "model_id"),
                ("anthropic", "model"),
                ("anthropic_model",),
            ),
            env_map=env_map,
            json_cfg=json_cfg,
            dotenv_map=dotenv_map,
        )
        or _DEFAULT_ANTHROPIC_MODEL
    )
    endpoint = (
        _lookup_str(
            ("MAX_ANTHROPIC_ENDPOINT", "ANTHROPIC_ENDPOINT"),
            (
                ("providers", "anthropic", "endpoint"),
                ("anthropic", "endpoint"),
                ("anthropic_endpoint",),
            ),
            env_map=env_map,
            json_cfg=json_cfg,
            dotenv_map=dotenv_map,
        )
        or _DEFAULT_ANTHROPIC_ENDPOINT
    )
    input_rate = _lookup_float(
        ("MAX_ANTHROPIC_INPUT_USD_PER_MILLION",),
        (
            ("providers", "anthropic", "input_usd_per_million"),
            ("anthropic", "input_usd_per_million"),
        ),
        _DEFAULT_ANTHROPIC_INPUT_USD_PER_MILLION,
        env_map=env_map,
        json_cfg=json_cfg,
        dotenv_map=dotenv_map,
    )
    output_rate = _lookup_float(
        ("MAX_ANTHROPIC_OUTPUT_USD_PER_MILLION",),
        (
            ("providers", "anthropic", "output_usd_per_million"),
            ("anthropic", "output_usd_per_million"),
        ),
        _DEFAULT_ANTHROPIC_OUTPUT_USD_PER_MILLION,
        env_map=env_map,
        json_cfg=json_cfg,
        dotenv_map=dotenv_map,
    )
    max_cost = _lookup_float(
        ("MAX_ANTHROPIC_MAX_COST_USD", "MAX_COST_USD"),
        (
            ("providers", "anthropic", "max_cost_usd"),
            ("anthropic", "max_cost_usd"),
            ("max_cost_usd",),
        ),
        _DEFAULT_MAX_COST_USD,
        env_map=env_map,
        json_cfg=json_cfg,
        dotenv_map=dotenv_map,
    )
    return ProviderConfig(
        provider="anthropic",
        model_id=model_id,
        endpoint=endpoint,
        api_key=api_key,
        input_usd_per_million=input_rate,
        output_usd_per_million=output_rate,
        max_cost_usd=max_cost,
    )


def load_desktop_config(
    *,
    env: Mapping[str, str] | None = None,
    dotenv_path: Path | None = None,
    config_file: Path | None = None,
    data_dir: Path | None = None,
) -> DesktopConfig:
    """Load desktop configuration with precedence ``env > config_file > .env > defaults``.

    Never creates directories on disk and never raises when API keys are missing or
    the optional JSON config / ``.env`` file is absent or malformed.
    """
    env_map: Mapping[str, str] = os.environ if env is None else env
    if dotenv_path is not None:
        dotenv_map = load_dotenv_into_mapping(dotenv_path)
    elif env is None:
        dotenv_map = load_dotenv_into_mapping(Path.cwd() / ".env")
    else:
        dotenv_map = {}

    if data_dir is not None:
        resolved_data_dir = Path(data_dir).expanduser()
    else:
        resolved_data_dir = _default_data_dir(env_map, dotenv_map)

    if config_file is not None:
        cfg_path = Path(config_file).expanduser()
    else:
        cfg_override = _lookup_env_only_str(("MAX_CONFIG_PATH",), env_map, dotenv_map)
        cfg_path = (
            Path(cfg_override).expanduser()
            if cfg_override is not None
            else resolved_data_dir / "config.json"
        )
    json_cfg = _load_json_config(cfg_path)

    database_url, is_sqlite_local = _resolve_database_url(
        resolved_data_dir,
        env_map=env_map,
        json_cfg=json_cfg,
        dotenv_map=dotenv_map,
    )

    workspace_raw = _lookup_str(
        ("MAX_WORKSPACE_ROOT",),
        (("workspace_root",),),
        env_map=env_map,
        json_cfg=json_cfg,
        dotenv_map=dotenv_map,
    )
    workspace_root = (
        Path(workspace_raw).expanduser().resolve()
        if workspace_raw is not None
        else Path.cwd().resolve()
    )

    notes_raw = _lookup_str(
        ("MAX_NOTES_DIR",),
        (("notes_dir",),),
        env_map=env_map,
        json_cfg=json_cfg,
        dotenv_map=dotenv_map,
    )
    notes_dir = (
        Path(notes_raw).expanduser().resolve()
        if notes_raw is not None
        else (resolved_data_dir / "notes").resolve()
    )

    openai_cfg = _build_provider(
        "openai", env_map=env_map, json_cfg=json_cfg, dotenv_map=dotenv_map
    )
    anthropic_cfg = _build_provider(
        "anthropic", env_map=env_map, json_cfg=json_cfg, dotenv_map=dotenv_map
    )

    selected_provider = _lookup_str(
        ("MAX_LLM_PROVIDER",),
        (("active_provider",), ("llm_provider",)),
        env_map=env_map,
        json_cfg=json_cfg,
        dotenv_map=dotenv_map,
    )
    provider_choice = selected_provider.lower() if selected_provider is not None else "auto"
    if provider_choice == "openai":
        active_provider = openai_cfg
    elif provider_choice == "anthropic":
        active_provider = anthropic_cfg
    else:
        active_provider = openai_cfg if openai_cfg is not None else anthropic_cfg

    raw_locale = _lookup_str(
        ("MAX_VOICE_LOCALE",),
        (("voice", "locale"), ("voice_locale",)),
        env_map=env_map,
        json_cfg=json_cfg,
        dotenv_map=dotenv_map,
    )
    voice_locale = (
        raw_locale
        if raw_locale is not None and _LOCALE_RE.fullmatch(raw_locale)
        else _DEFAULT_VOICE_LOCALE
    )

    raw_presentation = _lookup_str(
        ("MAX_VOICE_PRESENTATION",),
        (("voice", "presentation"), ("voice_presentation",)),
        env_map=env_map,
        json_cfg=json_cfg,
        dotenv_map=dotenv_map,
    )
    voice_presentation = (
        raw_presentation.lower()
        if raw_presentation is not None and raw_presentation.lower() == "feminine"
        else _DEFAULT_VOICE_PRESENTATION
    )

    tts_enabled_default = _lookup_bool(
        ("MAX_TTS_ENABLED",),
        (("voice", "tts_enabled"), ("tts_enabled",), ("tts_enabled_default",)),
        False,
        env_map=env_map,
        json_cfg=json_cfg,
        dotenv_map=dotenv_map,
    )

    return DesktopConfig(
        database_url=database_url,
        is_sqlite_local=is_sqlite_local,
        workspace_root=workspace_root,
        notes_dir=notes_dir,
        active_provider=active_provider,
        openai=openai_cfg,
        anthropic=anthropic_cfg,
        voice_locale=voice_locale,
        voice_presentation=voice_presentation,
        tts_enabled_default=tts_enabled_default,
    )
