"""Tests for configuration loading and timeout parsing."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sub_agy.config import Config, load_config, parse_timeout


def test_parse_timeout_valid() -> None:
    assert parse_timeout("90s") == 90
    assert parse_timeout("30m") == 1800
    assert parse_timeout("1h") == 3600


def test_parse_timeout_invalid() -> None:
    with pytest.raises(ValueError):
        parse_timeout("5")
    with pytest.raises(ValueError):
        parse_timeout("5 minutes")
    with pytest.raises(ValueError):
        parse_timeout("1d")


def test_load_config_defaults() -> None:
    # Ensure no env var points to a real file
    os.environ.pop("SUB_AGY_CONFIG", None)
    config = load_config(Path("/nonexistent/config.toml"))
    assert config.default_model == "gemini-3.7-flash"
    assert config.default_timeout == "30m"
    assert config.max_concurrent == 3


def test_load_config_from_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'default_model = "gemini-2.0"\ndefault_timeout = "1h"\nmax_concurrent = 5\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SUB_AGY_CONFIG", str(config_path))
    config = load_config()
    assert config.default_model == "gemini-2.0"
    assert config.default_timeout == "1h"
    assert config.max_concurrent == 5


def test_config_timeout_seconds() -> None:
    config = Config(default_timeout="2m")
    assert config.timeout_seconds() == 120


def test_load_config_ignores_legacy_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "legacy-config.toml"
    config_path.write_text(
        'default_model = "gemini-2.5"\nauto_approve = true\nsandbox = false\nunknown_key = 123\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("SUB_AGY_CONFIG", str(config_path))
    config = load_config()
    assert config.default_model == "gemini-2.5"


def test_load_config_accounts_array(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "accounts-array.toml"
    config_path.write_text(
        """
low_quota_threshold = 25.0

[[accounts]]
name = "main"
agy_bin = "/path/to/agy"
brain_dir = "/path/to/brain"
low_quota_threshold = 30.0

[[accounts]]
name = "rainy"
agy_bin = "/path/to/agy-rainy"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SUB_AGY_CONFIG", str(config_path))
    config = load_config()
    assert config.low_quota_threshold == 25.0
    assert len(config.accounts) == 2

    acc1 = config.get_account("main")
    assert acc1 is not None
    assert acc1.name == "main"
    assert acc1.agy_bin == "/path/to/agy"
    assert acc1.brain_dir == "/path/to/brain"
    assert acc1.low_quota_threshold == 30.0

    acc2 = config.get_account("rainy")
    assert acc2 is not None
    assert acc2.name == "rainy"
    assert acc2.agy_bin == "/path/to/agy-rainy"
    assert acc2.brain_dir == ""
    assert acc2.low_quota_threshold is None

    assert config.get_account("unknown") is None


def test_load_config_accounts_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "accounts-table.toml"
    config_path.write_text(
        """
low_quota_threshold = "15%"

[accounts.first]
agy_bin = "agy-1"
brain_dir = "~/.brain1"

[accounts.second]
agy_bin = "agy-2"
low_quota_threshold = "10%"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SUB_AGY_CONFIG", str(config_path))
    config = load_config()
    assert config.low_quota_threshold == 15.0
    assert len(config.accounts) == 2

    first = config.get_account("first")
    assert first is not None
    assert first.agy_bin == "agy-1"

    second = config.get_account("second")
    assert second is not None
    assert second.low_quota_threshold == 10.0


def test_default_account_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "single.toml"
    config_path.write_text('agy_bin = "/custom/agy"\n', encoding="utf-8")
    monkeypatch.setenv("SUB_AGY_CONFIG", str(config_path))
    config = load_config()
    assert len(config.accounts) == 0
    default_acc = config.default_account()
    assert default_acc.name == "default"
    assert default_acc.agy_bin == "/custom/agy"
    assert "antigravity-cli" in str(default_acc.resolved_brain_dir())
