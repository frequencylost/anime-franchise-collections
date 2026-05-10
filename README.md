# anime-franchise-collections

A small Docker app that scans your Plex anime libraries (TV + Movies),
groups related anime into franchises using AniList's relations data, and
applies matching collection tags to every item in a franchise. Plex auto-
links these collections across libraries so a movie spin-off shows up
alongside its parent series.


## What it does

For each anime in your libraries:
1. Resolves Plex's TVDB/TMDB/IMDb ID to an AniList ID via the
   PlexAniBridge mapping.
2. Walks AniList's relation graph (sequels, prequels, side stories,
   spin-offs, alternative versions, compilations, contains-relations).
   Filters out manga adaptations, source material, and character
   cross-references.
3. Builds franchise clusters via union-find.
4. Picks a collection name for each cluster:
   - YAML override (if configured), else
   - the name you've manually applied in Plex (rename detection), else
   - the English title of the cluster's earliest entry (Season 1).
5. Applies the collection tag to every Plex item in the cluster, in both
   the TV and Movies libraries.

Singletons (anime with no related entries in your library) are skipped.

## Running on Unraid

Pull the image from GitHub Container Registry:

```bash
docker pull ghcr.io/<your-username>/anime-franchise-collections:latest
```

Then add a Docker container in Unraid:

| Setting | Value |
|---|---|
| Repository | `ghcr.io/<your-username>/anime-franchise-collections:latest` |
| Network | `bridge` |
| Path: `/config` | `/mnt/user/appdata/anime-franchise-collections/` |
| Variable: `PLEX_URL` | `http://<plex-host>:32400` |
| Variable: `PLEX_TOKEN` | your Plex token |
| Variable: `LIBRARIES` | `Anime,Anime Movies` (your library names) |
| Variable: `SCHEDULE_CRON` | (optional) `0 3 * * 0` for Sun 03:00, or leave blank to run once and exit |

Place `config.yaml` (optional, copy from `config.example.yaml`) into the
appdata folder before the first run if you want overrides.

## Run modes

- **Run once, then exit** — leave `SCHEDULE_CRON` empty. Suitable for
  one-shot Unraid User Scripts or `docker run` from a cron job.
- **Long-running scheduled** — set `SCHEDULE_CRON` (e.g. `0 3 * * 0` for
  weekly at Sunday 03:00). The container runs immediately on startup and
  then on the schedule.

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `PLEX_URL` | (required) | Plex base URL |
| `PLEX_TOKEN` | (required) | Plex auth token |
| `LIBRARIES` | `Anime,Anime Movies` | Comma-separated library names |
| `CONFIG_PATH` | `/config/config.yaml` | Path to overrides YAML |
| `CACHE_DIR` | `/config/cache` | Path for state + caches |
| `SCHEDULE_CRON` | (none — run once) | Cron expression for scheduled mode |
| `DRY_RUN` | `false` | If true, log changes without writing to Plex |
| `MIN_CLUSTER_SIZE` | `2` | Skip franchises with fewer items in your library |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_KEEP` | `10` | Number of past per-run log files to retain |
| `ANILIST_CACHE_DAYS` | `1` | TTL for AniList relations + titles cache. Bump to e.g. `30` for personal use to avoid re-hitting AniList each run |
| `MAPPING_CACHE_HOURS` | `12` | TTL for the PlexAniBridge mapping fetch |
| `MANAGED_LABEL_PATTERN` | `^Agregarr` | Regex; any Plex collection whose label matches is treated as managed by another tool and ignored by rename detection |

## Manual renames in Plex

The state file at `/config/cache/state.json` records the collection name
applied to each franchise. If you rename a collection in Plex (e.g.
"A Certain Magical Index" → "Academy City Collection"), the next run
detects that the cluster's items now share a new name and adopts it.
This is the recommended workflow — no config edits needed.

## YAML overrides

For franchises where you want a name set up front (or to handle clusters
where the auto-detected name is awkward), see `config.example.yaml`.
Two match modes:

- `parent_anilist_id: <id>` — matches the cluster whose earliest entry
  is that AniList ID. Find it from the AniList URL of the parent show
  (e.g. `https://anilist.co/anime/4654/` → `4654`).
- `match_title: "..."` — matches the auto-generated English title
  (case-insensitive).

## First-run preview (dry run)

Before letting it touch Plex, run with `DRY_RUN=true` to see exactly
which collections it would create:

```bash
docker run --rm \
  -e PLEX_URL=http://192.168.1.10:32400 \
  -e PLEX_TOKEN=xxx \
  -e LIBRARIES="Anime,Anime Movies" \
  -e DRY_RUN=true \
  -v /mnt/user/appdata/anime-franchise-collections:/config \
  ghcr.io/<your-username>/anime-franchise-collections:latest
```

The logs will list every cluster, its size, and the chosen name. No
Plex writes happen.

## Logs and cluster report

After every run, two files are written to `/config/logs/`:

- `run-YYYYMMDD-HHMMSS.log` — full DEBUG-level log of the run, including
  every relation union the script made. Older logs are rotated; the most
  recent 10 are kept (override with `LOG_KEEP=N`).
- `clusters_report.json` — the most useful file for auditing results.
  Lists every cluster, the chosen collection name, every member's
  AniList ID + title, and which Plex items are in the cluster. Open it
  in any text editor or JSON viewer.

Use the cluster report to spot unexpected groupings (see Troubleshooting).

## Caches

- `/config/cache/plexanibridge.json` — PlexAniBridge mapping data,
  refreshed every 12h.
- `/config/cache/anilist_cache.json` — AniList relation/title responses,
  TTL 24h.
- `/config/cache/state.json` — last-applied collection name per cluster.

You can delete any of these to force a re-fetch on the next run.

## Troubleshooting

### Two unrelated franchises ended up in the same collection

AniList's relation graph occasionally connects unrelated franchises
through a single bridge entry — usually a compilation, anthology, or
crossover special that lists both as relations.

**To find the bridge:**

1. Open `/config/logs/clusters_report.json`.
2. Locate the over-large cluster (search for the unexpected collection
   name, e.g. `"Neon Genesis Evangelion"`).
3. Scan the cluster's `members` list for entries that don't belong —
   compilation specials, anniversary OVAs, parody crossovers. Note the
   `anilist_id` of the offender.
4. Edit `config.yaml` and add the AniList ID to `blocklist_anilist_ids`:

   ```yaml
   blocklist_anilist_ids:
     - 12345   # the bridge entry
   ```

5. Restart the container. The next run skips that entry entirely so it
   can no longer bridge the two franchises.

**Preventive option:** if you keep hitting this problem, drop the
relation types that most often cause cross-franchise leaks. Edit
`config.yaml`:

```yaml
extra_dropped_relation_types:
  - COMPILATION
  - CONTAINS
  - OTHER
```

This is a global setting and is more aggressive than the blocklist.
Use it if you'd rather break a few legitimate compilation links than
keep playing whack-a-mole with bridge entries.

### The script took hours to finish

That's expected on the first run for a large library. AniList rate-
limits at ~90 requests/min, so a library with 200 franchises with deep
relation graphs can easily take 30–60 minutes the first time. Subsequent
runs are fast — the AniList cache (`/config/cache/anilist_cache.json`)
holds responses for 24h, and the PlexAniBridge mapping is cached for 12h.

### A specific Plex item ends up in a cluster it has nothing to do with

This usually means the upstream PlexAniBridge mapping data has a wrong
AniList ↔ TMDB/TVDB/IMDb ID match for that item. Symptoms in the
cluster report: a `plex_items` entry whose title doesn't match anything
in the cluster's `members` list — the title shown in `members` is what
AniList says the AniList ID should be; the title shown in `plex_items`
is what your Plex item actually is. If they disagree, you've hit a
mapping bug.

Two fixes via `config.yaml`:

**(a) Skip the item entirely** if you don't care which collection it
ends up in:

```yaml
plex_item_blocklist:
  - "84589"   # rating_key from clusters_report.json
```

**(b) Force the correct AniList ID** so the item ends up in its real
franchise:

```yaml
mapping_overrides:
  - rating_key: "84589"
    anilist_id: 4147   # found from the AniList URL of the right entry
```

Search AniList for the show, take the numeric ID from the URL
(`/anime/4147/...` → `4147`), and put it here. The script then ignores
PlexAniBridge for that one item.

After the next run, `clusters_report.json` will show a `via_override: true`
flag on items whose mapping you've forced, so you can confirm.

### Genre collections from another tool got picked up as renames

If you also run agregarr (or another tool) that creates genre or theme
collections, the script identifies them via their Plex **labels** and
excludes them from rename detection.

Every collection agregarr creates carries a label like `Agregarranilist18351`
/ `Agregarrtmdb12345`. The script reads each collection's labels via the
Plex API and excludes any whose label matches `MANAGED_LABEL_PATTERN`
(default `^Agregarr`). If you use a different tool that stamps its own
labels, set the env var to a regex that matches them, e.g.
`^(Agregarr|MyOtherTool)`.

Collection size is **not** used as a heuristic — small genre collections
(e.g. "Death Game", 12 entries) and large ones ("Action", 80 entries)
are treated identically. The only signal is the label.

If you ran an older version of this script and ended up with corrupted
state entries (e.g. state.json shows a cluster's name as "Action"), the
recovery is automatic: on the next run, that wrong name is the new
"state" name, the rename detector excludes it, and the franchise's real
collection tag (still on the Plex items from a prior run) becomes the
detected rename — restoring the correct name. No manual intervention
needed.

If you want to be sure, you can also delete `state.json` and let the
script rebuild names from scratch on the next run.

### I want to see what happened during a long run

The full log is written to `/config/logs/run-*.log` whether or not the
container is still running. Open the most recent file to see every
union, every cluster decision, every Plex write.

## Notes

- The script only **adds** to a Plex item's existing collection list; it
  doesn't remove or replace other collection tags. If an item is in
  "Top Anime 2024" and gets added to "Attack on Titan", it stays in both.
- Movies and TV shows in different libraries collect under the same
  collection name automatically — Plex links them across libraries by
  matching the tag text.
- AniList rate limit is ~90 req/min; the script throttles automatically
  on 429 responses.
