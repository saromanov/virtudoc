from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DATA_DIR_ENV = "VIRTUDOC_HOME"

DEFAULTS: dict[str, dict[str, object]] = {
    "db": {"path": None},
    "fetch": {"branch": None},
    "chunk": {"target_tokens": 600, "max_tokens": 1200, "overlap_tokens": 60},
    "search": {"default_tokens": 5000, "max_results": 20},
    "notes": {"max_tokens": 800},
    "server": {
        "transport": "stdio",
        "http_host": "127.0.0.1",
        "http_port": 8765,
    },
    "log": {"level": "INFO"},
}

_FLAT_DEFAULTS = {
    f"{section}.{key}": val for section, vals in DEFAULTS.items() for key, val in vals.items()
}


def data_dir() -> Path:
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return Path(override)
    return Path.home() / ".virtudoc"


@dataclass
class Config:
    values: dict[str, object] = field(default_factory=lambda: dict(_FLAT_DEFAULTS))

    @classmethod
    def load(cls, config_dir: Path | None = None) -> Config:
        cfg = cls()
        base = config_dir if config_dir is not None else data_dir()
        path = base / "config.toml"
        if path.is_file():
            with open(path, "rb") as fh:
                raw = tomllib.load(fh)
            for section, vals in raw.items():
                if not isinstance(vals, dict):
                    continue
                for key, val in vals.items():
                    full = f"{section}.{key}"
                    if full not in _FLAT_DEFAULTS:
                        print(f"warning: unknown configuration key '{full}'", file=sys.stderr)
                        continue
                    cfg.values[full] = val
        return cfg

    def get(self, key: str) -> object:
        if key not in self.values:
            raise KeyError(f"unknown configuration key: {key}")
        return self.values[key]

    def get_int(self, key: str) -> int:
        return int(self.get(key))

    def get_str(self, key: str) -> str:
        return str(self.get(key))

    @property
    def db_path(self) -> Path:
        configured = self.values.get("db.path")
        if configured:
            return Path(str(configured)).expanduser()
        return data_dir() / "index.db"
