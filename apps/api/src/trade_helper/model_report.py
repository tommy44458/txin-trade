"""Read Agent output for display without rejecting its strategy or evidence claims."""

import json
from decimal import Decimal, DecimalException

from .prompts.contracts import validate_locale
from .risk import costed_risk

REPORT_FIELDS = frozenset({
    "market", "levels", "strategy", "supporting_evidence", "counter_evidence", "evidence_tools",
    "strategy_decision", "agent_stance", "entry_decision", "direction_assessment", "macro_outlook",
    "position_decisions",
})


# A waiting entry starts on a touch of its price, or on a primary close beyond it.
TRIGGER_TYPES = ('touch', 'close_above', 'close_below')

def _closing(text: str) -> str | None:
    """The brackets an object left open, or None when it ends inside a string."""
    stack, in_string, escaped = [], False, False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]" and (not stack or stack.pop() != char):
            return None
    return None if in_string else "".join(reversed(stack))


def _restore_fields(value: dict) -> dict:
    """Move report fields the model nested by mistake back to the top level.

    Seen in practice: a brace missing in the middle nests every later field in the
    object it failed to close, and a made-up "string_fields" wrapper around the
    text fields. Fields already at the top level are never replaced.
    """
    def find(node: dict, key: str):
        for child in node.values():
            if isinstance(child, dict):
                if key in child:
                    return child
                found = find(child, key)
                if found is not None:
                    return found
        return None

    for key in sorted(REPORT_FIELDS - value.keys()):
        holder = find(value, key)
        if holder is not None:
            value[key] = holder.pop(key)
    return value


def _json_object(text: str):
    """Parse the model's JSON object, tolerating trailing prose or missing final brackets.

    Repairs only structure: text cut off inside a string is not guessed at.
    """
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    start = text.find("{") if isinstance(text, str) else -1
    if start < 0:
        return {}
    try:
        return json.JSONDecoder().raw_decode(text[start:])[0]
    except json.JSONDecodeError:
        pass
    body = text[start:].rstrip()
    closing = _closing(body)
    if not closing:
        return {}
    try:
        value = json.loads(body + closing)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def read_model_report(raw: str, trace: list[dict], *, output_locale: str = "zh-TW") -> dict:
    validate_locale(output_locale)
    text = raw.strip()
    if text.startswith('```'):
        text = text.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    value = _json_object(text)
    if isinstance(value, dict):
        value = _restore_fields(value)
    if not isinstance(value, dict):
        value = {}
    def sentence(key):
        item = value.get(key)
        return item if isinstance(item, str) else ''
    result = {key: sentence(key) for key in
              ('market', 'levels', 'strategy', 'supporting_evidence', 'counter_evidence')}
    if not any(result.values()):
        result['strategy'] = raw or (
            'The model did not return analysis text.' if output_locale == 'en-US'
            else '模型此次未回傳分析文字。'
        )
    result['evidence_tools'] = [name for name in value.get('evidence_tools', []) if isinstance(name, str)] if isinstance(value.get('evidence_tools'), list) else []
    result['strategy_decision'] = value.get('strategy_decision') if value.get('strategy_decision') in ('candidate', 'wait') else 'wait'
    result['agent_stance'] = value.get('agent_stance') if value.get('agent_stance') in ('long', 'short', 'wait') else None
    plan = value.get('entry_decision')
    if isinstance(plan, dict) and plan.get('action') in ('open_now', 'wait_for_entry', 'stand_aside'):
        # Preserve absent values as absent; do not invent prices, triggers or actions.
        result['entry_decision'] = {key: plan.get(key) for key in (
            'action', 'side', 'entry_price', 'stop_loss', 'take_profit', 'trigger', 'invalidation', 'reason')}
        for key in ('reason', 'trigger', 'invalidation'):
            if not isinstance(result['entry_decision'][key], str):
                result['entry_decision'][key] = None
        if result['entry_decision']['side'] not in ('long', 'short'):
            result['entry_decision']['side'] = None
        for key in ('entry_price', 'stop_loss', 'take_profit'):
            try:
                price = Decimal(str(plan.get(key)))
                result['entry_decision'][key] = str(price) if price.is_finite() else None
            except (DecimalException, ValueError, TypeError):
                result['entry_decision'][key] = None
        result['entry_decision']['basis_level_ids'] = plan.get('basis_level_ids') if isinstance(plan.get('basis_level_ids'), list) else []
        result['entry_decision']['trigger_rule'] = trigger_rule(plan.get('trigger_rule'))
        if result['entry_decision']['action'] != 'wait_for_entry':
            result['entry_decision']['trigger_rule'] = None
    else:
        result['entry_decision'] = None
    supplied_directions = value.get('direction_assessment')
    directions = {side: {'verdict': item['verdict'], 'reason': item['reason'].strip()}
                  for side, item in (supplied_directions.items() if isinstance(supplied_directions, dict) else ())
                  if side in ('long', 'short') and isinstance(item, dict)
                  and item.get('verdict') in ('reasonable', 'conditional', 'unsuitable')
                  and isinstance(item.get('reason'), str) and item['reason'].strip()}
    result['direction_assessment'] = directions or None
    macro = value.get('macro_outlook')
    result['macro_outlook'] = (macro | {'evidence_ids': macro.get('evidence_ids') if isinstance(macro.get('evidence_ids'), list) else []}
                              if isinstance(macro, dict) and isinstance(macro.get('reason'), str) else None)
    supplied = value.get('position_decisions', {})
    result['position_decisions'] = {key: item for key, item in supplied.items()
        if isinstance(item, dict) and item.get('decision') in ('hold', 'close_now') and isinstance(item.get('reason'), str)} if isinstance(supplied, dict) else {}
    result['position_choices'] = {item['position_id']: item['default_action_id']
        for run in trace if run['tool'] == 'evaluate_positions' for item in run['result']['positions']}
    return result


def trigger_rule(value) -> dict | None:
    """The checkable part of a waiting entry's condition, or None; never invented."""
    if not isinstance(value, dict) or value.get('type') not in TRIGGER_TYPES:
        return None
    try:
        price = Decimal(str(value.get('price')))
    except (DecimalException, ValueError, TypeError):
        return None
    return {'type': value['type'], 'price': str(price)} if price.is_finite() and price > 0 else None


def entry_cost_reference(plan: dict | None, quote: dict, leverage: int) -> dict | None:
    """Optional arithmetic only; unavailable estimates never block the report."""
    if not plan or plan.get('action') == 'stand_aside' or plan.get('side') not in ('long', 'short'):
        return None
    try:
        entry, stop, target = (Decimal(str(plan.get(key))) for key in ('entry_price', 'stop_loss', 'take_profit'))
        if not all(price.is_finite() and price > 0 for price in (entry, stop, target)):
            return None
        if not (stop < entry < target if plan['side'] == 'long' else target < entry < stop):
            return None
        return costed_risk(plan['side'], entry, stop, target, leverage, quote)
    except (DecimalException, ValueError, TypeError, KeyError, ZeroDivisionError):
        return None
