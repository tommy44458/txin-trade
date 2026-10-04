"""Share settled outcomes with the txinTrade cloud, only when the user turns it on.

Each record is labels, versions and ratios: what kind of call it was, how it
ended, in R or percent, and which model and prompt made it. Report text, entry
and position prices, sizes, account equity and exchange details never leave
the computer. Turning sharing off stops uploads; deleting removes every shared
record from the cloud and turns sharing off.
"""

import json
import logging

import httpx
from fastapi import APIRouter, HTTPException

from .cloud_account import CloudAccountError, cloud_origin, session_record
from .config import local_user_id
from .db import connect, utc_now
from .local_settings import patch_preferences, preferences
from .outcomes import RULES_VERSION
from .product_version import product_version

_LOG = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/outcomes/sharing", tags=["Outcomes"])
BATCH = 100
_FIELDS = {"risk_tolerance": "$.preference_assessment.risk_tolerance",
           "trading_style": "$.preference_assessment.trading_style",
           "market_id": "$.market_id", "timeframe": "$.timeframe",
           "model": "$.analysis_execution.model", "provider": "$.analysis_execution.provider",
           "prompt_version": "$.analysis_execution.prompt_version",
           "policy_version": "$.analysis_execution.prompt_bundle.policy_version"}


def sharing_enabled() -> bool:
    return preferences().get("share_outcomes") is True


def _record(row) -> dict:
    item, state = json.loads(row["item_json"]), json.loads(row["state_json"])
    version = product_version()
    return {
        "outcome_id": row["id"], "rules_version": row["rules_version"], "kind": row["kind"],
        "action": item.get("action"), "side": item.get("side"), "result": row["result"],
        "r_multiple": row["r_multiple"], "max_up_pct": state.get("max_up_pct"),
        "max_down_pct": state.get("max_down_pct"), "end_pct": state.get("end_pct"),
        **{name: row[name] for name in _FIELDS},
        "app_version": version if version.count(".") == 2 else None,
        "report_day": row["created_at"][:10], "resolved_at": row["resolved_at"],
    }


def upload_once(post=httpx.post) -> int:
    """Send settled outcomes not yet shared; nothing happens unless sharing is on and signed in."""
    if not sharing_enabled():
        return 0
    try:
        session = session_record()
    except CloudAccountError:
        return 0
    if not session:
        return 0
    columns = "".join(f", json_extract(a.report_json,'{path}') AS {name}" for name, path in _FIELDS.items())
    with connect() as db:
        rows = db.execute(
            f"""SELECT o.id, o.rules_version, o.kind, o.result, o.r_multiple, o.item_json, o.state_json,
                       o.resolved_at, a.created_at{columns}
                FROM analysis_outcomes o JOIN analyses a ON a.id=o.analysis_id AND a.user_id=o.user_id
                WHERE o.user_id=? AND o.status='resolved' AND o.kind<>'none' AND o.uploaded_at IS NULL
                  AND o.rules_version=? ORDER BY o.resolved_at LIMIT ?""",
            (local_user_id(), RULES_VERSION, BATCH)).fetchall()
    if not rows:
        return 0
    response = post(f"{cloud_origin()}/api/v1/outcomes", json={"records": [_record(row) for row in rows]},
                    headers={"Authorization": f"Bearer {session['session_token']}"}, timeout=20)
    response.raise_for_status()
    with connect() as db:
        stamp = utc_now()
        for row in rows:
            db.execute("UPDATE analysis_outcomes SET uploaded_at=? WHERE id=?", (stamp, row["id"]))
        db.commit()
    return len(rows)


@router.get("")
def sharing_status():
    with connect(readonly=True) as db:
        shared = db.execute("SELECT COUNT(*) AS n FROM analysis_outcomes WHERE uploaded_at IS NOT NULL "
                            "AND user_id=?", (local_user_id(),)).fetchone()["n"]
    return {"enabled": sharing_enabled(), "shared": shared}


@router.post("/delete")
def delete_shared():
    """Remove every shared outcome from the cloud and stop sharing."""
    patch_preferences({"share_outcomes": False})
    try:
        session = session_record()
    except CloudAccountError:
        session = None
    if not session:
        raise HTTPException(409, {"code": "cloud_sign_in_required"})
    try:
        response = httpx.request("DELETE", f"{cloud_origin()}/api/v1/outcomes",
                           headers={"Authorization": f"Bearer {session['session_token']}"}, timeout=20)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(503, {"code": "cloud_unavailable"}) from exc
    with connect() as db:
        db.execute("UPDATE analysis_outcomes SET uploaded_at=NULL WHERE user_id=?", (local_user_id(),))
        db.commit()
    return {"enabled": False, "shared": 0, "deleted": response.json().get("deleted", 0)}
