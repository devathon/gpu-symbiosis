"""One-off: convert app_metric_lifetime from fixed columns to the JSON
lifetime document (spec 08 §5.1, spec 04 §8).

The type-specific fields are now dynamic (stored in the ``lifetime`` JSON
column), so the old columns are dropped. Existing rows are converted first.
Idempotent — re-running after the migration is a no-op.

Run WITH THE SERVER STOPPED:

    env UV_CACHE_DIR=$PWD/.uv-cache uv run python3 \
        scripts/migrate_lifetime_json.py prod/gpusymbiosis.db
"""

from __future__ import annotations

import json
import sqlite3
import sys

# Old type-specific columns (the tailer's state() keys minus the mechanism
# columns that stay as columns: log_offset, log_fingerprint).
_OLD_COLUMNS = (
    "input_tokens", "output_tokens", "total_tokens", "total_time_ms",
    "prompt_time_ms", "eval_time_ms", "requests",
    "draft_accepted", "draft_generated", "draft_mean_len_weighted",
    "slot_selections", "cache_hit_requests",
)


def main(db_path: str) -> None:
    con = sqlite3.connect(db_path)
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(app_metric_lifetime)")}
        present = [c for c in _OLD_COLUMNS if c in cols]
        if "lifetime" not in cols:
            con.execute("ALTER TABLE app_metric_lifetime "
                        "ADD COLUMN lifetime TEXT DEFAULT '{}'")
            con.commit()
        if not present:
            print("already migrated — nothing to do")
            return
        for row in con.execute(
            "SELECT application_id, " + ", ".join(present)
            + " FROM app_metric_lifetime"
        ):
            app_id = row[0]
            data = {c: v for c, v in zip(present, row[1:]) if v is not None}
            con.execute(
                "UPDATE app_metric_lifetime SET lifetime=? WHERE application_id=?",
                (json.dumps(data), app_id),
            )
            print(f"converted {app_id}: {len(data)} field(s) -> lifetime JSON")
        con.commit()
        for c in present:
            con.execute(f"ALTER TABLE app_metric_lifetime DROP COLUMN {c}")
        con.commit()
        print(f"dropped {len(present)} old column(s)")
    finally:
        con.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "prod/gpusymbiosis.db")
