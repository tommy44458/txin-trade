from copy import deepcopy

import pytest

from trade_helper.entry_decision import validate_entry_decision

QUOTE = {"price": "100.0", "tick_size": "0.1"}
LEVELS = [{"id": "support-one"}, {"id": "resistance-one"}]


def plan(action="open_now", side="long"):
    return {
        "action": action, "side": side, "entry_price": "100.0",
        "stop_loss": "98.0" if side == "long" else "102.0",
        "take_profit": "105.0" if side == "long" else "95.0",
        "trigger": "現價已符合方向條件。",
        "invalidation": "價格觸及止損或結構失效。",
        "reason": "目前價格與方向證據支持此方案。",
        "basis_level_ids": ["support-one"],
    }


def test_agent_may_open_at_quote_with_explicit_stop_and_target():
    result = validate_entry_decision(plan(), QUOTE, LEVELS, "2", "long", 5)
    assert result["net_risk_reward"] is not None
    short = plan(side="short")
    assert validate_entry_decision(short, QUOTE, LEVELS, "2", "short", 10)


def test_agent_may_wait_for_entry_or_stand_aside():
    waiting = plan("wait_for_entry")
    waiting["entry_price"] = "99.0"
    assert validate_entry_decision(waiting, QUOTE, LEVELS, "2", "long", 5)
    standing = {
        "action": "stand_aside", "side": None, "entry_price": None,
        "stop_loss": None, "take_profit": None, "trigger": None,
        "invalidation": None, "reason": "目前沒有可信的價格方案。",
        "basis_level_ids": [],
    }
    assert validate_entry_decision(standing, QUOTE, LEVELS, "2", "long", 5) is None


@pytest.mark.parametrize(("field", "value", "message"), [
    ("entry_price", "101.0", "analysis quote"),
    ("stop_loss", "98.05", "tick size"),
    ("stop_loss", "101.0", "wrong direction"),
    ("take_profit", "140.0", "too far"),
    ("basis_level_ids", ["unverified"], "unverified level"),
])
def test_entry_plan_rejects_unanchored_or_incoherent_prices(field, value, message):
    changed = deepcopy(plan())
    changed[field] = value
    with pytest.raises(ValueError, match=message):
        validate_entry_decision(changed, QUOTE, LEVELS, "2", "long", 5)


def test_trigger_and_invalidation_accept_concrete_prices_but_not_empty_text():
    concrete = plan()
    concrete["trigger"] = "現價 100.0 已符合做多條件。"
    concrete["invalidation"] = "跌破 98.0 則方案失效。"
    assert validate_entry_decision(concrete, QUOTE, LEVELS, "2", "long", 5)
    concrete["trigger"] = "  "
    with pytest.raises(ValueError, match="lacks trigger or invalidation"):
        validate_entry_decision(concrete, QUOTE, LEVELS, "2", "long", 5)


def test_entry_reason_accepts_concrete_price_but_rejects_empty_or_oversized_text():
    concrete = plan()
    concrete["reason"] = "現價 100.0 附近已有確認依據，因此採用此方案。"
    assert validate_entry_decision(concrete, QUOTE, LEVELS, "2", "long", 5)
    for invalid in (None, "  ", "x" * 1001):
        concrete["reason"] = invalid
        with pytest.raises(ValueError, match="lacks a clear reason"):
            validate_entry_decision(concrete, QUOTE, LEVELS, "2", "long", 5)


def test_a_high_risk_tolerance_accepts_a_wider_stop_and_a_farther_target():
    # ATR 1 at 100: the usual reach is 10; a high tolerance reaches 15.
    wide = plan() | {"stop_loss": "88.0", "take_profit": "114.0"}
    for risk in (None, "low", "medium"):
        with pytest.raises(ValueError, match="too far"):
            validate_entry_decision(wide, QUOTE, LEVELS, "1", "long", 5, risk)
    assert validate_entry_decision(wide, QUOTE, LEVELS, "1", "long", 5, "high")
    with pytest.raises(ValueError, match="too far"):
        validate_entry_decision(wide | {"take_profit": "116.0"}, QUOTE, LEVELS, "1", "long", 5, "high")


def test_both_prompt_languages_size_entry_stops_and_targets_to_risk_tolerance():
    from trade_helper.prompts.registry import _resource_json

    for locale, phrases in {"zh-TW": ("1 倍 atr14", "越過最近的反向有效區間", "強平距離之內"),
                            "en-US": ("1 × atr14", "past the nearest opposing", "liquidation distance")}.items():
        rule = _resource_json(f"{locale}/policies.json")["ENTRY_PLAN_PRICE_CONTRACT"]
        assert all(phrase in rule for phrase in phrases), locale
