import json
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from trade_helper import fund_flows
from trade_helper.api import app
from trade_helper.db import connect, init_db

DAY = 86_400_000
TODAY = int(datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


def change(net, price):
    return {"net": net, "net_usd": net * price}


def reserve(held, price, d1, d7, d30, movers=(), excluded=0, exchanges=9):
    return {"amount": held, "usd": held * price, "exchanges": exchanges,
            "1d": change(d1, price), "7d": change(d7, price), "30d": change(d30, price),
            "movers_7d": [{"exchange": name, **change(net, price)} for name, net in movers],
            "excluded_30d": [{"exchange": "OKX", "day": TODAY - i * DAY, "net": 1, "net_usd": 1}
                             for i in range(excluded)]}


def summary(through=TODAY, stale=False, lagging=()):
    return {"generated_at": TODAY + 3_600_000, "source": "DefiLlama", "through": through, "stale": stale,
            "lagging": [{"exchange": name, "through": TODAY - 5 * DAY} for name in lagging],
            "assets": {
                "BTC": reserve(1_800_000, 60_000, -1200, -8000, -30000, [("Binance", -5000), ("OKX", -2000)], 1),
                "ETH": reserve(15_000_000, 2_000, 40000, 90000, -100000, [("Bybit", 60000)]),
                "USDT": reserve(45e9, 1, 3e8, 1.2e9, 2e9),
                "USDC": reserve(9e9, 1, -1e8, 2e8, 5e8, exchanges=8),
                "LINK": reserve(60_000_000, 14, 100_000, -500_000, -2_000_000, [("Binance", -400_000)]),
            }}


def link_detail(window="30d"):
    days = {"7d": 7, "30d": 30, "90d": 90}[window]
    return {"generated_at": TODAY, "asset": "LINK", "window": window, "through": TODAY, "stale": False,
            "amount": 60_000_000, "usd": 840_000_000, "totals": change(-2_000_000, 14),
            "series": [{"t": TODAY - (days - 1 - i) * DAY, "amount": 60_000_000, "usd": 840_000_000,
                        **change(-1000, 14)} for i in range(days)],
            "exchanges": [{"exchange": "Binance", "amount": 30_000_000, "usd": 420_000_000, "share": 0.5,
                           **change(-1_500_000, 14)}],
            "excluded": [{"exchange": "OKX", "day": TODAY - DAY, **change(9_000_000, 14)}]}


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setenv("TRADE_FUND_FLOWS_ENABLED", "1")
    state = {"down": False, "summary": summary(), "supply": True, "calls": []}

    def fetch(path):
        state["calls"].append(path)
        if state["down"]:
            raise HTTPException(503, {"code": "smart_money_unavailable"})
        if path == "/smart-money/reserves":
            return state["summary"]
        if path == "/smart-money/overview":
            if not state["supply"]:
                raise HTTPException(503, {"code": "smart_money_unavailable"})
            return {"stablecoins": {"total_usd": 3.1e11, "change_1d": 4e8, "change_7d": 4e9}}
        if path.startswith("/smart-money/reserves/LINK"):
            return link_detail(path.rsplit("=", 1)[-1])
        raise HTTPException(404, {"code": "asset_not_tracked"})

    monkeypatch.setattr(fund_flows, "fetch_cloud", fetch)
    return state


def test_markets_name_their_coins():
    assert fund_flows.asset_for_market("binance:perp:BTCUSDT") == "BTC"
    assert fund_flows.asset_for_market("binance:perp:1000PEPEUSDT") == "PEPE"
    assert fund_flows.asset_for_market("binance:perp:1INCHUSDT") == "1INCH"


def test_analysis_gets_exchange_reserves_for_btc_eth_stablecoins_and_the_pair(cloud):
    context = fund_flows.fund_flows_context("binance:perp:LINKUSDT")
    assert context["version"] == "fund_flows_context_v3"
    assert context["status"] == "available"
    assert context["as_of"] == datetime.fromtimestamp(TODAY / 1000, UTC).isoformat()
    assert set(context["assets"]) == {"BTC", "ETH"}
    assert context["assets"]["BTC"] == {
        "held": 1800000, "held_usd": 108000000000, "exchanges_reporting": 9,
        "net_1d": {"amount": -1200, "usd": -72000000}, "net_7d": {"amount": -8000, "usd": -480000000},
        "net_30d": {"amount": -30000, "usd": -1800000000},
        "largest_movers_7d": [{"exchange": "Binance", "amount": -5000, "usd": -300000000},
                              {"exchange": "OKX", "amount": -2000, "usd": -120000000}],
        "excluded_changes_30d": 1}
    stable = context["stablecoins"]
    assert set(stable["exchange_reserves"]) == {"USDT", "USDC"}
    assert stable["exchange_reserves"]["USDC"]["exchanges_reporting"] == 8
    assert stable["total_supply_usd"] == 310000000000 and stable["supply_change_7d_usd"] == 4000000000
    link = context["pair_asset"]
    assert link["status"] == "tracked" and link["asset"] == "LINK"
    assert link["net_7d"] == {"amount": -500000, "usd": -7000000}
    # One summary answers everything; no per-transfer lists reach the model any more.
    assert cloud["calls"] == ["/smart-money/reserves", "/smart-money/overview"]


def test_missing_supply_leaves_out_only_the_supply(cloud):
    cloud["supply"] = False
    stable = fund_flows.fund_flows_context("binance:perp:BTCUSDT")["stablecoins"]
    assert "total_supply_usd" not in stable and set(stable["exchange_reserves"]) == {"USDT", "USDC"}


def test_untracked_stale_and_outage_cases_are_labelled_not_guessed(cloud):
    assert fund_flows.fund_flows_context("binance:perp:TONUSDT")["pair_asset"] == {"asset": "TON", "status": "not_tracked"}
    assert fund_flows.fund_flows_context("binance:perp:ETHUSDT")["pair_asset"] == {"asset": "ETH", "status": "same_as_reference"}
    cloud["summary"] = summary(stale=True, lagging=("MEXC",))
    context = fund_flows.fund_flows_context("binance:perp:BTCUSDT")
    assert context["status"] == "stale" and context["exchanges_not_updating"] == ["MEXC"]
    unavailable = {"version": fund_flows.VERSION, "source": fund_flows.SOURCE, "status": "unavailable",
                   "pair_asset": {"asset": "LINK", "status": "unavailable"}}
    cloud["summary"] = summary(through=None)
    assert fund_flows.fund_flows_context("binance:perp:LINKUSDT") == unavailable
    cloud["down"] = True
    assert fund_flows.fund_flows_context("binance:perp:LINKUSDT") == unavailable


def test_disabled_fund_flows_never_reach_the_cloud(cloud, monkeypatch):
    monkeypatch.setenv("TRADE_FUND_FLOWS_ENABLED", "0")
    assert fund_flows.fund_flows_context("binance:perp:BTCUSDT")["status"] == "unavailable"
    assert cloud["calls"] == []


def test_a_conversation_starts_from_a_frozen_snapshot_and_asks_with_its_data(cloud, monkeypatch):
    from trade_helper import discussions

    init_db()
    client = TestClient(app)
    assert client.get("/api/v1/smart-money/snapshots/latest").json() == {"snapshot": None}
    created = client.post("/api/v1/smart-money/snapshots", json={"asset": "LINK", "window": "30d"})
    assert created.status_code == 201
    snapshot = created.json()
    assert snapshot["asset"] == "LINK" and snapshot["window"] == "30d"
    assert client.get("/api/v1/smart-money/snapshots/latest").json()["snapshot"]["id"] == snapshot["id"]
    assert "/smart-money/reserves/LINK?window=30d" in cloud["calls"]

    sent = client.post(f"/api/v1/discussions/fund_flows/{snapshot['id']}/messages", json={
        "message": "LINK 流出交易所代表什麼？", "request_id": "00000000-0000-4000-8000-000000000001",
        "output_locale": "en-US"})
    assert sent.status_code == 202
    assert sent.json()["subject"]["title"] == "Fund flows"

    seen = {}

    def reply(context, messages, *, timeout, instructions=None, on_text=None):
        from trade_helper.discussion_context import build_discussion_input

        seen["instructions"] = instructions
        seen["input"] = json.loads(build_discussion_input(context, messages))
        return "Outflows may be withdrawals to holding; labels are incomplete.", "codex", "test-model"

    monkeypatch.setattr(discussions, "generate_reply", reply)
    discussions.run_once()
    state = client.get(f"/api/v1/discussions/fund_flows/{snapshot['id']}").json()
    assert state["messages"][-1]["status"] == "completed", state["messages"][-1]
    frozen = seen["input"]["frozen_context"]
    focus = frozen["fund_flows"]["focus"]
    assert focus["asset"] == "LINK" and focus["window"] == "30d" and len(focus["daily"]) == 30
    assert focus["exchanges"] == [{"exchange": "Binance", "held": 30000000, "held_usd": 420000000,
                                   "share_pct": 50.0, "amount": -1500000, "usd": -21000000}]
    assert focus["excluded_changes"][0]["exchange"] == "OKX"
    assert "live_market" not in seen["input"]
    assert "frozen_context.fund_flows" in seen["instructions"]


def test_an_unknown_snapshot_or_coin_is_refused(cloud):
    init_db()
    client = TestClient(app)
    assert client.post("/api/v1/smart-money/snapshots", json={"asset": "link", "window": "7d"}).status_code == 422
    assert client.post("/api/v1/smart-money/snapshots", json={"asset": "LINK", "window": "2y"}).status_code == 422
    # Reserves are daily: there is no one-day window to ask about.
    assert client.post("/api/v1/smart-money/snapshots", json={"asset": "LINK", "window": "1d"}).status_code == 422
    assert client.post("/api/v1/smart-money/snapshots", json={"asset": "TON", "window": "7d"}).status_code == 404
    response = client.post("/api/v1/discussions/fund_flows/flow_missing/messages", json={
        "message": "hi", "request_id": "00000000-0000-4000-8000-000000000002"})
    assert response.status_code == 404


def test_upgrade_keeps_existing_conversations_and_their_messages():
    init_db()
    with connect() as db:
        db.execute("INSERT INTO macro_interpretations(id,user_id,status,evidence_version,prompt_version,"
                   "fingerprint,evidence_json,created_at,updated_at) VALUES('m1','local-demo','succeeded',"
                   "'v','p','f','{}','2026-10-01','2026-10-01')")
        db.execute("INSERT INTO discussion_sessions(id,user_id,subject_type,subject_id,macro_id,subject_json,"
                   "context_json,created_at,updated_at) VALUES('s1','local-demo','macro','m1','m1','{}','{}',"
                   "'2026-10-01','2026-10-01')")
        db.execute("INSERT INTO discussion_messages(id,session_id,sequence,role,content,status,request_id,"
                   "created_at) VALUES('u1','s1',1,'user','hello','completed','r1','2026-10-01')")
        db.execute("DELETE FROM schema_migrations WHERE version>=10")
        db.execute("PRAGMA user_version=9")
    init_db()
    with connect(readonly=True) as db:
        assert db.execute("SELECT subject_type FROM discussion_sessions").fetchone()["subject_type"] == "macro"
        assert db.execute("SELECT content FROM discussion_messages").fetchone()["content"] == "hello"
        assert "fund_flows" in db.execute(
            "SELECT sql FROM sqlite_master WHERE name='discussion_sessions'").fetchone()["sql"]
