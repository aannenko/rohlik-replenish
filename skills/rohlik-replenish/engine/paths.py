"""Data directory, auth.json and config.toml (spec §3.1, §11)."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .constants import APP_NAME, DEFAULT_STOCKUP_BUDGET_CZK, DEFAULT_STOCKUP_MIN_DISCOUNT

DATA_DIR_ENV = "ROHLIK_REPLENISH_DATA_DIR"  # test-only override of the platformdirs location


def data_dir() -> Path:
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return Path(override)
    import platformdirs

    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    stockup_min_discount: float = DEFAULT_STOCKUP_MIN_DISCOUNT
    stockup_budget_czk: float = DEFAULT_STOCKUP_BUDGET_CZK
    warnings: tuple[str, ...] = ()


def load_config(directory: Path) -> Config:
    path = directory / "config.toml"
    if not path.exists():
        return Config()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"config.toml is not valid TOML: {exc}") from exc
    warnings = []
    known = {"stockup_min_discount", "stockup_budget_czk"}
    for key in sorted(set(raw) - known):
        warnings.append(f"config.toml: unknown key '{key}' ignored")
    disc = raw.get("stockup_min_discount", DEFAULT_STOCKUP_MIN_DISCOUNT)
    budget = raw.get("stockup_budget_czk", DEFAULT_STOCKUP_BUDGET_CZK)
    if not isinstance(disc, (int, float)) or isinstance(disc, bool) or not 0 < disc < 1:
        raise ConfigError("config.toml: stockup_min_discount must be a number between 0 and 1, e.g. 0.35")
    if not isinstance(budget, (int, float)) or isinstance(budget, bool) or budget < 0:
        raise ConfigError("config.toml: stockup_budget_czk must be a number >= 0, e.g. 1000")
    return Config(float(disc), float(budget), tuple(warnings))


class AuthFile:
    """auth.json: the OAuth client registration, the public server metadata discovered at login (so a new process
    can refresh), whether a refresh token was issued and the last account ID. Never secrets (those live in
    the OS keyring)."""

    def __init__(self, directory: Path):
        self.path = directory / "auth.json"

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return {}

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def update(self, **values) -> dict:
        data = self.load()
        for key, value in values.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        self.save(data)
        return data


def account_db_path(directory: Path, account_id: str) -> Path:
    safe = "".join(ch for ch in str(account_id) if ch.isalnum() or ch in "-_")
    if not safe:
        raise ValueError("empty account id")
    return directory / safe / "replenish.db"
