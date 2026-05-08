"""Cluster anime into franchises and resolve each cluster's collection name.

Clustering: Union-Find over AniList relations. Two AniList IDs end up in the
same cluster if they're connected by any chain of (non-dropped) relation
edges. The "parent" of a cluster is the lowest AniList ID — the canonical
"the show" entry, same heuristic we already use for ratings.

Naming priority (highest to lowest):
  1. YAML override matching the parent_anilist_id (or the auto-name title)
  2. State-aware Plex rename detection — if a cluster's items currently
     share a different collection name from the one we last applied, adopt
     that new name. Lets the user rename in Plex and have the change stick.
  3. The English title of the lowest-AniList-ID entry in the cluster.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

from .anilist_client import AniListClient, DROP_RELATION_TYPES
from .state import State

log = logging.getLogger("cluster")


@dataclass
class Cluster:
    parent_id: int
    anilist_ids: list[int] = field(default_factory=list)


class _UnionFind:
    def __init__(self):
        self._parent: dict[int, int] = {}

    def add(self, x: int) -> None:
        if x not in self._parent:
            self._parent[x] = x

    def find(self, x: int) -> int:
        self.add(x)
        # Path compression
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # Always parent to the smaller AniList ID — convenient because
            # the smaller ID is also the one we want as the cluster "parent".
            if rb < ra:
                ra, rb = rb, ra
            self._parent[rb] = ra

    def groups(self) -> dict[int, list[int]]:
        out: dict[int, list[int]] = {}
        for node in list(self._parent):
            out.setdefault(self.find(node), []).append(node)
        return out


def build_clusters(
    seed_anilist_ids: Iterable[int], anilist: AniListClient
) -> list[Cluster]:
    """BFS through AniList relations starting from each seed, then union-find.

    The cluster set may include AniList IDs the user does not own in Plex
    (intermediate relations). That's fine — they get filtered out at apply
    time when we look up Plex items by AniList ID.
    """
    uf = _UnionFind()
    visited: set[int] = set()
    queue: list[int] = list(set(seed_anilist_ids))

    for sid in queue:
        uf.add(sid)

    while queue:
        anilist_id = queue.pop()
        if anilist_id in visited:
            continue
        visited.add(anilist_id)

        entry = anilist.get_entry(anilist_id)
        if entry is None:
            continue
        if (entry.media.media_type or "ANIME") != "ANIME":
            continue

        for rel in entry.relations:
            if rel.relation_type in DROP_RELATION_TYPES:
                continue
            if (rel.node.media_type or "ANIME") != "ANIME":
                continue
            other = rel.node.id
            uf.union(anilist_id, other)
            if other not in visited:
                queue.append(other)

    anilist.flush()

    clusters: list[Cluster] = []
    for parent, members in uf.groups().items():
        members_sorted = sorted(members)
        clusters.append(
            Cluster(parent_id=members_sorted[0], anilist_ids=members_sorted)
        )
    # Stable order for logging.
    clusters.sort(key=lambda c: c.parent_id)
    return clusters


# ---- Naming ----

def _override_for(cluster: Cluster, auto_name: str, config: dict) -> str | None:
    overrides = (config or {}).get("overrides") or []
    for entry in overrides:
        if not isinstance(entry, dict):
            continue
        pid = entry.get("parent_anilist_id")
        if pid is not None and int(pid) == cluster.parent_id:
            return str(entry["name"])
        match_title = entry.get("match_title")
        if match_title and str(match_title).lower() == (auto_name or "").lower():
            return str(entry["name"])
    return None


def _detect_plex_rename(
    cluster_items, current_state_name: str | None
) -> str | None:
    """Pick a collection name that the user has applied in Plex to a
    majority of this cluster's items, ignoring the name we previously set.

    Returns the user-applied name if one dominates the cluster, else None.
    """
    if not cluster_items:
        return None
    counter: Counter[str] = Counter()
    for item in cluster_items:
        for name in item.collections:
            counter[name] += 1

    # Need a majority (>= ceil(N/2)) and the candidate must NOT be the
    # state name (which is unchanged) and must NOT be globally common
    # (we don't have that signal here, but the majority bar handles it).
    threshold = (len(cluster_items) + 1) // 2
    candidates = [
        (name, count)
        for name, count in counter.items()
        if count >= threshold
        and (current_state_name is None or name != current_state_name)
    ]
    if not candidates:
        return None
    # If multiple, pick highest count, tie-break alphabetically.
    candidates.sort(key=lambda x: (-x[1], x[0]))
    return candidates[0][0]


def resolve_collection_name(
    cluster: Cluster,
    cluster_items,
    anilist: AniListClient,
    state: State,
    config: dict,
) -> str:
    # Auto-name: English title of the parent (lowest-AniList-ID) entry.
    parent_entry = anilist.get_entry(cluster.parent_id)
    auto_name = (
        parent_entry.media.best_title()
        if parent_entry
        else f"AniList #{cluster.parent_id}"
    )

    # 1. YAML override wins
    override = _override_for(cluster, auto_name, config)
    if override:
        return override

    # 2. Plex-rename detection
    last_applied = state.get_cluster_name(cluster.parent_id)
    if last_applied:
        # Did the user remove the last_applied tag from most items? If so,
        # adopt whatever they put in its place.
        renamed = _detect_plex_rename(cluster_items, last_applied)
        if renamed:
            log.info(
                "Detected rename for cluster %d: %r -> %r",
                cluster.parent_id,
                last_applied,
                renamed,
            )
            return renamed
        # Otherwise stick with the last name we applied (idempotent).
        return last_applied

    # 3. Default: auto-name
    return auto_name
