from trade_helper.trader_question import answer, web_sources


def reasoning(plan_side=None, action="open_now", long="reasonable", short="unsuitable", given=None):
    plan = ({"action": action, "side": plan_side, "entry_price": "100", "stop_loss": "98",
             "take_profit": "104", "reason": "plan"} if plan_side else
            {"action": "stand_aside", "side": None, "reason": "wait"})
    return {"agent_stance": plan_side or "wait", "entry_decision": plan, "bias_answer": given,
            "direction_assessment": {"long": {"verdict": long, "reason": "long reason"},
                                     "short": {"verdict": short, "reason": "short reason"}}}


LONG = {"kind": "market", "directional_bias": "bullish"}


def test_without_a_question_there_is_no_answer_and_the_plan_stands():
    result = answer(reasoning("short"), {"kind": "market", "directional_bias": None})
    assert result["bias_answer"] is None and result["entry_decision"]["side"] == "short"


def test_an_answer_that_enters_on_the_asked_side_is_kept():
    given = {"direction": "long", "verdict": "reasonable", "recommendation": "enter", "reason": "Support held."}
    result = answer(reasoning("long", given=given), LONG)
    assert result["bias_answer"]["recommendation"] == "enter" and result["entry_decision"]["side"] == "long"


def test_asking_long_and_getting_a_short_plan_without_a_reverse_becomes_standing_aside():
    # The reported failure: a long question answered with a short plan.
    given = {"direction": "long", "verdict": "conditional", "recommendation": "wait_for_entry",
             "reason": "Needs a close above 102."}
    result = answer(reasoning("short", action="wait_for_entry", short="conditional", given=given), LONG)
    assert result["entry_decision"]["action"] == "stand_aside"
    assert result["bias_answer"]["recommendation"] == "stand_aside"
    assert result["bias_answer"]["adjusted"] == "plan_mismatch"


def test_reversing_needs_the_opposite_side_reasonable_now():
    given = {"direction": "long", "verdict": "unsuitable", "recommendation": "reverse", "reason": "Broke down."}
    allowed = answer(reasoning("short", long="unsuitable", short="reasonable", given=given), LONG)
    assert allowed["bias_answer"]["recommendation"] == "reverse"
    assert allowed["entry_decision"]["side"] == "short"
    refused = answer(reasoning("short", long="unsuitable", short="conditional", given=given), LONG)
    assert refused["bias_answer"]["recommendation"] == "stand_aside"
    assert refused["bias_answer"]["adjusted"] == "reverse_not_supported"
    assert refused["entry_decision"]["action"] == "stand_aside"


def test_a_missing_answer_is_read_from_the_models_own_assessment():
    result = answer(reasoning("long", action="wait_for_entry", long="conditional"), LONG)
    assert result["bias_answer"] == {"direction": "long", "verdict": "conditional",
                                     "recommendation": "wait_for_entry", "reason": "long reason", "derived": True}


def test_only_titled_https_sources_are_kept_and_at_most_five():
    sources = web_sources([{"title": "A", "url": "https://a.test/x", "published": "2026-10-08"},
                           {"title": "B", "url": "http://b.test"}, {"url": "https://c.test"},
                           "junk"] + [{"title": f"T{i}", "url": f"https://t{i}.test"} for i in range(9)])
    assert [item["title"] for item in sources] == ["A", "T0", "T1", "T2", "T3"]


def test_claude_code_opens_web_search_only_when_allowed(monkeypatch, tmp_path):
    from trade_helper import claude_code_bridge

    monkeypatch.setattr(claude_code_bridge, "claude_executable", lambda: "/bin/claude")
    session = claude_code_bridge.ClaudeCodeSession()
    try:
        quiet = session._command("", "medium", tmp_path / "p", 1)
        searching = session._command("", "medium", tmp_path / "p", 4, web_search=True)
    finally:
        session.workspace.cleanup()
    assert quiet[quiet.index("--tools") + 1] == "" and "--allowedTools" not in quiet
    assert searching[searching.index("--tools") + 1] == "WebSearch"
    assert searching[searching.index("--allowedTools") + 1] == "WebSearch"


def test_the_claude_api_uses_the_search_tool_its_model_supports():
    from trade_helper.anthropic_api_bridge import search_tool

    assert search_tool("claude-opus-5-5") == {"type": "web_search_20260209", "name": "web_search", "max_uses": 3}
    assert search_tool("claude-haiku-4-5")["type"] == "web_search_20250305"
