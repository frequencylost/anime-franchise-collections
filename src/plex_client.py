"""Thin wrapper around plexapi for the operations we need."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable

from plexapi.server import PlexServer

log = logging.getLogger("plex")


@dataclass
class PlexAnimeItem:
    rating_key: str
    title: str
    year: int | None
    media_type: str  # 'show' | 'movie'
    library_name: str
    tvdb_id: int | None = None
    tmdb_id: int | None = None
    imdb_id: str | None = None
    anidb_id: int | None = None
    collections: list[str] = field(default_factory=list)
    _plex_obj: object = None  # plexapi item, used for writes

    def has_collection(self, name: str) -> bool:
        return any(c.lower() == name.lower() for c in self.collections)


_GUID_PATTERNS = {
    "tvdb": re.compile(r"tvdb://(\d+)"),
    "tmdb": re.compile(r"tmdb://(\d+)"),
    "imdb": re.compile(r"imdb://(tt\d+)"),
    "anidb": re.compile(r"anidb://(\d+)"),
}


def _extract_external_ids(item) -> dict:
    """Pull TVDB/TMDB/IMDb/AniDB IDs from a plexapi item's GUIDs."""
    ids: dict = {"tvdb": None, "tmdb": None, "imdb": None, "anidb": None}
    guids = []

    # plexapi 4.x exposes Guid via item.guids (list of Guid objs with .id)
    raw_guids = getattr(item, "guids", None) or []
    for g in raw_guids:
        guid = getattr(g, "id", None) or str(g)
        guids.append(guid)

    # The primary guid (item.guid) is sometimes the only one populated.
    primary = getattr(item, "guid", None)
    if primary:
        guids.append(primary)

    for guid in guids:
        if not guid:
            continue
        for source, pattern in _GUID_PATTERNS.items():
            if ids[source] is not None:
                continue
            m = pattern.search(guid)
            if not m:
                continue
            value = m.group(1)
            ids[source] = value if source == "imdb" else int(value)

    return ids


class PlexClient:
    def __init__(self, base_url: str, token: str):
        log.info("Connecting to Plex at %s", base_url)
        self._server = PlexServer(base_url, token, timeout=60)

    def list_anime_items(self, library_names: Iterable[str]) -> list[PlexAnimeItem]:
        """Return all items in the named libraries.

        For TV libraries we return show-level items (not seasons/episodes).
        For movie libraries we return movie items.
        """
        items: list[PlexAnimeItem] = []
        for lib_name in library_names:
            try:
                section = self._server.library.section(lib_name)
            except Exception as e:
                log.warning(
                    "Library %r not found on this Plex server (%s) — skipping",
                    lib_name,
                    e,
                )
                continue
            log.info("Scanning library %r (type=%s)", lib_name, section.type)
            try:
                raw = section.all()
            except Exception:
                log.exception("Failed to list items in %r", lib_name)
                continue

            for raw_item in raw:
                ids = _extract_external_ids(raw_item)
                collections = []
                try:
                    collections = [
                        c.tag for c in (getattr(raw_item, "collections", []) or [])
                    ]
                except Exception:  # noqa: BLE001
                    pass
                media_type = "show" if section.type == "show" else "movie"
                items.append(
                    PlexAnimeItem(
                        rating_key=str(raw_item.ratingKey),
                        title=raw_item.title,
                        year=getattr(raw_item, "year", None),
                        media_type=media_type,
                        library_name=lib_name,
                        tvdb_id=ids["tvdb"],
                        tmdb_id=ids["tmdb"],
                        imdb_id=ids["imdb"],
                        anidb_id=ids["anidb"],
                        collections=collections,
                        _plex_obj=raw_item,
                    )
                )
        return items

    def ensure_collection(self, item: PlexAnimeItem, name: str) -> None:
        """Add `name` to the item's collection tags if not already present."""
        if item.has_collection(name):
            return
        try:
            item._plex_obj.addCollection(name)
            item.collections.append(name)
            log.debug("Added %r → collection %r", item.title, name)
        except Exception:
            log.exception(
                "Failed to add collection %r to item %r", name, item.title
            )
