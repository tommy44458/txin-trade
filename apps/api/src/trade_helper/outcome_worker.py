"""Background reconciliation: judge each finished report against later candles.

Completed reports, including those from before this feature, are enrolled once
per rules version. Pending items then advance over newly closed 5-minute candles
a few days at a time until the rules in outcomes.py settle them.
"""

import json
import logging
import sys
import time
from datetime import UTC, datetime, timedelta

import httpx

from .config import assert_local_mode
from .db import connect, init_db, new_id, utc_now
from .market import fetch_candles_range
from .outcome_upload import upload_once
from .outcomes import RESOLUTION, RULES_VERSION, STEP, due, items_for, new_state, settle, step
from .timeframes import candle_open

_LOG = logging.getLogger(__name__)
ENROLL_BATCH = 200
CHECK_BATCH = 40
FETCH_SPAN = timedelta(days=3)
RECHECK = timedelta(minutes=15)
RETRY = timedelta(hours=1)
MAX_FAILURES = 6
IDLE_SECONDS = 300


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def enroll(db, now: datetime) -> int:
    """Create outcome rows for completed reports that have none under this rules version."""
    rows = db.execute(
        """SELECT id, user_id, report_json FROM analyses a
           WHERE status='completed' AND report_json IS NOT NULL
             AND NOT EXISTS (SELECT 1 FROM analysis_outcomes o
                             WHERE o.analysis_id=a.id AND o.rules_version=?)
           ORDER BY completed_at LIMIT ?""", (RULES_VERSION, ENROLL_BATCH)).fetchall()
    stamp = utc_now()
    for row in rows:
        try:
            items = items_for(json.loads(row["report_json"]))
        except (ValueError, TypeError, KeyError):
            items = []
        # A report with nothing to judge keeps one marker, so it is not scanned again.
        entries = [(item, "pending", None) for item in items] or [
            ({"key": "none", "kind": "none"}, "resolved", "not_applicable")]
        for item, status, result in entries:
            db.execute(
                """INSERT OR IGNORE INTO analysis_outcomes
                   (id,user_id,analysis_id,item_key,kind,rules_version,status,result,
                    item_json,state_json,next_check_at,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (new_id("out"), row["user_id"], row["id"], item["key"], item["kind"], RULES_VERSION,
                 status, result, json.dumps(item), json.dumps(new_state(item) if status == "pending" else {}),
                 _iso(now) if status == "pending" else None, stamp, stamp))
    return len(rows)


def advance(item: dict, state: dict, now: datetime, fetch=fetch_candles_range) -> tuple[dict, bool]:
    """Feed one batch of newly closed candles; True when more are already available."""
    available = candle_open(now, RESOLUTION)  # Only fully closed candles.
    start = datetime.fromisoformat(state["through"])
    end = min(available, candle_open(due(item, state), RESOLUTION) + STEP, start + FETCH_SPAN)
    if end > start:
        candles = fetch(item["market_id"], RESOLUTION, start, end)
        state = step(item, state, candles)
    state = settle(item, state)
    return state, not state["done"] and end < min(available, due(item, state))


def run_once(now: datetime | None = None, fetch=fetch_candles_range) -> bool:
    now = now or datetime.now(UTC)
    with connect() as db:
        enrolled = enroll(db, now)
        db.commit()
        rows = db.execute(
            """SELECT id, item_json, state_json FROM analysis_outcomes
               WHERE status='pending' AND rules_version=? AND (next_check_at IS NULL OR next_check_at<=?)
               ORDER BY next_check_at LIMIT ?""", (RULES_VERSION, _iso(now), CHECK_BATCH)).fetchall()
    more = False
    for row in rows:
        item, state = json.loads(row["item_json"]), json.loads(row["state_json"])
        try:
            state, behind = advance(item, state, now, fetch)
            state.pop("failures", None)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            # Market data can be briefly unavailable; a market that never returns ends the item.
            _LOG.warning("outcome_check_failed id=%s exception=%s", row["id"], type(exc).__name__)
            state = state | {"failures": state.get("failures", 0) + 1}
            behind = False
        failed = state.get("failures", 0) >= MAX_FAILURES
        status = "resolved" if state["done"] else "unavailable" if failed else "pending"
        later = now if behind else now + (RETRY if state.get("failures") else RECHECK)
        more = more or behind
        with connect() as db:
            db.execute(
                """UPDATE analysis_outcomes SET status=?, result=?, r_multiple=?, state_json=?,
                   next_check_at=?, resolved_at=?, updated_at=? WHERE id=?""",
                (status, state.get("result"), state.get("r_multiple"), json.dumps(state),
                 None if status != "pending" else _iso(later),
                 state.get("resolved_at") if state["done"] else None, utc_now(), row["id"]))
            db.commit()
    try:
        upload_once()
    except Exception as exc:  # noqa: BLE001 - sharing retries next cycle; judging never depends on it
        _LOG.warning("outcome_upload_failed exception=%s", type(exc).__name__)
    return bool(enrolled == ENROLL_BATCH or more)


def main() -> None:
    assert_local_mode()
    init_db()
    while True:
        try:
            worked = run_once()
        except Exception as exc:  # noqa: BLE001 - a bad cycle must not stop the service
            _LOG.error("outcome_cycle_failed exception=%s", type(exc).__name__)
            worked = False
        if "--once" in sys.argv:
            return
        time.sleep(1 if worked else IDLE_SECONDS)


if __name__ == "__main__":
    main()
