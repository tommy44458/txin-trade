"""The AI track record: reconciled outcomes of finished reports, read-only."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query

from .config import local_user_id
from .db import connect
from .outcomes import RULES_VERSION
from .prompts import task_prompt_version

router = APIRouter(prefix="/api/v1", tags=["Outcomes"])
PERIODS = {"30d": timedelta(days=30), "90d": timedelta(days=90), "all": None}
# Report fields the track record can be grouped by.
GROUPS = {
    "market": "$.market_id", "timeframe": "$.timeframe",
    "risk": "$.preference_assessment.risk_tolerance", "style": "$.preference_assessment.trading_style",
    "model": "$.analysis_execution.model", "prompt": "$.analysis_execution.prompt_version",
}
TRADE_RESULTS = {"win", "loss", "expired"}


def current_versions() -> list[str]:
    """The strategy prompts this app version writes reports with."""
    return sorted({task_prompt_version("strategy_market"), task_prompt_version("strategy_positions")})


def _rows(db, *, since: datetime | None, market_id: str | None = None, timeframe: str | None = None,
          analysis_id: str | None = None, versions: list[str] | None = None):
    query = ["""SELECT o.id, o.analysis_id, o.item_key, o.kind, o.status, o.result, o.r_multiple,
                       o.item_json, o.state_json, o.resolved_at, a.created_at,
                       json_extract(a.request_json,'$.kind') AS report_kind""",
             *(f", json_extract(a.report_json,'{path}') AS {name}" for name, path in GROUPS.items()),
             """ FROM analysis_outcomes o JOIN analyses a ON a.id=o.analysis_id AND a.user_id=o.user_id
                 WHERE o.user_id=? AND o.rules_version=? AND o.kind<>'none'"""]
    params: list = [local_user_id(), RULES_VERSION]
    if since:
        query.append(" AND a.created_at>=?")
        params.append(since.isoformat())
    if market_id:
        query.append(" AND json_extract(a.report_json,'$.market_id')=?")
        params.append(market_id)
    if timeframe:
        query.append(" AND json_extract(a.report_json,'$.timeframe')=?")
        params.append(timeframe)
    if analysis_id:
        query.append(" AND o.analysis_id=?")
        params.append(analysis_id)
    if versions:
        query.append(f" AND json_extract(a.report_json,'$.analysis_execution.prompt_version') IN "
                     f"({','.join('?' * len(versions))})")
        params.extend(versions)
    query.append(" ORDER BY a.created_at DESC, o.item_key")
    return db.execute("".join(query), params).fetchall()


def _mean(values: list[Decimal]) -> str | None:
    return str((sum(values) / len(values)).quantize(Decimal("0.01"))) if values else None


def summarize(rows) -> dict:
    """Counts and R statistics; rates only over settled trades, never pending ones."""
    def count(kind, result=None):
        return sum(1 for row in rows if row["kind"] == kind and (result is None or row["result"] == result))

    trades = [row for row in rows if row["kind"] == "entry" and row["result"] in TRADE_RESULTS]
    trade_r = [Decimal(row["r_multiple"]) for row in trades if row["r_multiple"]]
    wins, losses = count("entry", "win"), count("entry", "loss")
    holds = [row for row in rows if row["kind"] == "hold" and row["status"] == "resolved"]
    hold_r = [Decimal(row["r_multiple"]) for row in holds if row["r_multiple"]]
    closes = count("close", "good_close") + count("close", "early_close")
    observed = [json.loads(row["state_json"]) for row in rows if row["kind"] == "observe" and row["result"]]
    return {
        "total": len(rows), "pending": sum(1 for row in rows if row["status"] == "pending"),
        "entries": {"wins": wins, "losses": losses, "expired": count("entry", "expired"),
                    "not_triggered": count("entry", "not_triggered"),
                    "unverifiable": count("entry", "unverifiable"),
                    "win_rate": str(round(wins / (wins + losses) * 100, 1)) if wins + losses else None,
                    "average_r": _mean(trade_r), "total_r": str(sum(trade_r)) if trade_r else None},
        "holds": {"target_hit": count("hold", "target_hit"), "invalidated": count("hold", "invalidated"),
                  "expired": count("hold", "expired"), "average_r": _mean(hold_r)},
        "closes": {"good": count("close", "good_close"), "early": count("close", "early_close"),
                   "good_rate": str(round(count("close", "good_close") / closes * 100, 1)) if closes else None},
        "stand_aside": {"count": len(observed),
                        "average_max_up_pct": _mean([Decimal(item["max_up_pct"]) for item in observed]),
                        "average_max_down_pct": _mean([Decimal(item["max_down_pct"]) for item in observed])},
    }


def _public(row) -> dict:
    item, state = json.loads(row["item_json"]), json.loads(row["state_json"])
    return {
        "id": row["id"], "analysis_id": row["analysis_id"], "item_key": row["item_key"],
        "kind": row["kind"], "status": row["status"], "result": row["result"],
        "r_multiple": row["r_multiple"], "resolved_at": row["resolved_at"], "created_at": row["created_at"],
        "report_kind": row["report_kind"], "market_id": row["market"], "timeframe": row["timeframe"],
        "side": item.get("side"), "action": item.get("action"),
        "entry": item.get("entry"), "stop": item.get("stop"), "target": item.get("target"),
        "reference": item.get("reference"), "invalidation": item.get("invalidation"),
        "entered_at": state.get("entered_at"), "exit_price": state.get("exit_price"),
        "fill": state.get("fill"), "trigger": item.get("trigger"),
        "max_up_pct": state.get("max_up_pct"), "max_down_pct": state.get("max_down_pct"),
        "end_pct": state.get("end_pct"),
    }


@router.get("/outcomes/summary")
def outcome_summary(period: str = Query("all"), group: str | None = Query(None),
                    market_id: str | None = Query(None), timeframe: str | None = Query(None),
                    version: str = Query("all")):
    if period not in PERIODS or (group is not None and group not in GROUPS) or version not in {"all", "current"}:
        raise HTTPException(422, {"code": "invalid_filter"})
    since = datetime.now(UTC) - PERIODS[period] if PERIODS[period] else None
    versions = current_versions() if version == "current" else None
    with connect(readonly=True) as db:
        rows = _rows(db, since=since, market_id=market_id, timeframe=timeframe, versions=versions)
    result = {"rules_version": RULES_VERSION, "period": period, "overall": summarize(rows),
              "version": version, "current_versions": current_versions(),
              "assumptions": {"resolution": "5m", "same_candle": "loss", "fees_and_slippage": "excluded"}}
    if group:
        buckets: dict = {}
        for row in rows:
            buckets.setdefault(row[group] or "unknown", []).append(row)
        result["groups"] = [{"key": key, **summarize(items)} for key, items in
                            sorted(buckets.items(), key=lambda pair: -len(pair[1]))]
    return result


@router.get("/outcomes")
def outcome_list(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                 period: str = Query("all"), version: str = Query("all"), judged: bool = Query(False)):
    if period not in PERIODS or version not in {"all", "current"}:
        raise HTTPException(422, {"code": "invalid_filter"})
    since = datetime.now(UTC) - PERIODS[period] if PERIODS[period] else None
    with connect(readonly=True) as db:
        rows = _rows(db, since=since, versions=current_versions() if version == "current" else None)
    if judged:  # Leave out plans whose condition cannot be checked.
        rows = [row for row in rows if row["result"] != "unverifiable"]
    return {"items": [_public(row) for row in rows[offset:offset + limit]], "total": len(rows)}


@router.get("/analyses/{analysis_id}/outcomes")
def analysis_outcomes(analysis_id: str):
    with connect(readonly=True) as db:
        rows = _rows(db, since=None, analysis_id=analysis_id)
    return {"items": [_public(row) for row in rows]}
