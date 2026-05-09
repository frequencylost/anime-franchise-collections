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

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone

from croniter import croniter

from .anilist_client import AniListClient
from .cluster import build_clusters, resolve_collection_name
from .mappings import MappingIndex
from .plex_client import PlexClient
from .state import State

_FILE_LOG_HANDLER: logging.Handler | None = None


def _setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _attach_file_logging(cache_dir: str) -> str | None:
    """Add a per-run file handler under <cache_dir>/../logs/run-<ts>.log.
    Keeps the most recent N log files; older ones are deleted.
    """
    global _FILE_LOG_HANDLER
    log_dir = os.environ.get("LOG_DIR") or os.path.normpath(
        os.path.join(cache_dir, "..", "logs")
    )
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError:
        logging.getLogger("main").warning(
            "Could not create log dir %s — file logging disabled", log_dir
        )
        return None

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = os.path.join(log_dir, f"run-{ts}.log")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        )
    )
    fh.setLevel(logging.DEBUG)  # always capture full detail to file
    logging.getLogger().addHandler(fh)
    _FILE_LOG_HANDLER = fh

    # Rotate: keep most recent N
    try:
        keep = int(os.environ.get("LOG_KEEP", "10"))
    except ValueError:
        keep = 10
    try:
        files = sorted(
            (
                f
                for f in os.listdir(log_dir)
                if f.startswith("run-") and f.endswith(".log")
            ),
            reverse=True,
        )
        for old in files[keep:]:
            try:
                os.remove(os.path.join(log_dir, old))
            except OSError:
                pass
    except OSError:
        pass

    logging.getLogger("main").info("Log file: %s", log_path)
    return log_path


def _detach_file_logging() -> None:
    """Close the per-run file handler so the next scheduled run gets a
    fresh log file with its own timestamp."""
    global _FILE_LOG_HANDLER
    if _FILE_LOG_HANDLER is None:
        return
    logging.getLogger().removeHandler(_FILE_LOG_HANDLER)
    try:
        _FILE_LOG_HANDLER.close()
    except Exception:  # noqa: BLE001
        pass
    _FILE_LOG_HANDLER = None


def _write_cluster_report(
    cache_dir: str,
    clusters,
    items_by_anilist,
    anilist,
    final_names,
    blocklist,
) -> None:
    """Dump a JSON report of every cluster, its members, and the final name.

    This is the easiest way to find unexpected entries: open the file,
    search for an unexpected title, and you'll see what cluster it landed
    in and which neighbours pulled it in. Add the offending AniList ID to
    the blocklist in config.yaml to break that bridge on the next run.
    """
    log_dir = os.environ.get("LOG_DIR") or os.path.normpath(
        os.path.join(cache_dir, "..", "logs")
    )
    try:
        os.makedirs(log_dir, exist_ok=True)
    except OSError:
        return

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "blocklist": sorted(blocklist or []),
        "clusters": [],
    }
    for cluster in clusters:
        plex_items_in_cluster = []
        for aid in cluster.anilist_ids:
            for item in items_by_anilist.get(aid, []):
                plex_items_in_cluster.append(
                    {
                        "rating_key": item.rating_key,
                        "title": item.title,
                        "library": item.library_name,
                        "anilist_id": aid,
                    }
                )

        # Resolve AniList titles for every member (even ones not in Plex)
        # so the report shows what bridged the cluster together.
        members = []
        for aid in cluster.anilist_ids:
            entry = anilist.get_entry(aid)
            members.append(
                {
                    "anilist_id": aid,
                    "title": entry.media.best_title() if entry else f"AniList #{aid}",
                    "in_plex": any(
                        i["anilist_id"] == aid for i in plex_items_in_cluster
                    ),
                }
            )

        payload["clusters"].append(
            {
                "parent_id": cluster.parent_id,
                "name": final_names.get(cluster.parent_id),
                "size_in_library": len(plex_items_in_cluster),
                "members": members,
                "plex_items": plex_items_in_cluster,
            }
        )

    payload["clusters"].sort(
        key=lambda c: (-c["size_in_library"], c["parent_id"])
    )

    out_path = os.path.join(log_dir, "clusters_report.json")
    try:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        logging.getLogger("main").info("Cluster report: %s", out_path)
    except OSError:
        logging.getLogger("main").warning(
            "Could not write cluster report to %s", out_path
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
    _attach_file_logging(cache_dir)
    config = _load_config()

    # Cluster-tuning options pulled from config.
    blocklist = {
        int(x)
        for x in (config.get("blocklist_anilist_ids") or [])
        if isinstance(x, (int, str)) and str(x).isdigit()
    }
    extra_dropped = {
        str(x).upper()
        for x in (config.get("extra_dropped_relation_types") or [])
    }
    if blocklist:
        log.info("Blocklist active: %s", sorted(blocklist))
    if extra_dropped:
        log.info("Dropping extra relation types: %s", sorted(extra_dropped))

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
    clusters = build_clusters(
        plex_to_anilist.values(),
        anilist,
        blocklist=blocklist,
        extra_dropped_relation_types=extra_dropped,
    )
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

    # Identify collections managed by another tool (default: agregarr,
    # whose collections carry a label like "Agregarranilist18351").
    # We never treat these as franchise-rename candidates because their
    # names describe genres / lists, not franchises.
    label_pattern_raw = os.environ.get("MANAGED_LABEL_PATTERN", "^Agregarr")
    try:
        label_pattern = re.compile(label_pattern_raw)
    except re.error as e:
        log.warning(
            "Invalid MANAGED_LABEL_PATTERN=%r (%s) — using default '^Agregarr'",
            label_pattern_raw,
            e,
        )
        label_pattern = re.compile("^Agregarr")
    excluded_collection_names = plex.list_managed_collection_names(
        libraries, label_pattern
    )

    applied = 0
    skipped_singletons = 0
    final_names: dict[int, str] = {}
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
            excluded_collection_names=excluded_collection_names,
        )
        final_names[cluster.parent_id] = name
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
        applied += 1

    if not dry_run:
        state.save()

    # Always write the cluster report so the user can audit results.
    _write_cluster_report(
        cache_dir=cache_dir,
        clusters=clusters,
        items_by_anilist=items_by_anilist,
        anilist=anilist,
        final_names=final_names,
        blocklist=blocklist,
    )

    log.info(
        "Done. Applied=%d clusters, skipped singletons=%d, dry_run=%s",
        applied,
        skipped_singletons,
        dry_run,
    )
    _detach_file_logging()
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
