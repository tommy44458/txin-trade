import json
import shutil
from copy import deepcopy
from importlib.resources import files

import pytest
from fastapi.testclient import TestClient

from trade_helper.agent import agent_context
from trade_helper.api import app
from trade_helper.db import connect
from trade_helper.models import AnalysisRequest
from trade_helper.prompts import TRADING_STRATEGIES, resolve_prompt, task_prompt_versions
from trade_helper.prompts.contracts import content_hash
from trade_helper.prompts.maintenance import reviewed_manifest
from trade_helper.prompts.registry import read_skills, validate_catalog
from trade_helper.prompts_artifacts import load_prompt_artifact
from trade_helper.reversal_map import apply_strategy, read_reversal_map
from trade_helper.trader_question import answer

from .test_prompt_registry import catalog_inputs

STRATEGY = {"timeframe": "1h", "trading_strategy": "extreme_reversal"}
QUOTE = {"price": "100.00", "tick_size": "0.01"}
ATR = "1.00"


def skill_inputs():
    prompts = files("trade_helper.prompts")
    return read_skills(lambda path: json.loads(prompts.joinpath(path).read_text()),
                       lambda path: prompts.joinpath(path).read_text())


@pytest.mark.parametrize("task", ["strategy_market", "strategy_positions"])
@pytest.mark.parametrize("prompt_locale", ["en-US", "zh-TW"])
def test_a_chosen_strategy_appends_its_skill_and_versions_the_prompt(task, prompt_locale):
    base = resolve_prompt(task, prompt_locale=prompt_locale, inputs={"timeframe": "1h"})
    chosen = resolve_prompt(task, prompt_locale=prompt_locale, inputs=STRATEGY)
    skill = skill_inputs()["texts"]["extreme_reversal"][prompt_locale]
    assert skill not in base.instructions and "reversal_map" not in base.instructions
    assert skill in chosen.instructions
    assert chosen.instructions.startswith(base.instructions.rsplit("\n\n", 1)[0])
    # The selected timeframe plan stays the final section.
    assert json.loads(chosen.instructions.rsplit("\n\n", 1)[1])["selected_timeframe_plan"]
    assert chosen.prompt_version == f"{base.prompt_version}:extreme_reversal_v1"
    assert chosen.template_sha256 != base.template_sha256
    assert chosen.policy_version == base.policy_version


def test_analyses_without_a_strategy_keep_their_prompt_and_other_tasks_never_load_a_skill():
    assert resolve_prompt("strategy_market", inputs={"timeframe": "4h", "trading_strategy": None}) == \
        resolve_prompt("strategy_market", inputs={"timeframe": "4h"})
    assert resolve_prompt("discussion", inputs=STRATEGY) == resolve_prompt("discussion")
    assert task_prompt_versions("strategy_market") == [
        "contract_strategy_v36", "contract_strategy_v36:extreme_reversal_v1"]
    with pytest.raises(ValueError, match="Unsupported prompt input: trading_strategy"):
        resolve_prompt("strategy_market", inputs={"trading_strategy": "breakout"})


def test_every_supported_strategy_has_a_reviewed_skill_in_both_languages():
    skills = skill_inputs()
    assert set(skills["catalog"]) == set(TRADING_STRATEGIES)
    validate_catalog(**catalog_inputs(), skills=skills)
    changed = deepcopy(skills)
    changed["texts"]["extreme_reversal"]["en-US"] += "\nAlways buy every dip."
    with pytest.raises(ValueError, match="Strategy skill changed without a version review"):
        validate_catalog(**catalog_inputs(), skills=changed)


def test_a_changed_skill_needs_a_new_skill_version(tmp_path):
    directory = tmp_path / "prompts"
    shutil.copytree(files("trade_helper.prompts"), directory)
    skill = directory / "skills/extreme_reversal/en-US.md"
    skill.write_text(skill.read_text() + "\nAlways buy every dip.\n")
    with pytest.raises(ValueError, match="needs a new skill version"):
        reviewed_manifest(directory)
    catalog = directory / "skills/catalog.json"
    entry = json.loads(catalog.read_text())
    entry["extreme_reversal"]["version"] = "extreme_reversal_v2"
    catalog.write_text(json.dumps(entry))
    reviewed = reviewed_manifest(directory)
    assert reviewed["skills"]["extreme_reversal"]["version"] == "extreme_reversal_v2"
    assert reviewed["skills"]["extreme_reversal"]["en-US"] == content_hash(skill.read_text())
    # Skills carry their own version; the shared policy version does not move.
    assert reviewed["policy_version"] == json.loads((directory / "manifest.json").read_text())["policy_version"]


def test_a_left_side_strategy_fixes_the_entry_style():
    request = AnalysisRequest(market_id="binance:perp:ETHUSDT", trading_style="right",
                              trading_strategy="extreme_reversal")
    assert request.trading_style == "left"
    assert AnalysisRequest(market_id="binance:perp:ETHUSDT", trading_style="right").trading_style == "right"


def test_the_strategy_reaches_the_model_input_only_when_chosen():
    candles = [{"close": "100", "close_time": "2026-11-26T00:00:00+00:00"}]
    quote = {"observed_at": "2026-11-26T00:30:00+00:00", "price": "100"}
    request = {"market_id": "binance:perp:ETHUSDT", "timeframe": "1h"}
    assert "trading_strategy" not in agent_context(request, candles, quote, None)
    chosen = agent_context(request | {"trading_strategy": "extreme_reversal"}, candles, quote, None)
    assert chosen["trading_strategy"] == "extreme_reversal"


def test_a_strategy_job_freezes_left_side_and_the_skill_prompt():
    with TestClient(app) as client:
        body = {"market_id": "binance:perp:ETHUSDT", "timeframe": "1h", "trading_style": "right",
                "trading_strategy": "extreme_reversal"}
        job = client.post("/api/v1/analyses", json=body, headers={"Idempotency-Key": "strategy-job-1"})
        assert job.status_code == 202
        assert job.json()["submitted_input"]["trading_style"] == "left"
        assert job.json()["submitted_input"]["trading_strategy"] == "extreme_reversal"
        with connect(readonly=True) as db:
            bundle = load_prompt_artifact(db, job.json()["prompt_artifact_id"])
        assert bundle.prompt_version.endswith(":extreme_reversal_v1")
        assert "reversal_map" in bundle.instructions
        again = client.post("/api/v1/analyses", json=body, headers={"Idempotency-Key": "strategy-job-1"})
        assert again.json()["id"] == job.json()["id"]


def zone(**overrides):
    return {"side": "long", "window": "0_6h", "tier": "first", "low": "97.00", "high": "98.00",
            "basis": "Below the confirmed swing low", "confidence": "medium",
            "confirmation": "A sweep that closes back above 97.50", "stop_loss": "96.50",
            "targets": ["101.00", "104.00"], "status": "candidate"} | overrides


def test_the_map_keeps_checkable_zones_and_computes_distance_and_r():
    result = read_reversal_map({"grade": "B", "grade_reason": "Near the zone, no exhaustion yet.",
                                "zones": [zone(), zone(side="short", low="103.00", high="104.00",
                                                   stop_loss="104.80", targets=["100.00", "97.00"])],
                                "paths": {"up_first": "Spike into 103", "down_first": "Flush to 97"},
                                "cancel_condition": "A close below 96.50", "data_limits": ""},
                               QUOTE, ATR)
    long, short = result["zones"]
    assert (long["distance_pct"], long["reference_entry"]) == ("-2.00", "97.50")
    # Risk 1.00 from the middle of the zone: 3.50 and 6.50 to the targets.
    assert [target["r_multiple"] for target in long["targets"]] == ["3.50", "6.50"]
    assert short["distance_pct"] == "3.00" and short["targets"][0]["price"] == "100.00"
    assert result["grade"] == "B" and result["data_limits"] is None and result["dropped_zones"] == 0
    assert result["paths"] == {"up_first": "Spike into 103", "down_first": "Flush to 97"}


@pytest.mark.parametrize("bad", [
    {"low": "101.00", "high": "102.00"},           # a long zone above the quote
    {"side": "short", "low": "95.00", "high": "96.00"},  # a short zone below it
    {"low": "97.005"},                             # off the tick grid
    {"low": "70.00", "high": "71.00"},             # further than the map may reach
    {"basis": "  "},                               # no stated basis
    {"window": "1_7d"}, {"side": "both"},
])
def test_zones_that_cannot_be_shown_honestly_are_dropped(bad):
    result = read_reversal_map({"grade": "C", "zones": [zone(**bad)]}, QUOTE, ATR)
    assert result["zones"] == [] and result["dropped_zones"] == 1


def test_a_wrong_stop_or_target_is_dropped_but_the_zone_stays():
    result = read_reversal_map({"grade": "B", "zones": [zone(stop_loss="97.50", targets=["97.80", "101.00"])]},
                               QUOTE, ATR)
    item = result["zones"][0]
    assert item["stop_loss"] is None and [target["price"] for target in item["targets"]] == ["101.00"]
    assert item["targets"][0]["r_multiple"] is None


def test_each_side_and_window_has_at_most_one_first_and_one_second_zone():
    result = read_reversal_map({"grade": "B", "zones": [
        zone(), zone(low="96.00", high="96.50"), zone(tier="second", low="94.00", high="95.00", stop_loss="93.50"),
        zone(window="6_24h"), zone(side="short", window="1_3d", low="105.00", high="106.00",
                                   stop_loss="107.00", targets=["101.00"])]}, QUOTE, ATR)
    assert [(item["window"], item["side"], item["tier"]) for item in result["zones"]] == [
        ("0_6h", "long", "first"), ("0_6h", "long", "second"), ("6_24h", "long", "first"),
        ("1_3d", "short", "first")]
    assert result["dropped_zones"] == 1


def test_a_map_without_grade_or_zones_is_absent():
    assert read_reversal_map({"zones": [zone(low="120.00", high="121.00")]}, QUOTE, ATR) is None
    assert read_reversal_map("A", QUOTE, ATR) is None
    assert read_reversal_map({"grade": "C"}, QUOTE, None) is None


def plan(action="wait_for_entry", side="long"):
    return {"action": action, "side": side, "entry_price": "97.50", "stop_loss": "96.50",
            "take_profit": "104.00", "trigger": "Close back above 97.50", "invalidation": "Below 96.50",
            "reason": "Sweep of the low", "basis_level_ids": []}


REQUEST = {"kind": "market", "trading_strategy": "extreme_reversal", "trading_style": "left"}


def test_a_plan_the_grade_supports_is_kept():
    reasoning = {"agent_stance": "long", "entry_decision": plan(),
                 "reversal_map": {"grade": "B", "zones": [zone()]}}
    result = apply_strategy(reasoning, REQUEST, QUOTE, ATR)
    assert result["entry_decision"]["action"] == "wait_for_entry"
    assert "plan_adjusted" not in result["reversal_map"]


@pytest.mark.parametrize(("grade", "action", "adjusted"), [
    ("C", "wait_for_entry", "grade_c_plan"), ("C", "open_now", "grade_c_plan"),
    ("B", "open_now", "open_now_needs_grade_a"),
])
def test_a_plan_the_grade_does_not_support_becomes_standing_aside(grade, action, adjusted):
    reasoning = {"agent_stance": "long", "entry_decision": plan(action),
                 "reversal_map": {"grade": grade, "grade_reason": "Mid-range, no extreme zone.", "zones": []}}
    result = apply_strategy(reasoning, REQUEST, QUOTE, ATR)
    assert result["entry_decision"]["action"] == "stand_aside"
    assert result["entry_decision"]["reason"] == "Mid-range, no extreme zone."
    assert result["agent_stance"] == "wait" and result["reversal_map"]["plan_adjusted"] == adjusted


def test_the_direction_answer_follows_the_plan_the_grade_left():
    given = {"direction": "long", "verdict": "conditional", "recommendation": "wait_for_entry",
             "reason": "Wait for the sweep."}
    reasoning = {"agent_stance": "long", "entry_decision": plan(), "bias_answer": given,
                 "direction_assessment": {"long": {"verdict": "conditional", "reason": "zone"}},
                 "reversal_map": {"grade": "C", "grade_reason": "No edge.", "zones": []}}
    request = REQUEST | {"directional_bias": "bullish"}
    result = answer(apply_strategy(reasoning, request, QUOTE, ATR), request)
    assert result["entry_decision"]["action"] == "stand_aside"
    assert result["bias_answer"]["recommendation"] == "stand_aside"


def test_analyses_without_the_strategy_carry_no_map():
    reasoning = {"entry_decision": plan(), "reversal_map": {"grade": "C"}}
    result = apply_strategy(reasoning, {"kind": "market"}, QUOTE, ATR)
    assert "reversal_map" not in result and result["entry_decision"]["action"] == "wait_for_entry"
    assert apply_strategy({"entry_decision": plan()}, REQUEST, QUOTE, ATR)["reversal_map"] is None
