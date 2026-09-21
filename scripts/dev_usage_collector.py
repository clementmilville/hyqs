#!/usr/bin/env python3
"""Claude Code Stop hook: record this interactive dev session's usage in Postgres.

The pipeline records token/cost usage for its own agent runs (plan/build/fix/
review). Interactive Claude Code sessions were invisible to that dashboard — this
hook closes the gap. On each Stop it reads the session transcript, sums per-model
token usage, computes cost from per-model pricing (input/output plus the cache
write-5m, write-1h, and read tiers), and upserts a single row into the pipeline
``usage`` table as ``source='dev-session'`` with ``job_id=NULL`` (the
"Non-pipeline" bucket in usage_summary).

One row per session: a small state file remembers the row id, so repeated Stop
events update the same row with cumulative totals rather than double-counting.

Wire it up as a Stop hook in .claude/settings.json; it reads the hook JSON on
stdin (needs ``transcript_path`` and ``session_id``). Never fails the turn — any
error is swallowed with a message on stderr.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from hyqs.pipeline.pricing import PRICING, _price
except ImportError:
    # Inline fallback so this hook works when hyqs package is not on sys.path.
    # USD per 1M tokens. Cache tiers follow the documented multipliers off input:
    # write-5m = 1.25x, write-1h = 2x, read = 0.1x.
    PRICING = {
        "claude-opus-4-8":   {"in": 5.0, "out": 25.0, "w5": 6.25, "w1h": 10.0, "read": 0.50},
        "claude-opus-4-7":   {"in": 5.0, "out": 25.0, "w5": 6.25, "w1h": 10.0, "read": 0.50},
        "claude-sonnet-4-6": {"in": 3.0, "out": 15.0, "w5": 3.75, "w1h": 6.0,  "read": 0.30},
        "claude-haiku-4-5":  {"in": 1.0, "out": 5.0,  "w5": 1.25, "w1h": 2.0,  "read": 0.10},
    }

    def _price(model: str) -> dict | None:
        """Longest-prefix match so dated snapshots (…-4-5-20251001) resolve."""
        best = None
        for name, p in PRICING.items():
            if model.startswith(name) and (best is None or len(name) > len(best[0])):
                best = (name, p)
        return best[1] if best else None

_STATE_DIR = Path.home() / ".claude" / "hyqs-usage-state"


def _dsn() -> str:
    dsn = os.environ.get("HYQS_DB_URL", "").strip() or os.environ.get("DATABASE_URL", "").strip()
    if dsn:
        return dsn
    # Fall back to the DSN in the project's .env (hook cwd is the repo root).
    env = Path(".env")
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            for key in ("HYQS_DB_URL=", "DATABASE_URL="):
                if line.startswith(key):
                    return line[len(key):].strip().strip('"').strip("'")
    return "postgresql://hyqs:hyqs@localhost:5432/hyqs"


def _summarize(transcript_path: str) -> dict | None:
    """Sum per-model token usage + cost across the whole transcript."""
    tot = {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0, "cost": 0.0}
    model = ""
    seen = False
    with open(transcript_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("type") != "assistant":
                continue
            msg = obj.get("message") or {}
            u = msg.get("usage")
            if not u:
                continue
            seen = True
            model = msg.get("model") or model
            it = int(u.get("input_tokens", 0) or 0)
            ot = int(u.get("output_tokens", 0) or 0)
            cr = int(u.get("cache_read_input_tokens", 0) or 0)
            cc = int(u.get("cache_creation_input_tokens", 0) or 0)
            split = u.get("cache_creation") or {}
            w5 = int(split.get("ephemeral_5m_input_tokens", 0) or 0)
            w1h = int(split.get("ephemeral_1h_input_tokens", 0) or 0)
            if not (w5 or w1h):  # no breakdown → treat all cache writes as 5m
                w5 = cc
            tot["input"] += it
            tot["output"] += ot
            tot["cache_creation"] += cc
            tot["cache_read"] += cr
            p = _price(model)
            if p:
                tot["cost"] += (
                    it * p["in"] + ot * p["out"] + cr * p["read"]
                    + w5 * p["w5"] + w1h * p["w1h"]
                ) / 1_000_000
    if not seen:
        return None
    tot["model"] = model
    return tot


def _upsert(dsn: str, session_id: str, t: dict) -> None:
    import psycopg

    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = _STATE_DIR / f"{session_id}.json"
    row_id = None
    if state.exists():
        try:
            row_id = json.loads(state.read_text()).get("usage_id")
        except ValueError:
            row_id = None
    now = datetime.now(timezone.utc).isoformat()
    with psycopg.connect(dsn, autocommit=True) as conn:
        if row_id is not None:
            cur = conn.execute(
                "UPDATE usage SET model=%s, input_tokens=%s, output_tokens=%s, "
                "cache_creation_tokens=%s, cache_read_tokens=%s, cost_usd=%s "
                "WHERE id=%s",
                (t["model"], t["input"], t["output"], t["cache_creation"],
                 t["cache_read"], round(t["cost"], 6), row_id),
            )
            if cur.rowcount:
                return  # updated the existing session row
        row = conn.execute(
            "INSERT INTO usage(job_id, source, model, provider, input_tokens, "
            "output_tokens, cache_creation_tokens, cache_read_tokens, cost_usd, created_at) "
            "VALUES (NULL, 'dev-session', %s, 'claude', %s, %s, %s, %s, %s, %s) RETURNING id",
            (t["model"], t["input"], t["output"], t["cache_creation"],
             t["cache_read"], round(t["cost"], 6), now),
        ).fetchone()
    state.write_text(json.dumps({"usage_id": row[0]}))


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return
    transcript = payload.get("transcript_path")
    session_id = payload.get("session_id")
    if not transcript or not session_id or not os.path.exists(transcript):
        return
    t = _summarize(transcript)
    if not t or (t["input"] + t["output"] + t["cache_creation"] + t["cache_read"]) == 0:
        return
    _upsert(_dsn(), session_id, t)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - a hook must never break the session
        print(f"dev_usage_collector: {exc}", file=sys.stderr)
