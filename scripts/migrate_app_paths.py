"""One-off: add the per-app ``log_file``/``pid_file`` columns to the
``applications`` table (spec 03 §2.5).

``create_all`` creates missing tables but does not ALTER existing ones, so a
pre-existing database has no columns for the two optional paths. Both are
nullable (NULL = the id-based default path), so no data conversion is needed —
the migration only adds the columns. Idempotent — re-running is a no-op.

Note: the new code also adds these columns automatically on startup (the
``init_db`` column-drift step, spec 16), so this script is an *optional*
pre-migration — useful to convert the database before starting the new code.

Run WITH THE SERVER STOPPED:

    env UV_CACHE_DIR=$PWD/.uv-cache uv run python3 \
        scripts/migrate_app_paths.py prod/gpusymbiosis.db
"""

from __future__ import annotations

import sqlite3
import sys


def main(db_path: str) -> None:
    con = sqlite3.connect(db_path)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(applications)")}
        if not cols:
            print(f"no 'applications' table in {db_path} — nothing to do")
            return
        added = []
        for col in ("log_file", "pid_file"):
            if col not in cols:
                con.execute("ALTER TABLE applications ADD COLUMN " + col + " TEXT")
                added.append(col)
        con.commit()
        if added:
            print("added column(s): " + ", ".join(added))
        else:
            print("already migrated — nothing to do")
    finally:
        con.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "prod/gpusymbiosis.db")
