"""Allowlisted remote commands, run against the local app on behalf of the cloud relay.

Each operation maps to an existing local function; nothing accepts a URL, path,
header, file or shell command. Results are compact projections so they fit the
relay's frame limit; full records stay on this computer.
"""

import json

from fastapi import HTTPException

from .local_settings import trading_preferences
from .models import AnalysisRequest
from .product_version import product_version

MAX_RESULT_BYTES = 240_000


class CommandFailed(Exception):
    """Carries one of the relay's desktop failure codes."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _analysis_summary(job: dict) -> dict:
    submitted = job.get("submitted_input") or {}
    report = job.get("report") or {}
    entry = report.get("entry_decision") or {}
    return {
        "id": job.get("id"), "status": job.get("status"), "kind": submitted.get("kind"),
        "market_id": submitted.get("market_id"), "timeframe": submitted.get("timeframe"),
        "created_at": job.get("created_at"), "completed_at": job.get("completed_at"),
        "agent_stance": report.get("agent_stance"), "entry_action": entry.get("action"),
    }


def _analysis_detail(job: dict) -> dict:
    report = job.get("report") or {}
    reasoning = report.get("reasoning") or {}
    detail = {key: report.get(key) for key in (
        "market_id", "timeframe", "generated_at", "agent_stance", "entry_decision",
        "entry_risk_reference", "preference_assessment", "market_reference")}
    detail["reasoning"] = {key: reasoning.get(key) for key in (
        "market", "levels", "strategy", "supporting_evidence", "counter_evidence",
        "direction_assessment", "macro_outlook")}
    detail["quote"] = {key: (report.get("quote") or {}).get(key) for key in ("price", "observed_at")}
    return {**_analysis_summary(job), "error": job.get("error"), "report": detail if report else None}


def _start_analysis(command: dict, idempotency_key: str) -> dict:
    # Imported here: the API module also starts the connector that calls this.
    from .api import create_analysis

    saved = trading_preferences()
    # A saved left/right style is no longer offered; the chosen strategy decides the entry style.
    request = AnalysisRequest(
        kind="market", market_id=command["market_id"], timeframe=command["timeframe"],
        output_locale=command["output_locale"],
        directional_bias=saved.get("directional_bias"), risk_tolerance=saved.get("risk_tolerance"),
        trading_strategy=saved.get("trading_strategy"), leverage=saved.get("leverage") or 5,
    )
    job = create_analysis(request, idempotency_key=idempotency_key)
    return {"analysis_id": job["id"], "status": job["status"]}


def run_command(command: dict, idempotency_key: str) -> dict:
    """Return a JSON-serializable result or raise CommandFailed."""
    from .api import get_analysis, list_analyses, list_positions

    operation = command.get("operation")
    try:
        if operation == "status.read":
            # Capabilities let the remote page tell an older app it needs updating.
            result = {"app": "txinTrade", "version": product_version(), "online": True,
                      "capabilities": ["api.request", "stream"]}
        elif operation == "positions.list":
            result = {"positions": list_positions()}
        elif operation == "analyses.list":
            limit = command.get("limit", 20)
            if not isinstance(limit, int) or not 1 <= limit <= 50:
                raise CommandFailed("unsupported_operation")
            result = {"analyses": [_analysis_summary(job) for job in list_analyses()[:limit]]}
        elif operation == "analyses.get":
            result = {"analysis": _analysis_detail(get_analysis(str(command.get("analysis_id"))))}
        elif operation == "analyses.start":
            result = _start_analysis(command, idempotency_key)
        else:
            raise CommandFailed("unsupported_operation")
    except CommandFailed:
        raise
    except HTTPException as exc:
        # Missing records and an update in progress are reported, not hidden.
        raise CommandFailed("local_unavailable" if exc.status_code in {404, 409, 503} else "local_error") from None
    except Exception:  # noqa: BLE001 - never forward local error details to the cloud
        raise CommandFailed("local_error") from None
    if len(json.dumps(result, ensure_ascii=False, default=str).encode()) > MAX_RESULT_BYTES:
        raise CommandFailed("local_error")
    return json.loads(json.dumps(result, ensure_ascii=False, default=str))
