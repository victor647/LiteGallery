from __future__ import annotations

import os
from pathlib import Path


APP_NAME = "LiteGallery"


def app_data_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    directory = base / APP_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def bridge_dir() -> Path:
    directory = app_data_dir() / "lightroom-bridge"
    (directory / "requests").mkdir(parents=True, exist_ok=True)
    (directory / "acks").mkdir(parents=True, exist_ok=True)
    return directory


def thumbnail_dir() -> Path:
    directory = app_data_dir() / "thumbnails"
    directory.mkdir(parents=True, exist_ok=True)
    return directory
