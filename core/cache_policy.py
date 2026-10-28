"""Shared disk retention policy (reads never extend the write-time lifetime)."""
import time
from pathlib import Path


# Shared by rendered-card and persistent parse-result identities. Bump only when
# parser/card behavior changes make existing results unsafe or semantically stale;
# ordinary releases and changes already captured by config/input keys need no bump.
# This is not DiskResultCache.VERSION, which describes the JSON storage schema.
CACHE_BEHAVIOR_VERSION = "parser_x_behavior_v23_bili_adjacent_topics"
CACHE_RETENTION_SECONDS = 24 * 60 * 60


def reusable_card(path: object) -> bool:
    """Accept nonempty cards for 24h after writing, independent of cleanup runs."""
    if not isinstance(path, Path):
        return False
    try:
        stat = path.stat()
        return (path.is_file() and stat.st_size > 0
                and 0 <= time.time() - stat.st_mtime < CACHE_RETENTION_SECONDS)
    except OSError:
        return False
