"""Central configuration: paths, YAML settings, environment, and the (overridable) notion of 'today'."""
from __future__ import annotations

import datetime as dt
import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"

load_dotenv(ROOT / ".env")


@lru_cache(maxsize=1)
def settings() -> dict:
    with open(CONFIG_DIR / "settings.yaml", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    total = sum(cfg["weights"].values())
    if abs(total - 100) > 1e-6:
        raise ValueError(f"confidence weights must sum to 100, got {total}")
    return cfg


@lru_cache(maxsize=1)
def domain_config() -> dict:
    with open(CONFIG_DIR / "domains.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@lru_cache(maxsize=1)
def seed_config() -> dict:
    with open(CONFIG_DIR / "seeds.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def db_path() -> Path:
    p = Path(os.getenv("ATLAS_DB", "data/atlas.db"))
    return p if p.is_absolute() else ROOT / p


_AS_OF_OVERRIDE: dt.date | None = None


def set_as_of(d: dt.date | None) -> None:
    """Lets the CLI pin the logical date for lifecycle checks, never for source evidence."""
    global _AS_OF_OVERRIDE
    _AS_OF_OVERRIDE = d


def today() -> dt.date:
    if _AS_OF_OVERRIDE:
        return _AS_OF_OVERRIDE
    env = os.getenv("ATLAS_AS_OF", "").strip()
    if env:
        return dt.date.fromisoformat(env)
    return dt.date.today()


def now_iso() -> str:
    base = dt.datetime.now().replace(microsecond=0)
    if _AS_OF_OVERRIDE or os.getenv("ATLAS_AS_OF", "").strip():
        # keep wall-clock time-of-day but use the logical date so histories sort sensibly
        base = dt.datetime.combine(today(), base.time())
    return base.isoformat(sep=" ")
