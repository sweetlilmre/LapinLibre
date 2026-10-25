# SPDX-License-Identifier: MIT
"""Shared helpers for LapinLibre's Python build tools (standard library only)."""

import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_config():
    with open(ROOT / "config.toml", "rb") as f:
        return tomllib.load(f)


def work_base():
    """The directory holding dl/, work/ and out/ (see scripts/common.sh)."""
    if os.environ.get("LAPINLIBRE_WORK"):
        return Path(os.environ["LAPINLIBRE_WORK"])
    if str(ROOT).startswith("/mnt/"):
        return Path.home() / ".cache" / "lapinlibre"
    return ROOT


def dl_dir():
    return Path(os.environ.get("LL_DL") or work_base() / "dl")


def work_dir():
    return Path(os.environ.get("LL_WORK") or work_base() / "work")


def out_dir():
    return Path(os.environ.get("LL_OUT") or work_base() / "out")
