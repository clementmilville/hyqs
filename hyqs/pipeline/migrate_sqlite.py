"""One-shot copy of the legacy sqlite pipeline db into the shared Postgres.

The pipeline store moved from a local ``data/pipeline.db`` file to a networked
Postgres so workers on many hosts can share it. This script carries the existing
history across — projects, epics, jobs, the agent roster, usage and the per-step
event timeline — preserving primary keys so existing references stay valid.

Usage::

    # uses HYQS_DB_URL + data_dir/pipeline.db from the environment/.env
    hyqs-migrate-sqlite

    # or be explicit
    hyqs-migrate-sqlite --sqlite ./data/pipeline.db \
        --db-url postgresql://hyqs:hyqs@localhost:5432/hyqs

Re-runnable with ``--reset`` (truncates the Postgres pipeline tables first). The
transient fleet/lock tables (``workers``, ``merge_locks``) are intentionally not
copied — they're live runtime state, rebuilt by the workers themselves.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import psycopg

from .store import JobStore

# FK-safe insert order. workers/merge_locks are ephemeral and skipped.
_TABLES = ["projects", "epics", "jobs", "project_agents", "usage", "job_events", "meta"]
# Tables whose id is an IDENTITY sequence to bump after we insert explicit ids.
_IDENTITY_TABLES = ["projects", "epics", "jobs", "project_agents", "usage", "job_events"]


def _sqlite_columns(scon: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in scon.execute(f"PRAGMA table_info({table})")]


def _pg_columns(pcon: psycopg.Connection, table: str) -> dict[str, str]:
    rows = pcon.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s",
        (table,),
    ).fetchall()
    return {r["column_name"]: r["data_type"] for r in rows}


def _copy_table(scon: sqlite3.Connection, pcon: psycopg.Connection, table: str) -> int:
    src_cols = _sqlite_columns(scon, table)
    if not src_cols:
        return 0
    pg_types = _pg_columns(pcon, table)
    cols = [c for c in src_cols if c in pg_types]  # only columns both sides share
    bool_cols = {c for c in cols if pg_types[c] == "boolean"}

    scon.row_factory = sqlite3.Row
    rows = scon.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
    if not rows:
        return 0

    values = []
    for r in rows:
        rec = []
        for c in cols:
            v = r[c]
            if c in bool_cols and v is not None:
                v = bool(v)  # sqlite stored 0/1; Postgres wants a real boolean
            rec.append(v)
        values.append(rec)

    placeholders = ", ".join(["%s"] * len(cols))
    collist = ", ".join(cols)
    # meta is keyed by `key`; don't clobber the schema_version row we just seeded.
    conflict = " ON CONFLICT(key) DO NOTHING" if table == "meta" else ""
    with pcon.cursor() as cur:
        cur.executemany(
            f"INSERT INTO {table} ({collist}) VALUES ({placeholders}){conflict}", values
        )
    return len(values)


def _reset_identity(pcon: psycopg.Connection, table: str) -> None:
    """Advance the IDENTITY sequence past the explicit ids we just inserted."""
    pcon.execute(
        f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
        f"COALESCE((SELECT MAX(id) FROM {table}), 1), "
        f"(SELECT MAX(id) FROM {table}) IS NOT NULL)"
    )


def _counts(con, tables: list[str], *, pg: bool) -> dict[str, int]:
    out = {}
    for t in tables:
        out[t] = con.execute(f"SELECT COUNT(*) {'AS n ' if pg else ''}FROM {t}").fetchone()[
            "n" if pg else 0
        ]
    return out


def migrate(sqlite_path: Path, db_url: str, *, reset: bool) -> int:
    if not sqlite_path.exists():
        print(f"error: sqlite db not found: {sqlite_path}", file=sys.stderr)
        return 2

    # Instantiate the store once so the canonical Postgres schema exists.
    JobStore(db_url).close()

    scon = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    scon.row_factory = sqlite3.Row
    with psycopg.connect(db_url, row_factory=psycopg.rows.dict_row) as pcon:
        # Guard against a double migration unless the caller asked to reset.
        existing = pcon.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
        if existing and not reset:
            print(
                f"error: target already has {existing} job(s). "
                f"Re-run with --reset to truncate and re-migrate.",
                file=sys.stderr,
            )
            return 3
        if reset:
            pcon.execute(
                "TRUNCATE projects, epics, jobs, project_agents, usage, job_events, "
                "workers, merge_locks RESTART IDENTITY CASCADE"
            )

        copied = {}
        for table in _TABLES:
            copied[table] = _copy_table(scon, pcon, table)
        for table in _IDENTITY_TABLES:
            _reset_identity(pcon, table)
        pcon.commit()

        # --- verify ----------------------------------------------------------
        src = _counts(scon, _TABLES, pg=False)
        dst = _counts(pcon, _TABLES, pg=True)
        src_cost = scon.execute("SELECT COALESCE(SUM(cost_usd),0) FROM usage").fetchone()[0]
        dst_cost = pcon.execute("SELECT COALESCE(SUM(cost_usd),0) AS c FROM usage").fetchone()["c"]
        # The store seeds a `schema_version` row into meta, so target meta legitimately
        # carries one extra row beyond what's copied — verify by key presence instead.
        src_meta = {r[0] for r in scon.execute("SELECT key FROM meta")}
        dst_meta = {r["key"] for r in pcon.execute("SELECT key FROM meta")}

    scon.close()

    def _row_ok(t: str) -> bool:
        return src_meta <= dst_meta if t == "meta" else src[t] == dst[t]

    print("migration complete:")
    for t in _TABLES:
        flag = "" if _row_ok(t) else "  <-- MISMATCH"
        print(f"  {t:16} sqlite={src[t]:<6} postgres={dst[t]:<6} (copied {copied[t]}){flag}")
    print(f"  usage cost      sqlite=${src_cost:.4f}  postgres=${dst_cost:.4f}")

    ok = all(_row_ok(t) for t in _TABLES) and round(src_cost, 4) == round(dst_cost, 4)
    if not ok:
        print("\nWARNING: source/target counts differ — inspect before cutting over.", file=sys.stderr)
        return 1
    print("\nAll counts and usage totals match. ✅")
    return 0


def main() -> None:
    from hyqs.config import Config

    config = Config.from_env()
    parser = argparse.ArgumentParser(description="Migrate the sqlite pipeline db into Postgres.")
    parser.add_argument(
        "--sqlite", type=Path, default=config.data_dir / "pipeline.db",
        help="path to the legacy sqlite pipeline db (default: <data_dir>/pipeline.db)",
    )
    parser.add_argument(
        "--db-url", default=config.db_url,
        help="target Postgres DSN (default: HYQS_DB_URL / DATABASE_URL)",
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="truncate the Postgres pipeline tables before migrating (re-runnable)",
    )
    args = parser.parse_args()
    if not args.db_url:
        parser.error("no target Postgres DSN — set HYQS_DB_URL or pass --db-url")
    raise SystemExit(migrate(args.sqlite, args.db_url, reset=args.reset))


if __name__ == "__main__":
    main()
