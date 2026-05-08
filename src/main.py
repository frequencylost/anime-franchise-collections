"""Entry point for anime-franchise-collections.

Modes:
- Run-once (default): execute one sync pass and exit. Suitable for cron.
- Scheduled (SCHEDULE_CRON env var set): run on the cron expression in a
  long-lived loop. Suitable for `docker run -d`.

Environment variables:
  PLEX_URL              Plex base URL (e.g. http://192.168.1.10:32400)
  PLEX_TOKEN            Plex auth token
  LIBRARIES             Comma-separated library names to scan
                        (default: "Anime,Anime Movies")
  CONFIG_PATH           YAML config path (default: /config/config.yaml)
  CACHE_DIR             Directory for state + caches (default: /config/cache)
  SCHEDULE_CRON         Cron expression; if set, runs on schedule instead
                        of once-and-exit (e.g. "0 3 * * 0" for Sun 03:00)
  DRY_RUN               "true" to skip writing to Plex (default: false)
  MIN_CLUSTER_SIZE      Skip clusters smaller than this (default: 2)
  LOG_LEVEL             DEBUG, INFO, WARNING, ERROR (default: INFO)
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone

from croniter import croniter

from .anilist_client import AniListClient
from .cluster import build_clusters, resolve_collection_name
from .mappings import MappingIndex
from .plex_client import PlexClient
from .state import State


def _setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _load_config() -> dict:
    """Load YAML config if present. Returns empty dict if missing."""
    import yaml

    path = os.environ.get("CONFIG_PATH", "/config/config.yaml")
    if not os.path.exists(path):
        logging.info("No config file at %s — using defaults", path)
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    logging.info("Loaded config from %s", path)
    return data


def run_once() -> int:
    """Execute one full sync pass. Returns process exit code."""
    log = logging.getLogger("main")

    plex_url = os.environ.get("PLEX_URL")
    plex_token = os.environ.get("PLEX_TOKEN")
    if not plex_url or not plex_token:
        log.error("PLEX_URL and PLEX_TOKEN must both be set")
        return 2

    libraries = [
        s.strip()
        for s in os.environ.get("LIBRARIES", "Anime,Anime Movies").split(",")
        if s.strip()
    ]
    cache_dir = os.environ.get("CACHE_DIR", "/config/cache")
    dry_run = os.environ.get("DRY_RUN", "false").lower() in ("1", "true", "yes")
    min_cluster_size = int(os.environ.get("MIN_CLUSTER_SIZE", "2"))

    os.makedirs(cache_dir, exist_ok=True)
    config = _load_config()

    # 1. Connect to Plex and inventory both libraries.
    plex = PlexClient(plex_url, plex_token)
    items = plex.list_anime_items(libraries)
    log.info("Found %d items across %s", len(items), libraries)

    # 2. Load PlexAniBridge mapping data and find each item's AniList ID.
    mappings = MappingIndex(cache_dir)
    mappings.load()

    plex_to_anilist: dict[str, int] = {}
    for item in items:
        anilist_id = mappings.lookup_anilist_id_for(item)
        if anilist_id is not None:
            plex_to_anilist[item.rating_key] = anilist_id
    log.info(
        "Mapped %d/%d Plex items to AniList IDs",
        len(plex_to_anilist),
        len(items),
    )
    if not plex_to_anilist:
        log.warning("No items could be mapped to AniList — nothing to do")
        return 0

    # 3. Fetch relations for each AniList ID and cluster franchises.
    anilist = AniListClient(cache_dir)
    clusters = build_clusters(plex_to_anilist.values(), anilist)
    log.info("Built %d clusters before filtering", len(clusters))

    # 4. Resolve each cluster's name (overrides → state-aware Plex
    #    rename detection → auto-name) and apply collection tags.
    state = State(os.path.join(cache_dir, "state.json"))
    state.load()

    items_by_anilist: dict[int, list] = {}
    for item in items:
        aid = plex_to_anilist.get(item.rating_key)
        if aid is None:
            continue
        items_by_anilist.setdefault(aid, []).append(item)

    applied = 0
    skipped_singletons = 0
    for cluster in clusters:
        cluster_items = []
        for aid in cluster.anilist_ids:
            cluster_items.extend(items_by_anilist.get(aid, []))

        if len(cluster_items) < min_cluster_size:
            skipped_singletons += 1
            continue

        name = resolve_collection_name(
            cluster=cluster,
            cluster_items=cluster_items,
            anilist=anilist,
            state=state,
            config=config,
        )
        log.info(
            "Cluster parent=%d size=%d name=%r",
            cluster.parent_id,
            len(cluster_items),
            name,
        )

        if not dry_run:
            for item in cluster_items:
                plex.ensure_collection(item, name)

        state.set_cluster_name(cluster.parent_id, name)

    if not dry_run:
        state.save()

    log.info(
        "Done. Applied=%d clusters, skipped singletons=%d, dry_run=%s",
        applied,
        skipped_singletons,
        dry_run,
    )
    return 0


def main() -> int:
    _setup_logging()
    schedule = os.environ.get("SCHEDULE_CRON", "").strip()
    if not schedule:
        return run_once()

    log = logging.getLogger("main")
    log.info("Scheduled mode: cron=%r", schedule)
    # Run immediately on startup, then follow the cron schedule.
    run_once()
    while True:
        now = datetime.now(timezone.utc)
        nxt = croniter(schedule, now).get_next(datetime)
        sleep_for = max(1, int((nxt - now).total_seconds()))
        log.info(
            "Next run at %s (sleeping %d seconds)",
            nxt.isoformat(),
            sleep_for,
        )
        time.sleep(sleep_for)
        try:
            run_once()
        except Exception:  # noqa: BLE001 — keep loop alive on any error
            log.exception("run_once raised; will retry at next scheduled time")


if __name__ == "__main__":
    sys.exit(main())
