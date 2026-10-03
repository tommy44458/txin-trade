import json
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from trade_helper.agent import analyze_with_tools
from trade_helper.analysis import build_report
from trade_helper.api import app
from trade_helper.db import connect
from trade_helper.position_advice import build_position_options
from trade_helper.report_contract import validate_report
from trade_helper.technical_snapshot import prepare_analysis_evidence

from .test_analysis import detailed_explanation, sample_candles
from .test_position_advice import LEVELS, MARKET, QUOTE, position
from .test_positions_n3 import BASE


def test_twelve_selected_positions_reach_preparation_model_and_each_report_review(monkeypatch):
    candles = sample_candles(recent=True)
    quote = {"price": candles[-1]["close"], "mark_price": candles[-1]["close"],
             "tick_size": "0.1", "snapshot_hash": "a" * 64,
             "observed_at": datetime.now(UTC).isoformat()}
    selected = [position(f"batch_{index}", "long" if index % 2 == 0 else "short",
                         stop=None, target=None) for index in range(12)][::-1]
    ids = [item["id"] for item in selected]
    request = {"kind": "positions", "market_id": MARKET, "timeframe": "1h",
               "leverage": 5, "position_ids": ids}
    prepared = prepare_analysis_evidence(request, candles, quote, positions=selected)
    options = next(run["result"] for run in prepared if run["tool"] == "evaluate_positions")
    assert [item["position_id"] for item in options["positions"]] == ids
    candidates = next(run["result"] for run in prepared if run["tool"] == "strategy_candidates")
    decisions = {
        item["id"]: {"decision": "hold" if item["side"] == "long" else "close_now",
                     "reason": "已收盤上漲結構支持續抱。" if item["side"] == "long" else
                               "已收盤上漲結構不利於空單，建議現在平倉。"}
        for item in selected
    }
    response = {
        "market": "依已收盤結構判斷。", "levels": "參考已確認區間。",
        "strategy": "逐筆評估目前方向。", "evidence_tools": [
            "support_resistance", "strategy_candidates", "evaluate_positions"],
        "strategy_decision": "wait", **detailed_explanation(candidates),
        "position_decisions": decisions,
    }
    requests = []

    class FakeResponses:
        def create(self, **kwargs):
            context = json.loads(kwargs["input"][0]["content"])
            requests.append(context)
            assert [item["id"] for item in context["selected_positions"]] == ids
            assert [item["position_id"] for item in
                    context["precomputed_evidence"]["evaluate_positions"]["positions"]] == ids
            return SimpleNamespace(output=[], output_text=json.dumps(response))

    monkeypatch.setenv("OPENAI_API_KEY", "fake-unit-test-key")
    monkeypatch.setenv("OPENAI_MODEL", "fake-unit-test-model")
    monkeypatch.setattr("trade_helper.agent.OpenAI",
                        lambda **_kwargs: SimpleNamespace(responses=FakeResponses()))
    decision = analyze_with_tools(request, candles, quote, positions=selected,
                                  prepared_trace=prepared)
    assert len(requests) == 1
    assert decision["position_decisions"] == decisions
    report = build_report(request, candles, quote, selected, decision)
    assert [item["id"] for item in report["position_snapshot"]] == ids
    assert [item["position_id"] for item in report["position_reviews"]] == ids
    assert all(item["agent_decision"] == decisions[item["position_id"]] | {"exit_plan": None}
               for item in report["position_reviews"])
    validate_report(report)
    incomplete = deepcopy(report)
    incomplete["position_reviews"].pop()
    with pytest.raises(ValueError, match="Invalid position advice snapshot"):
        validate_report(incomplete)


def test_api_still_rejects_empty_duplicate_missing_closed_cross_market_and_other_user(monkeypatch):
    monkeypatch.setenv("APP_LOCAL_USER_ID", "batch-owner")
    with TestClient(app) as client:
        open_positions = [client.post("/api/v1/positions", json=BASE).json()["id"]
                          for _ in range(12)]
        other_market = client.post("/api/v1/positions", json={
            **BASE, "market_id": "binance:perp:ETHUSDT",
        }).json()["id"]
        closed = client.post("/api/v1/positions", json=BASE).json()["id"]
        assert client.post(f"/api/v1/positions/{closed}/close").status_code == 200
        body = {"kind": "positions", "market_id": MARKET, "position_ids": open_positions}
        for rejected, expected in (([], 422), (open_positions + open_positions[:1], 422),
                                   (open_positions + ["missing-position"], 404),
                                   (open_positions + [closed], 404),
                                   (open_positions + [other_market], 404)):
            response = client.post("/api/v1/analyses", json={**body, "position_ids": rejected},
                                   headers={"Idempotency-Key": str(uuid4())})
            assert response.status_code == expected
        monkeypatch.setenv("APP_LOCAL_USER_ID", "another-batch-owner")
        assert client.post("/api/v1/analyses", json=body,
                           headers={"Idempotency-Key": str(uuid4())}).status_code == 404
        with connect(readonly=True) as db:
            assert db.execute("SELECT count(*) AS n FROM analyses").fetchone()["n"] == 0


@pytest.mark.parametrize("count", [12, 25])
def test_python_position_reference_preserves_every_selected_item(count):
    selected = [position(f"batch_{index}", stop=None, target=None) for index in range(count)]
    options = build_position_options(selected, QUOTE, LEVELS, "range", Decimal(2))
    assert [item["position_id"] for item in options["positions"]] == [item["id"] for item in selected]


@pytest.mark.parametrize("selected", [[], [position(), position()], [position() | {"side": "invalid"}]])
def test_python_position_reference_still_rejects_invalid_selection(selected):
    with pytest.raises(ValueError):
        build_position_options(selected, QUOTE, LEVELS, "range", Decimal(2))
