"""PlexAniBridge mapping data.

Same data source agregarr uses. Maps AniList IDs ↔ TVDB / TMDB / IMDb / AniDB
so we can find an AniList ID for any Plex item by the IDs Plex already knows.

For multi-season shows, multiple AniList rows can share the same TVDB ID
(one entry per season). We deliberately keep the lowest AniList ID — almost
always Season 1 — to match the behaviour we already use in agregarr.
"""
from __future__ import annotations

import json
import logging
import os
import time

import requests

log = logging.getLogger("mappings")

MAPPING_URL = (
    "https://raw.githubusercontent.com/eliasbenb/PlexAniBridge-Mappings/"
    "refs/heads/v2/mappings.json"
)
MAX_AGE_SECONDS = 12 * 60 * 60  # refetch at most every 12h


def _normalize_array(v):
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


class MappingIndex:
    """Bidirectional index over PlexAniBridge mapping data."""

    def __init__(self, cache_dir: str):
        self._cache_path = os.path.join(cache_dir, "plexanibridge.json")
        # Forward
        self._by_anilist: dict[int, dict] = {}
        # Reverse — multiple AniList rows may map to the same TVDB/TMDB ID
        # (multi-season shows). We keep the row with the LOWEST AniList ID.
        self._by_tvdb: dict[int, dict] = {}
        self._by_tmdb_show: dict[int, dict] = {}
        self._by_tmdb_movie: dict[int, dict] = {}
        self._by_imdb: dict[str, dict] = {}
        self._by_anidb: dict[int, dict] = {}

    def load(self) -> None:
        raw = self._fetch_with_cache()

        for key, row in raw.items():
            if key.startswith("$"):
                continue
            try:
                anilist_id = int(key)
            except ValueError:
                continue
            normalized = dict(row)
            normalized["anilist_id"] = anilist_id
            self._by_anilist[anilist_id] = normalized

            # Reverse indexes — prefer the lowest AniList ID for each key.
            self._prefer_lowest(self._by_anidb, row.get("anidb_id"), anilist_id, normalized)
            self._prefer_lowest(self._by_tvdb, row.get("tvdb_id"), anilist_id, normalized)
            if "tmdb_show_id" in row:
                self._prefer_lowest(
                    self._by_tmdb_show, row.get("tmdb_show_id"), anilist_id, normalized
                )
            for tid in _normalize_array(row.get("tmdb_movie_id")):
                self._prefer_lowest(self._by_tmdb_movie, tid, anilist_id, normalized)
            for iid in _normalize_array(row.get("imdb_id")):
                if isinstance(iid, str):
                    self._prefer_lowest(
                        self._by_imdb, iid.lower(), anilist_id, normalized
                    )
        log.info("Indexed %d AniList mapping rows", len(self._by_anilist))

    @staticmethod
    def _prefer_lowest(target: dict, key, anilist_id: int, row: dict) -> None:
        if key is None:
            return
        existing = target.get(key)
        if existing is None or anilist_id < existing.get(
            "anilist_id", float("inf")
        ):
            target[key] = row

    def _fetch_with_cache(self) -> dict:
        # Use cached file if recent enough.
        try:
            stat = os.stat(self._cache_path)
            age = time.time() - stat.st_mtime
            if age < MAX_AGE_SECONDS:
                with open(self._cache_path, "r", encoding="utf-8") as f:
                    log.info(
                        "Using cached PlexAniBridge mappings (age=%ds)", int(age)
                    )
                    return json.load(f)
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass

        log.info("Fetching PlexAniBridge mappings: %s", MAPPING_URL)
        resp = requests.get(MAPPING_URL, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        os.makedirs(os.path.dirname(self._cache_path), exist_ok=True)
        try:
            with open(self._cache_path, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except OSError:
            log.warning("Could not persist mapping cache to %s", self._cache_path)
        return data

    # ---- Lookups ----

    def lookup_anilist_id_for(self, item) -> int | None:
        """Find the best AniList ID for a Plex item using its known IDs.

        Order of preference:
          - TV: TVDB → TMDB show → IMDb → AniDB
          - Movie: TMDB movie → IMDb
        """
        if item.media_type == "show":
            row = (
                (self._by_tvdb.get(item.tvdb_id) if item.tvdb_id else None)
                or (
                    self._by_tmdb_show.get(item.tmdb_id)
                    if item.tmdb_id
                    else None
                )
                or (
                    self._by_imdb.get(item.imdb_id.lower())
                    if item.imdb_id
                    else None
                )
                or (self._by_anidb.get(item.anidb_id) if item.anidb_id else None)
            )
        else:  # movie
            row = (
                (self._by_tmdb_movie.get(item.tmdb_id) if item.tmdb_id else None)
                or (
                    self._by_imdb.get(item.imdb_id.lower())
                    if item.imdb_id
                    else None
                )
            )
        if not row:
            return None
        return row.get("anilist_id")
