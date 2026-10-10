"""Configuration loading and defaults."""

from __future__ import annotations

import os
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .schema import EXIT_CODES


@dataclass(frozen=True)
class AccountConfig:
    name: str
    agy_bin: str = "agy"
    brain_dir: str = ""
    low_quota_threshold: float | None = None

    def resolved_brain_dir(self) -> Path:
        if self.brain_dir:
            return Path(self.brain_dir).expanduser().resolve()
        return Path.home() / ".gemini" / "antigravity-cli" / "brain"

    def resolved_agy_bin(self) -> str:
        name = self.agy_bin
        if os.path.isabs(name) or "/" in name or "\\" in name:
            return name
        return shutil.which(name) or name


@dataclass(frozen=True)
class Config:
    default_model: str = "gemini-3.7-flash"
    default_effort: str = "medium"
    default_timeout: str = "30m"
    max_concurrent: int = 3
    max_retries: int = 1
    queue_timeout: str = "2h"
    agy_bin: str = "agy"
    accounts: tuple[AccountConfig, ...] = ()
    low_quota_threshold: float = 20.0

    def timeout_seconds(self) -> int:
        return parse_timeout(self.default_timeout)

    def get_account(self, name: str) -> AccountConfig | None:
        for acc in self.accounts:
            if acc.name == name:
                return acc
        return None

    def default_account(self) -> AccountConfig:
        if self.accounts:
            return self.accounts[0]
        return AccountConfig(
            name="default",
            agy_bin=self.agy_bin,
            brain_dir=str(Path.home() / ".gemini" / "antigravity-cli" / "brain"),
            low_quota_threshold=self.low_quota_threshold,
        )


DEFAULT_CONFIG_PATH = Path.home() / ".config" / "sub-agy" / "config.toml"


_RE_DURATION = re.compile(r"^(\d+)(s|m|h)$")


def parse_timeout(value: str) -> int:
    """Parse Go-style duration string: Ns/Nm/Nh."""
    value = value.strip()
    match = _RE_DURATION.match(value)
    if not match:
        raise ValueError(f"invalid timeout duration: {value!r}")
    amount, unit = int(match.group(1)), match.group(2)
    if unit == "s":
        return amount
    if unit == "m":
        return amount * 60
    if unit == "h":
        return amount * 3600
    raise ValueError(f"invalid timeout unit: {value!r}")


def load_config(path: Path | None = None) -> Config:
    if path is None:
        env_path = os.environ.get("SUB_AGY_CONFIG")
        if env_path:
            path = Path(env_path)
        else:
            path = DEFAULT_CONFIG_PATH
    if not path.exists():
        return Config()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"invalid config file {path}: {exc}") from exc

    kwargs: dict[str, object] = {}
    if "default_model" in data:
        kwargs["default_model"] = str(data["default_model"])
    if "default_effort" in data:
        kwargs["default_effort"] = str(data["default_effort"])
    if "default_timeout" in data:
        kwargs["default_timeout"] = str(data["default_timeout"])
    if "max_concurrent" in data:
        kwargs["max_concurrent"] = int(data["max_concurrent"])
    if "max_retries" in data:
        kwargs["max_retries"] = int(data["max_retries"])
    if "queue_timeout" in data:
        kwargs["queue_timeout"] = str(data["queue_timeout"])
    if "agy_bin" in data:
        kwargs["agy_bin"] = str(data["agy_bin"])
    if "low_quota_threshold" in data:
        val = data["low_quota_threshold"]
        if isinstance(val, str) and val.strip().endswith("%"):
            kwargs["low_quota_threshold"] = float(val.strip()[:-1])
        else:
            kwargs["low_quota_threshold"] = float(val)

    accounts_list: list[AccountConfig] = []
    if "accounts" in data:
        raw_accounts = data["accounts"]
        if isinstance(raw_accounts, list):
            for item in raw_accounts:
                if isinstance(item, dict):
                    name = str(item.get("name", "")).strip()
                    acc_agy_bin = str(item.get("agy_bin", "agy")).strip()
                    brain_dir = str(item.get("brain_dir", "")).strip()
                    thresh = item.get("low_quota_threshold")
                    thresh_val = None
                    if thresh is not None:
                        if isinstance(thresh, str) and thresh.strip().endswith("%"):
                            thresh_val = float(thresh.strip()[:-1])
                        else:
                            thresh_val = float(thresh)
                    if name:
                        accounts_list.append(
                            AccountConfig(
                                name=name,
                                agy_bin=acc_agy_bin,
                                brain_dir=brain_dir,
                                low_quota_threshold=thresh_val,
                            )
                        )
        elif isinstance(raw_accounts, dict):
            for name_key, item in raw_accounts.items():
                if isinstance(item, dict):
                    name = str(item.get("name") or name_key).strip()
                    acc_agy_bin = str(item.get("agy_bin", "agy")).strip()
                    brain_dir = str(item.get("brain_dir", "")).strip()
                    thresh = item.get("low_quota_threshold")
                    thresh_val = None
                    if thresh is not None:
                        if isinstance(thresh, str) and thresh.strip().endswith("%"):
                            thresh_val = float(thresh.strip()[:-1])
                        else:
                            thresh_val = float(thresh)
                    if name:
                        accounts_list.append(
                            AccountConfig(
                                name=name,
                                agy_bin=acc_agy_bin,
                                brain_dir=brain_dir,
                                low_quota_threshold=thresh_val,
                            )
                        )
    if accounts_list:
        kwargs["accounts"] = tuple(accounts_list)

    return Config(**kwargs)


def agy_bin_path(config: Config | AccountConfig | str) -> str:
    """Resolve the configured agy binary; a bare name is looked up on PATH.

    The lookup goes through ``shutil.which`` because it honours PATHEXT: the
    file on disk under Windows is ``agy.exe`` or, for an npm install, ``agy.cmd``
    -- neither of which a probe for a bare ``agy`` finds, and the ``.cmd`` is not
    something Popen could start from a bare name anyway, since CreateProcess
    appends only ``.exe``.  A name that resolves to nothing comes back unchanged,
    leaving callers to report it as missing.
    """
    if isinstance(config, str):
        name = config
    elif isinstance(config, AccountConfig):
        name = config.agy_bin
    else:
        name = config.agy_bin
    if os.path.isabs(name) or "/" in name or "\\" in name:
        return name
    return shutil.which(name) or name


def agy_command(config: Config | AccountConfig | str) -> list[str]:
    """Argv prefix that launches agy on any platform.

    A ``.py`` path cannot be exec'd directly on Windows, as CreateProcess has no
    notion of an interpreter to hand it to, so run it through the current one.
    Real agy binaries take the plain path unchanged, ``.cmd`` and ``.bat`` shims
    included: CreateProcess recognises those extensions and delegates them to
    cmd.exe.
    """
    path = agy_bin_path(config)
    if path.lower().endswith(".py"):
        return [sys.executable, path]
    return [path]
