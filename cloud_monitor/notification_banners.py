"""Packaged, non-sensitive PNG headers shared by notification transports."""

from functools import lru_cache
from importlib.resources import files
from typing import Any


BANNER_FILES = {"P0": "critical.png", "P1": "warning.png", "P2": "info.png", "recovery": "recovery.png"}
BANNER_CID = "notification-banner"


def banner_key(event: dict[str, Any]) -> str:
    if event.get("phase") == "recovery":
        return "recovery"
    severity = event.get("severity")
    return severity if severity in BANNER_FILES and severity != "recovery" else "P2"


@lru_cache(maxsize=4)
def banner_bytes(key: str) -> bytes:
    filename = BANNER_FILES[key]
    return files("cloud_monitor").joinpath("assets", "notification-banners", filename).read_bytes()
