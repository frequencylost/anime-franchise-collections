"""Persistent state — tracks the most recently applied collection name for
each cluster, keyed by parent AniList ID.

Used to:
  - Detect when the user has renamed a collection in Plex (state name no
    longer matches the dominant collection on cluster items).
  - Avoid redundant Plex writes for unchanged names.
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("state")


class State:
    def __init__(self, path: str):
        self._path = path
        self._data: dict[str, str] = {}

    def load(self) -> None:
        if not os.path.exists(self._path):
            self._data = {}
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            self._data = {str(k): str(v) for k, v in (raw or {}).items()}
            log.info("Loaded state (%d clusters) from %s", len(self._data), self._path)
        except (OSError, json.JSONDecodeError):
            log.warning("Could not read state %s — starting fresh", self._path)
            self._data = {}

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            tmp = self._path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2, sort_keys=True)
            os.replace(tmp, self._path)
        except OSError:
            log.exception("Failed to write state to %s", self._path)

    def get_cluster_name(self, parent_anilist_id: int) -> str | None:
        return self._data.get(str(parent_anilist_id))

    def set_cluster_name(self, parent_anilist_id: int, name: str) -> None:
        self._data[str(parent_anilist_id)] = name
