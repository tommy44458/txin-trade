"""What a running analysis can already show, before its report is complete.

The worker records each finished step: the frozen quote, then the computed trend
and nearby support/resistance, then the AI report's sections as they are written.
The screen shows these while it waits; the completed report replaces all of it.
Progress is display-only: it is never validated, reused or sent to a model.
"""

import json
import re
from decimal import Decimal, InvalidOperation
from time import monotonic

from .db import connect

# Report sections shown as the AI writes them, in the order it usually writes them.
DRAFT_FIELDS = ("market", "levels", "strategy", "supporting_evidence", "counter_evidence")
MAX_DRAFT_CHARS = 4000
NEARBY_LEVELS = 3
DRAFT_INTERVAL_SECONDS = 1.0
_STRING_START = {field: re.compile(rf'"{field}"\s*:\s*"') for field in DRAFT_FIELDS}


def market_progress(request: dict, quote: dict) -> dict:
    return {"market_id": request["market_id"], "timeframe": request["timeframe"],
            "price": str(quote["price"]), "observed_at": quote.get("observed_at")}


def _decimal(value) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def evidence_progress(request: dict, trace: list[dict]) -> dict:
    """Trend and the active zones closest to the price on each side."""
    snapshot = trace[0]["result"]
    metrics = snapshot["timeframes"][request["timeframe"]]["metrics"]
    levels = next((run["result"] for run in trace if run["tool"] == "support_resistance"), {})
    price = _decimal(levels.get("reference_price")) or _decimal(metrics.get("last_close"))
    active = [zone for zone in levels.get("levels", []) if zone.get("zone_state") == "active"]

    def nearby(kind: str) -> list[dict]:
        zones = [zone for zone in active if zone.get("kind") == kind
                 and _decimal(zone.get("low")) is not None and _decimal(zone.get("high")) is not None]
        if price is not None:
            zones.sort(key=lambda zone: abs(_decimal(zone.get("center") or zone["low"]) - price))
        return [{"low": zone["low"], "high": zone["high"]} for zone in zones[:NEARBY_LEVELS]]

    return {"trend": metrics.get("trend"), "ma20": metrics.get("ma20"), "ma50": metrics.get("ma50"),
            "atr14": metrics.get("atr14"),
            "resistance": nearby("resistance"), "support": nearby("support")}


def _partial_string(text: str, start: int) -> tuple[str, bool]:
    """The JSON string beginning at `start`, decoded so far, and whether it is closed."""
    raw, index = [], start
    while index < len(text):
        char = text[index]
        if char == '"':
            return _decode(raw), True
        if char == "\\":
            escape = text[index:index + 2] if text[index + 1:index + 2] != "u" else text[index:index + 6]
            if len(escape) < 2 or (escape[1] == "u" and len(escape) < 6):
                break  # An escape still being written.
            raw.append(escape)
            index += len(escape)
            continue
        raw.append(char)
        index += 1
    return _decode(raw), False


def _decode(parts: list[str]) -> str:
    try:
        return json.loads('"' + "".join(parts) + '"')
    except ValueError:
        return "".join(part for part in parts if not part.startswith("\\"))


def draft_fields(text: str) -> dict:
    """Report sections already present in a JSON report that is still being written."""
    result = {}
    for field, pattern in _STRING_START.items():
        match = pattern.search(text)
        if not match:
            continue
        value, closed = _partial_string(text, match.end())
        value = value.strip()
        if value:
            result[field] = {"text": value[:MAX_DRAFT_CHARS], "complete": closed}
    return result


class ProgressRecorder:
    """Writes a running analysis's progress; a failed write never affects the analysis."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.progress: dict = {}
        self.last_draft_write = 0.0
        self.last_draft: dict = {}

    def record(self, **sections) -> None:
        self.progress.update(sections)
        self._write()

    def model_text(self, text: str) -> None:
        draft = draft_fields(text)
        if not draft or draft == self.last_draft:
            return
        # Every finished section is written at once; text inside a section at most once a second.
        finished = any(item["complete"] and not self.last_draft.get(field, {}).get("complete")
                       for field, item in draft.items())
        if not finished and monotonic() - self.last_draft_write < DRAFT_INTERVAL_SECONDS:
            return
        self.last_draft, self.last_draft_write = draft, monotonic()
        self.progress["draft"] = draft
        self._write()

    def _write(self) -> None:
        try:
            with connect() as db:
                db.execute("UPDATE analyses SET progress_json=? WHERE id=? AND status='running'",
                           (json.dumps(self.progress, ensure_ascii=False), self.job_id))
                db.commit()
        except Exception:  # noqa: BLE001, S110 -- progress is a convenience; the report is what counts
            pass
