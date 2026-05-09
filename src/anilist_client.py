"""AniList GraphQL client with on-disk caching and rate-limit handling.

We need two operations:
- get_relations(id): related media edges for a single anime
- get_title(id):     English/romaji/native titles for naming clusters
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field

import requests

log = logging.getLogger("anilist")

ANILIST_API_URL = "https://graphql.anilist.co"

# Relation types AniList exposes that aren't useful for "watch this together"
# franchise collections.
DROP_RELATION_TYPES = {"ADAPTATION", "SOURCE", "CHARACTER"}

# Default cache TTL for relations + titles. Override at runtime via the
# ANILIST_CACHE_DAYS env var. Franchises rarely change, so a long TTL
# (e.g. 30 days) is fine for personal use.
DEFAULT_ANILIST_CACHE_DAYS = 1


def _resolve_cache_ttl_seconds() -> int:
    """Read ANILIST_CACHE_DAYS env var, fall back to default. Bad values
    log a warning and revert to the default rather than crashing."""
    raw = os.environ.get("ANILIST_CACHE_DAYS")
    if raw is None or raw.strip() == "":
        return DEFAULT_ANILIST_CACHE_DAYS * 24 * 60 * 60
    try:
        days = float(raw)
        if days <= 0:
            raise ValueError("must be > 0")
        return int(days * 24 * 60 * 60)
    except ValueError:
        log.warning(
            "Invalid ANILIST_CACHE_DAYS=%r — using default %d day(s)",
            raw,
            DEFAULT_ANILIST_CACHE_DAYS,
        )
        return DEFAULT_ANILIST_CACHE_DAYS * 24 * 60 * 60


@dataclass
class AniListMedia:
    id: int
    title_english: str | None = None
    title_romaji: str | None = None
    title_native: str | None = None
    media_type: str | None = None  # 'ANIME' | 'MANGA'

    def best_title(self) -> str:
        return (
            self.title_english
            or self.title_romaji
            or self.title_native
            or f"AniList #{self.id}"
        )


@dataclass
class AniListRelation:
    relation_type: str
    node: AniListMedia


@dataclass
class AniListEntry:
    media: AniListMedia
    relations: list[AniListRelation] = field(default_factory=list)


class AniListClient:
    """Fetches and caches AniList media + relations data on disk."""

    _RELATIONS_QUERY = """
    query ($id: Int!) {
      Media(id: $id) {
        id
        type
        title { english romaji native }
        relations {
          edges {
            relationType
            node {
              id
              type
              title { english romaji native }
            }
          }
        }
      }
    }
    """

    def __init__(self, cache_dir: str):
        self._cache_path = os.path.join(cache_dir, "anilist_cache.json")
        self._cache_ttl_seconds = _resolve_cache_ttl_seconds()
        log.info(
            "AniList cache TTL: %d days (%d seconds)",
            self._cache_ttl_seconds // 86400,
            self._cache_ttl_seconds,
        )
        self._cache: dict[str, dict] = {}
        if os.path.exists(self._cache_path):
            try:
                with open(self._cache_path, "r", encoding="utf-8") as f:
                    self._cache = json.load(f)
                log.info(
                    "Loaded AniList cache (%d entries) from %s",
                    len(self._cache),
                    self._cache_path,
                )
            except (OSError, json.JSONDecodeError):
                log.warning("Failed to load AniList cache; starting fresh")
                self._cache = {}
        self._dirty = False

    def _save(self) -> None:
        if not self._dirty:
            return
        try:
            with open(self._cache_path, "w", encoding="utf-8") as f:
                json.dump(self._cache, f)
            self._dirty = False
        except OSError:
            log.exception("Failed to write AniList cache")

    def _post_with_retry(self, query: str, variables: dict) -> dict:
        """POST to AniList with retry on 429 + transient 5xx."""
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = requests.post(
                    ANILIST_API_URL,
                    json={"query": query, "variables": variables},
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "User-Agent": "anime-franchise-collections/1.0",
                    },
                    timeout=30,
                )
            except requests.RequestException as e:
                if attempt >= 4:
                    raise
                wait = 2 ** attempt
                log.warning("AniList request error %s — retrying in %ds", e, wait)
                time.sleep(wait)
                continue

            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", "1"))
                log.info("AniList rate-limited; sleeping %ds", retry_after)
                time.sleep(retry_after + 0.5)
                continue

            if 500 <= resp.status_code < 600 and attempt < 4:
                wait = 2 ** attempt
                log.warning(
                    "AniList %d; retrying in %ds", resp.status_code, wait
                )
                time.sleep(wait)
                continue

            resp.raise_for_status()
            data = resp.json()
            if data.get("errors"):
                # Bad input ID is the common case — log and return None-ish.
                msg = "; ".join(e.get("message", "?") for e in data["errors"])
                log.debug("AniList GraphQL error: %s", msg)
                return {}
            return data.get("data") or {}

    def get_entry(self, anilist_id: int) -> AniListEntry | None:
        """Fetch (or return cached) media + relations for an AniList ID."""
        key = str(anilist_id)
        cached = self._cache.get(key)
        if cached and (
            time.time() - cached.get("at", 0) < self._cache_ttl_seconds
        ):
            return _entry_from_cache(cached)

        data = self._post_with_retry(self._RELATIONS_QUERY, {"id": anilist_id})
        media = data.get("Media")
        if not media:
            # Cache the miss as a short-lived empty record to avoid hammering.
            self._cache[key] = {"at": time.time(), "miss": True}
            self._dirty = True
            self._save()
            return None

        record = {
            "at": time.time(),
            "media": _media_to_dict(media),
            "relations": [
                {
                    "relationType": e.get("relationType"),
                    "node": _media_to_dict(e.get("node") or {}),
                }
                for e in (media.get("relations") or {}).get("edges", [])
                if e and e.get("node")
            ],
        }
        self._cache[key] = record
        self._dirty = True
        # Save periodically (every 25 fresh fetches) so a crash mid-run
        # doesn't lose everything.
        if len([k for k, v in self._cache.items() if v.get("at")]) % 25 == 0:
            self._save()
        return _entry_from_cache(record)

    def flush(self) -> None:
        self._save()


def _media_to_dict(node: dict) -> dict:
    title = node.get("title") or {}
    return {
        "id": node.get("id"),
        "type": node.get("type"),
        "english": title.get("english"),
        "romaji": title.get("romaji"),
        "native": title.get("native"),
    }


def _entry_from_cache(record: dict) -> AniListEntry | None:
    if record.get("miss") or not record.get("media"):
        return None
    media = AniListMedia(
        id=record["media"]["id"],
        title_english=record["media"].get("english"),
        title_romaji=record["media"].get("romaji"),
        title_native=record["media"].get("native"),
        media_type=record["media"].get("type"),
    )
    relations = []
    for r in record.get("relations", []):
        n = r.get("node") or {}
        if n.get("id") is None:
            continue
        relations.append(
            AniListRelation(
                relation_type=str(r.get("relationType") or "").upper(),
                node=AniListMedia(
                    id=n["id"],
                    title_english=n.get("english"),
                    title_romaji=n.get("romaji"),
                    title_native=n.get("native"),
                    media_type=n.get("type"),
                ),
            )
        )
    return AniListEntry(media=media, relations=relations)
