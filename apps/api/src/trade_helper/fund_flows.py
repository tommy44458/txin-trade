"""Fund flows for analyses and follow-up questions.

Analyses get exchange reserves the cloud reads daily from DefiLlama's CEX transparency
data: what the largest exchanges hold in the wallets they publish, and how that changed
over one, seven and thirty days. BTC, ETH and the USDT/USDC stablecoins always, and the
analyzed pair's coin when it is followed. Holdings are counted, not single transfers, so
moves between an exchange's own wallets and unlabelled deposit addresses cannot be
mistaken for flows. It is built by code, not by a model, so it adds no model call and only
cached requests. A follow-up conversation on the fund-flows page is anchored on a snapshot
of the same data, frozen when the conversation starts.
"""

import json
import os
import re
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .config import local_user_id
from .db import connect, new_id, utc_now
from .smart_money import RESERVE_WINDOWS, fetch_cloud

VERSION = "fund_flows_context_v3"
SOURCE = "DefiLlama CEX transparency (wallets each exchange publishes), read daily by the txinTrade cloud"
REFERENCE_ASSETS = ("BTC", "ETH")
STABLECOINS = ("USDT", "USDC")
UNITS = ("held and net in the coin's own units; *_usd in US dollars, one day at that day's price and longer "
         "windows at the latest close's price; net is the change in exchange holdings: positive means coins "
         "moved onto exchanges, negative means they left")
NOTES = [
    ("Holdings are at 00:00 UTC on as_of, in the wallets each exchange publishes; exchanges that publish none, "
     "such as Coinbase, are not included."),
    ("A day whose change looked like an update to an exchange's published wallet list rather than coins moving "
     "is left out of net and counted in excluded_changes_30d."),
]
router = APIRouter(prefix="/api/v1/smart-money/snapshots", tags=["smart money"])


def asset_for_market(market_id: str) -> str:
    """The coin behind a market: binance:perp:1000PEPEUSDT trades PEPE."""
    symbol = market_id.rsplit(":", 1)[-1]
    return re.sub(r"^1000+(?=[A-Z])", "", re.sub(r"(USDT|USDC)$", "", symbol))


def _iso(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat() if ms else None


def _usd(value) -> int:
    return round(float(value or 0))


def _amount(value) -> float:
    value = float(value or 0)
    return round(value, 2 if abs(value) >= 1 else 6)


def _net(change: dict) -> dict:
    return {"amount": _amount(change["net"]), "usd": _usd(change["net_usd"])}


def _reserves(entry: dict) -> dict:
    """One coin across the followed exchanges: holdings now and the change over 1, 7 and 30 days."""
    return {"held": _amount(entry["amount"]), "held_usd": _usd(entry["usd"]),
            "exchanges_reporting": entry["exchanges"],
            "net_1d": _net(entry["1d"]), "net_7d": _net(entry["7d"]), "net_30d": _net(entry["30d"]),
            "largest_movers_7d": [{"exchange": m["exchange"], **_net(m)} for m in entry.get("movers_7d") or []],
            "excluded_changes_30d": len(entry.get("excluded_30d") or [])}


def _status(summary: dict) -> str:
    if summary.get("through") is None:
        return "unavailable"
    return "stale" if summary.get("stale") else "available"


def _header(summary: dict) -> dict:
    header = {"version": VERSION, "source": SOURCE, "status": _status(summary),
              "as_of": _iso(summary.get("through")), "units": UNITS, "notes": NOTES}
    lagging = [item["exchange"] for item in summary.get("lagging") or []]
    if lagging:
        header["exchanges_not_updating"] = lagging
    return header


def _stablecoin_supply() -> dict:
    """Total stablecoin supply from the cloud's DefiLlama series; left out when unavailable."""
    try:
        stable = fetch_cloud("/smart-money/overview").get("stablecoins") or {}
    except HTTPException:
        return {}
    if stable.get("total_usd") is None:
        return {}
    return {"total_supply_usd": _usd(stable["total_usd"]),
            "supply_change_1d_usd": _usd(stable.get("change_1d")),
            "supply_change_7d_usd": _usd(stable.get("change_7d"))}


def _stablecoins(assets: dict) -> dict:
    return {"exchange_reserves": {s: _reserves(assets[s]) for s in STABLECOINS if s in assets},
            **_stablecoin_supply()}


def unavailable_fund_flows(market_id: str) -> dict:
    return {"version": VERSION, "source": SOURCE, "status": "unavailable",
            "pair_asset": {"asset": asset_for_market(market_id), "status": "unavailable"}}


def fund_flows_context(market_id: str) -> dict:
    """What the analysis sees; never raises, so a cloud outage only marks the data unavailable."""
    pair = asset_for_market(market_id)
    if os.getenv("TRADE_FUND_FLOWS_ENABLED", "1") == "0":
        return unavailable_fund_flows(market_id)
    try:
        summary = fetch_cloud("/smart-money/reserves")
        assets = summary["assets"]
        context = {
            **_header(summary),
            "assets": {a: _reserves(assets[a]) for a in REFERENCE_ASSETS if a in assets},
            "stablecoins": _stablecoins(assets),
        }
    except (HTTPException, KeyError, TypeError, ValueError):
        return unavailable_fund_flows(market_id)
    if context["status"] == "unavailable":
        return unavailable_fund_flows(market_id)
    if pair in REFERENCE_ASSETS:
        context["pair_asset"] = {"asset": pair, "status": "same_as_reference"}
    elif pair in assets:
        context["pair_asset"] = {"asset": pair, "status": "tracked", **_reserves(assets[pair])}
    else:
        context["pair_asset"] = {"asset": pair, "status": "not_tracked"}
    return context


# ---- Snapshots for follow-up questions ---------------------------------------------

class SnapshotInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset: str = Field(pattern=r"^[A-Z0-9]{2,12}$")
    window: str = Field(pattern="^(" + "|".join(RESERVE_WINDOWS) + ")$")


def _public(row) -> dict:
    return {"id": row["id"], "asset": row["asset"], "window": row["flow_window"],
            "as_of": row["generated_at"], "created_at": row["created_at"]}


def _focus(detail: dict, window: str) -> dict:
    """The chosen coin over the chosen window: each day's change and each exchange's part."""
    return {
        "asset": detail["asset"], "window": window,
        "held": _amount(detail["amount"]), "held_usd": _usd(detail["usd"]),
        "net": _net(detail["totals"]),
        "daily": [{"date": _iso(point["t"])[:10], "held": _amount(point["amount"]), **_net(point)}
                  for point in detail["series"]],
        "exchanges": [{"exchange": e["exchange"], "held": _amount(e["amount"]), "held_usd": _usd(e["usd"]),
                       "share_pct": round(e["share"] * 100, 1), **_net(e)} for e in detail["exchanges"]],
        "excluded_changes": [{"exchange": c["exchange"], "date": _iso(c["day"])[:10], **_net(c)}
                             for c in detail.get("excluded") or []],
    }


@router.post("", status_code=201)
def create_snapshot(body: SnapshotInput) -> dict:
    """Freeze the fund flows on screen as the basis of a new conversation."""
    summary = fetch_cloud("/smart-money/reserves")
    detail = fetch_cloud(f"/smart-money/reserves/{body.asset}?window={body.window}")
    assets = summary.get("assets") or {}
    payload = {
        **_header(summary),
        "overview": {a: _reserves(assets[a]) for a in REFERENCE_ASSETS if a in assets},
        "stablecoins": _stablecoins(assets),
        "focus": _focus(detail, body.window),
    }
    row_id, now = new_id("flow"), utc_now()
    with connect() as db:
        db.execute(
            "INSERT INTO fund_flow_snapshots(id,user_id,asset,flow_window,generated_at,payload_json,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (row_id, local_user_id(), body.asset, body.window, payload["as_of"] or utc_now(),
             json.dumps(payload, ensure_ascii=False), now),
        )
        row = db.execute("SELECT * FROM fund_flow_snapshots WHERE id=?", (row_id,)).fetchone()
    return _public(row)


@router.get("/latest")
def latest_snapshot() -> dict:
    """The conversation to show on the page: the most recently started one."""
    with connect(readonly=True) as db:
        row = db.execute(
            "SELECT * FROM fund_flow_snapshots WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT 1",
            (local_user_id(),),
        ).fetchone()
    return {"snapshot": _public(row) if row else None}
