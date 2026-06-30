#!/usr/bin/env python3
"""
AlphaEdge Log Cleanup
─────────────────────
Deletes log files older than RETENTION_DAYS from logs/.
Run via GitHub Actions cron daily or manually.

Bounded to .log files only (won't touch .json state).
"""
import os
import sys
import time
from pathlib import Path
from datetime import datetime

RETENTION_DAYS = 30
LOGS_DIR = Path("logs")


def cleanup(dry_run: bool = False) -> tuple[int, int]:
    """Delete .log files older than RETENTION_DAYS. Returns (removed, kept)."""
    if not LOGS_DIR.exists():
        print(f"No logs/ dir — nothing to clean.")
        return 0, 0

    cutoff = time.time() - RETENTION_DAYS * 86400
    removed = 0
    kept    = 0
    bytes_freed = 0

    for f in LOGS_DIR.iterdir():
        if not f.is_file():
            continue
        if f.suffix not in (".log", ".log.gz"):
            continue
        try:
            mtime = f.stat().st_mtime
            size  = f.stat().st_size
        except OSError:
            continue

        if mtime < cutoff:
            age_days = (time.time() - mtime) / 86400
            action = "WOULD DELETE" if dry_run else "DELETED"
            print(f"  {action}: {f.name} ({age_days:.1f}d old, {size} bytes)")
            if not dry_run:
                try:
                    f.unlink()
                except OSError as e:
                    print(f"    ERROR: {e}")
                    continue
            removed += 1
            bytes_freed += size
        else:
            kept += 1

    print(f"\nSummary: removed={removed}, kept={kept}, freed={bytes_freed/1024:.1f}KB")
    return removed, kept


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    print(f"AlphaEdge log cleanup (retention={RETENTION_DAYS} days, dry_run={dry})")
    cleanup(dry_run=dry)
