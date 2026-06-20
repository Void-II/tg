"""
cleanup_files.py — deletes downloaded media from data/files/, with option to
exclude profile pictures and filter by age. Plain files, no encryption (private repo).
"""

import os
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

EXCLUDE_PROFILE_PICS = os.environ.get("EXCLUDE_PROFILE_PICS", "true").lower() == "true"
OLDER_THAN_DAYS      = int(os.environ.get("OLDER_THAN_DAYS", "0"))

DATA_DIR    = Path("data")
FILES_DIR   = DATA_DIR / "files"
FILES_INDEX = DATA_DIR / "files_index.json"

def load_index() -> dict:
    if FILES_INDEX.exists():
        try:
            return json.loads(FILES_INDEX.read_text())
        except Exception:
            return {}
    return {}

def save_index(idx: dict):
    FILES_INDEX.write_text(json.dumps(idx, indent=2, default=str))

def main():
    index = load_index()
    if not index:
        print("No files index found — nothing to clean up.")
        return

    cutoff = None
    if OLDER_THAN_DAYS > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(days=OLDER_THAN_DAYS)

    deleted, kept = [], []

    for safe_name, entry in list(index.items()):
        label = entry.get("label", "")
        is_profile = label in ("profile_pic", "profile_media")

        if EXCLUDE_PROFILE_PICS and is_profile:
            kept.append(safe_name)
            continue

        if cutoff:
            dl_at = entry.get("downloaded_at")
            try:
                dl_dt = datetime.fromisoformat(dl_at)
                if dl_dt > cutoff:
                    kept.append(safe_name)
                    continue
            except Exception:
                pass

        file_path = Path(entry.get("path", FILES_DIR / safe_name))
        if file_path.exists():
            file_path.unlink()
        deleted.append(safe_name)
        del index[safe_name]

    save_index(index)

    print(f"✓ Deleted {len(deleted)} file(s)")
    print(f"✓ Kept {len(kept)} file(s) ({'profile pics excluded' if EXCLUDE_PROFILE_PICS else 'age filter'})")
    print(f"  Remaining in index: {len(index)}")

main()
