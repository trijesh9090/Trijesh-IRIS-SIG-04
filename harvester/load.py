"""Step 5: write records into iris_staging.signal_ingest. Safe to run again: the same notice updates the same row."""
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb

SCHEMA_SQL = Path(__file__).resolve().parent.parent / "sql" / "schema.sql"

COLS = ["source", "source_record_id", "signal_type", "procedure_step", "title", "published_at", "date_source",
        "notice_url", "source_page_url", "link_type", "document_urls", "ags", "municipality", "district", "region",
        "geo_status", "parser", "confidence", "quality_flags", "content_hash", "run_id"]
MUTABLE = [c for c in COLS if c not in ("source", "source_record_id")]

# The "old" part reads the stored hash and type before the insert, so we can tell new, changed and unchanged apart.
# geom stays NULL when there is no valid point. updated_at only moves when the content or the type really changed,
# last_seen_at moves on every run, and first_seen_at / created_at are never touched after the first insert.
UPSERT = f"""
WITH old AS (SELECT content_hash, signal_type FROM iris_staging.signal_ingest
             WHERE source = %(source)s AND source_record_id = %(source_record_id)s)
INSERT INTO iris_staging.signal_ingest ({", ".join(COLS)}, geom)
VALUES ({", ".join(f"%({c})s" for c in COLS)},
        CASE WHEN %(x)s::float8 IS NULL THEN NULL ELSE ST_SetSRID(ST_MakePoint(%(x)s, %(y)s), 25832) END)
ON CONFLICT (source, source_record_id) DO UPDATE SET
    {", ".join(f"{c} = EXCLUDED.{c}" for c in MUTABLE)},
    geom = EXCLUDED.geom,
    last_seen_at = now(),
    updated_at = CASE WHEN (signal_ingest.content_hash, signal_ingest.signal_type)
                           IS DISTINCT FROM (EXCLUDED.content_hash, EXCLUDED.signal_type)
                      THEN now() ELSE signal_ingest.updated_at END
RETURNING (SELECT content_hash FROM old), (SELECT signal_type FROM old)
"""


def ensure_schema(conn: psycopg.Connection):
    """Create the schema, table, checks and indexes if they don't exist yet.

    schema.sql only uses "IF NOT EXISTS", so running it before every load does no harm.

    Args:
        conn: an open database connection.
    """
    conn.execute(SCHEMA_SQL.read_text())


def upsert(conn: psycopg.Connection, rows: list[dict]) -> dict:
    """Insert new records and update known ones, all in one transaction.

    Every row gets its own savepoint, so a row the database refuses is skipped and reported instead
    of rolling back the whole load.

    Args:
        conn: an open database connection.
        rows: records from records.to_row().

    Returns:
        Counts and lists: {"inserted", "updated_changed", "unchanged": numbers,
        "rejected": [{"source_record_id", "error"}], "new_ids": [...], "changed_ids": [...]}.
        The dashboard uses new_ids and changed_ids to list what is new in this run.
    """
    stats = {"inserted": 0, "updated_changed": 0, "unchanged": 0, "rejected": [], "new_ids": [], "changed_ids": []}
    with conn.transaction(), conn.cursor() as cursor:
        for row in rows:
            # the two list columns are stored as jsonb
            params = dict(row)
            params["document_urls"] = Jsonb(row["document_urls"])
            params["quality_flags"] = Jsonb(row["quality_flags"])
            try:
                with conn.transaction():  # savepoint for this one row
                    old_hash, old_type = cursor.execute(UPSERT, params).fetchone()
            except (psycopg.errors.IntegrityError, psycopg.errors.DataError) as e:
                error = str(e).splitlines()[0]
                stats["rejected"].append({"source_record_id": row["source_record_id"], "error": error})
                continue

            # nothing stored before = new; same hash and type = unchanged; anything else = changed
            if old_hash is None:
                stats["inserted"] += 1
                stats["new_ids"].append(row["source_record_id"])
            elif old_hash == row["content_hash"] and old_type == row["signal_type"]:
                stats["unchanged"] += 1
            else:
                stats["updated_changed"] += 1
                stats["changed_ids"].append(row["source_record_id"])
    return stats
