"""Answer the trader's direction question, and keep the plan consistent with the answer.

A market analysis may carry the side the trader wants to trade. The model answers
it in bias_answer; this module makes that answer checkable: the entry plan must be
on the asked side for enter or wait_for_entry, on the opposite side only for a
reverse the model itself rated reasonable now, and absent otherwise. A plan that
contradicts the answer is replaced by standing aside rather than shown as advice.
"""

from urllib.parse import urlparse

VERDICTS = {"reasonable", "conditional", "unsuitable"}
RECOMMENDATIONS = {"enter", "wait_for_entry", "stand_aside", "reverse"}
SIDES = {"bullish": "long", "bearish": "short"}
MAX_SOURCES = 5


def asked_side(request: dict) -> str | None:
    if request.get("kind") == "positions":
        return None
    return SIDES.get(request.get("directional_bias"))


def _opposite(side: str) -> str:
    return "short" if side == "long" else "long"


def _text(value, limit: int) -> str | None:
    return value.strip()[:limit] if isinstance(value, str) and value.strip() else None


def web_sources(value) -> list[dict]:
    """The pages the model says it used: https links only, titled, at most five."""
    sources = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        url, title = _text(item.get("url"), 500), _text(item.get("title"), 200)
        if not url or not title or urlparse(url).scheme != "https" or not urlparse(url).netloc:
            continue
        sources.append({"title": title, "url": url, "published": _text(item.get("published"), 40)})
        if len(sources) == MAX_SOURCES:
            break
    return sources


def _derived(side: str, reasoning: dict) -> dict | None:
    """An answer read from the model's own assessment when it gave none."""
    assessment = (reasoning.get("direction_assessment") or {}).get(side)
    if not isinstance(assessment, dict) or assessment.get("verdict") not in VERDICTS:
        return None
    plan = reasoning.get("entry_decision") or {}
    if plan.get("side") == side and plan.get("action") in {"open_now", "wait_for_entry"}:
        recommendation = "enter" if plan["action"] == "open_now" else "wait_for_entry"
    elif plan.get("side") == _opposite(side):
        recommendation = "reverse"
    else:
        recommendation = "stand_aside"
    return {"direction": side, "verdict": assessment["verdict"], "recommendation": recommendation,
            "reason": assessment.get("reason") or "", "derived": True}


def _stand_aside(reason: str) -> dict:
    return {"action": "stand_aside", "side": None, "entry_price": None, "stop_loss": None,
            "take_profit": None, "trigger": None, "trigger_rule": None, "invalidation": None,
            "invalidation_rule": None, "reason": reason, "basis_level_ids": []}


def answer(reasoning: dict, request: dict) -> dict:
    """Return reasoning with a checked bias_answer and an entry plan that agrees with it."""
    reasoning = dict(reasoning)
    reasoning["web_sources"] = web_sources(reasoning.get("web_sources"))
    side = asked_side(request)
    if side is None:
        reasoning["bias_answer"] = None
        return reasoning
    given = reasoning.get("bias_answer")
    if (isinstance(given, dict) and given.get("direction") == side and given.get("verdict") in VERDICTS
            and given.get("recommendation") in RECOMMENDATIONS and _text(given.get("reason"), 1200)):
        result = {"direction": side, "verdict": given["verdict"], "recommendation": given["recommendation"],
                  "reason": _text(given["reason"], 1200), "derived": False}
    else:
        result = _derived(side, reasoning)
    if result is None:
        reasoning["bias_answer"] = None
        return reasoning
    assessments = reasoning.get("direction_assessment") or {}
    # Reversing needs the opposite side rated reasonable now, not merely conditional.
    if (result["recommendation"] == "reverse"
            and (assessments.get(_opposite(side)) or {}).get("verdict") != "reasonable"):
        result["recommendation"] = "stand_aside"
        result["adjusted"] = "reverse_not_supported"
    expected = {"enter": side, "wait_for_entry": side, "reverse": _opposite(side)}.get(result["recommendation"])
    plan = reasoning.get("entry_decision")
    plan_side = plan.get("side") if isinstance(plan, dict) and plan.get("action") != "stand_aside" else None
    if plan_side != expected:
        # A plan that contradicts the answer is not shown as advice.
        reasoning["entry_decision"] = _stand_aside(result["reason"])
        reasoning["agent_stance"] = "wait"
        result["recommendation"] = "stand_aside"
        result.setdefault("adjusted", "plan_mismatch")
    reasoning["bias_answer"] = result
    return reasoning
