"""Replay saved reports with the prompts in this working tree and score both the same way.

The improvement loop: change a prompt, then replay reports whose outcomes are
already known. Each saved market snapshot (never any later price) goes to the
configured model with the current prompts; the new report's plan is judged by
outcomes.py on the same 5-minute candles that judged the original. The output
compares the original and candidate side by side.

It spends model allowance, so --live is required. Point APP_DB_PATH at a copy
of the desktop database, never the live one, e.g.:

    sqlite3 "$HOME/Library/Application Support/txinTrade/data/trade_helper.sqlite3" ".backup /tmp/replay.sqlite3"
    APP_DB_PATH=/tmp/replay.sqlite3 APP_DESKTOP=0 uv run python scripts/replay_outcomes.py --live --limit 10 --output /tmp/replay.json
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from evaluate_agent_strategy import evaluate_case

from trade_helper.db import connect, init_db
from trade_helper.market import fetch_candles_range
from trade_helper.outcomes import RESOLUTION, STEP, items_for, new_state, settle, step
from trade_helper.support_levels import order_book_evidence
from trade_helper.timeframes import candle_open


def judge(item: dict, cache: dict) -> dict:
    """Settle one item on its whole window of 5-minute candles, fetched once per market and window."""
    state = new_state(item)
    start = datetime.fromisoformat(state["through"])
    deadline = candle_open(datetime.fromisoformat(item["deadline"]), RESOLUTION) + STEP
    end = min(candle_open(datetime.now(UTC), RESOLUTION), deadline)
    key = (item["market_id"], start.isoformat(), end.isoformat())
    if key not in cache:
        cache[key] = fetch_candles_range(item["market_id"], RESOLUTION, start, end) if end > start else []
    return settle(item, step(item, state, cache[key]))


def saved_cases(*, limit: int, kind: str | None, market: str | None) -> list[dict]:
    query = ["""SELECT a.id, a.request_json, a.snapshot_json, a.report_json FROM analyses a
                WHERE a.status='completed' AND a.snapshot_json IS NOT NULL AND EXISTS (
                  SELECT 1 FROM analysis_outcomes o WHERE o.analysis_id=a.id AND o.status='resolved'
                    AND o.kind<>'none')"""]
    params: list = []
    if kind:
        query.append(" AND json_extract(a.request_json,'$.kind')=?")
        params.append(kind)
    if market:
        query.append(" AND json_extract(a.report_json,'$.market_id')=?")
        params.append(market)
    query.append(" ORDER BY a.created_at DESC LIMIT ?")
    params.append(limit)
    with connect(readonly=True) as db:
        rows = db.execute("".join(query), params).fetchall()
        cases = []
        for row in rows:
            outcomes = {item["item_key"]: dict(item) for item in db.execute(
                "SELECT item_key, kind, result, r_multiple FROM analysis_outcomes WHERE analysis_id=? "
                "AND status='resolved' AND kind<>'none'", (row["id"],)).fetchall()}
            snapshot = json.loads(row["snapshot_json"])
            quote = snapshot["quote"]
            # The worker adds these after freezing the snapshot; rebuild them the same way.
            quote.setdefault("order_book_evidence", order_book_evidence(snapshot.get("order_book"),
                                                                        Decimal(quote["price"])))
            cases.append({"id": row["id"], "source": "saved_report_replay",
                          "request": json.loads(row["request_json"]), "snapshot": snapshot,
                          "original": outcomes})
    return cases


def totals(results: list[dict]) -> dict:
    trades = [item for item in results if item and item.get("result") in {"win", "loss", "expired"}]
    r = [Decimal(item["r_multiple"]) for item in trades if item.get("r_multiple")]
    return {"wins": sum(item["result"] == "win" for item in trades),
            "losses": sum(item["result"] == "loss" for item in trades),
            "average_r": str((sum(r) / len(r)).quantize(Decimal("0.01"))) if r else None,
            "total_r": str(sum(r)) if r else None}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--live", action="store_true", help="Explicitly use the configured model provider")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--kind", choices=("market", "positions"))
    parser.add_argument("--market", help="Only this market id, e.g. binance:perp:ETHUSDT")
    parser.add_argument("--response-locale", choices=("en-US", "zh-TW"), default="zh-TW")
    parser.add_argument("--output", type=Path, required=True, help="Local results JSON")
    args = parser.parse_args()
    if not args.live:
        parser.error("Replaying calls the model and spends allowance; pass --live to run it")
    init_db()
    cases = saved_cases(limit=args.limit, kind=args.kind, market=args.market)
    cache: dict = {}
    rows = []
    for case in cases:
        evaluated = evaluate_case(case, response_locale=args.response_locale)
        candidate = {}
        if evaluated.get("contract_valid"):
            for item in items_for(evaluated["report"]):
                state = judge(item, cache)
                candidate[item["key"]] = {"kind": item["kind"], "action": item.get("action"),
                                          "result": state.get("result"), "r_multiple": state.get("r_multiple")}
        rows.append({"analysis_id": case["id"], "original": case["original"], "candidate": candidate,
                     "error": None if evaluated.get("contract_valid") else evaluated.get("error_type")})
        print(f"{case['id']}: " + ", ".join(
            f"{key} {case['original'].get(key, {}).get('result')}→{value['result']}"
            for key, value in candidate.items()) or "no plan", file=sys.stderr)
    summary = {"cases": len(rows), "errors": sum(1 for row in rows if row["error"]),
               "original": totals([item for row in rows for item in row["original"].values()]),
               "candidate": totals([item for row in rows for item in row["candidate"].values()]),
               "changed": sum(1 for row in rows for key, item in row["candidate"].items()
                              if row["original"].get(key, {}).get("result") != item["result"])}
    args.output.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
