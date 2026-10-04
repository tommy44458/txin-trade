import hashlib
import hmac
import json
import os
import sqlite3
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .anthropic_api_bridge import router as anthropic_auth_router
from .binance import BinanceError
from .binance import test_connection as test_binance_connection
from .binance_sync import BinanceAccountChangedError, BinanceSyncSupersededError
from .binance_sync import sync_positions as sync_binance_positions
from .bingx import BingXError
from .bingx_sync import sync_positions as sync_bingx_positions
from .chatgpt_plan_auth import router as chatgpt_plan_auth_router
from .claude_code_bridge import router as claude_code_auth_router
from .cloud_account import router as cloud_account_router
from .cloud_account import signed_in_profile
from .cloud_connector import connector as cloud_connector
from .cloud_connector import router as cloud_remote_router
from .codex_bridge import router as codex_auth_router
from .codex_bridge import shutdown as shutdown_codex
from .config import assert_local_mode, local_user_id
from .credential_migration import router as credential_migration_router
from .credential_store import CredentialStoreError
from .db import connect, database_path, init_db, new_id, public_job, utc_now
from .desktop_updates import (
    require_task_start_allowed,
    reset_desktop_update_gate,
    update_is_draining,
)
from .desktop_updates import router as desktop_updates_router
from .discussions import router as discussions_router
from .events import event_snapshot
from .fund_flows import router as fund_flow_snapshots_router
from .indicator_preferences import (
    INITIAL_INDICATOR_CATALOG_VERSION,
    INITIAL_INDICATOR_DEFAULT_PARAMETERS,
)
from .integration_sync_state import last_sync, with_exchange_estimates
from .live_market_context import router as live_market_context_router
from .local_settings import (
    binance_sync_credentials,
    initial_indicators,
    integration_status,
    preferences,
)
from .local_settings import router as settings_router
from .macro_interpretation import router as macro_interpretation_router
from .market import fetch_candles, fetch_quote
from .market_catalog import (
    MarketCatalogUnavailable,
    get_catalog,
    valid_market_id_format,
    validate_market_id,
)
from .models import AnalysisRequest, PositionInput, PositionUpdate
from .news import news_snapshot
from .openai_api_bridge import router as openai_auth_router
from .outcome_api import router as outcome_router
from .outcome_upload import router as outcome_sharing_router
from .position_chart_snapshot import router as position_chart_snapshot_router
from .product_version import product_version
from .smart_money import router as smart_money_router


@asynccontextmanager
async def lifespan(_app: FastAPI):
    assert_local_mode()
    init_db()
    reset_desktop_update_gate()
    # Idle unless the user enabled remote access while signed in.
    cloud_connector.start()
    yield
    cloud_connector.stop()
    shutdown_codex()


# The packaged app publishes no API documentation; browser development keeps it.
_API_DOCS = os.getenv("APP_DESKTOP") != "1"
app = FastAPI(title="txinTrade local API", version=product_version(), lifespan=lifespan,
              docs_url="/docs" if _API_DOCS else None, redoc_url="/redoc" if _API_DOCS else None,
              openapi_url="/openapi.json" if _API_DOCS else None)
app.include_router(settings_router)
app.include_router(credential_migration_router)
app.include_router(claude_code_auth_router)
app.include_router(anthropic_auth_router)
app.include_router(chatgpt_plan_auth_router)
app.include_router(openai_auth_router)
# Cloud account and remote access routes are never described in API documentation.
app.include_router(cloud_remote_router, include_in_schema=False)
app.include_router(cloud_account_router, include_in_schema=False)
app.include_router(codex_auth_router)
app.include_router(live_market_context_router)
app.include_router(macro_interpretation_router)
app.include_router(position_chart_snapshot_router)
app.include_router(discussions_router)
app.include_router(desktop_updates_router)
app.include_router(smart_money_router)
app.include_router(fund_flow_snapshots_router)
app.include_router(outcome_router)
app.include_router(outcome_sharing_router)


@app.exception_handler(RequestValidationError)
async def safe_validation_error(_request: Request, exc: RequestValidationError):
    # FastAPI normally echoes invalid input. Settings include credentials, which
    # must never appear in a response or in the renderer's error diagnostics.
    errors = [{key: error[key] for key in ("loc", "msg", "type") if key in error}
              for error in exc.errors()]
    return JSONResponse({"detail": errors}, status_code=422)


@app.exception_handler(CredentialStoreError)
async def credential_error(_request: Request, exc: CredentialStoreError):
    return JSONResponse({"detail": str(exc)}, status_code=503)


@app.exception_handler(MarketCatalogUnavailable)
async def market_catalog_error(_request: Request, exc: MarketCatalogUnavailable):
    return JSONResponse({"detail": str(exc)}, status_code=503)


def job_with_freshness(db, row: dict) -> dict:
    result = public_job(row)
    reasons = []
    for saved in json.loads(row["positions_json"]):
        current = db.execute(
            "SELECT version,status FROM positions WHERE id=? AND user_id=?",
            (saved["id"], row["user_id"]),
        ).fetchone()
        if current is None or current["status"] != "open" or current["version"] != saved["version"]:
            reasons.append("position_changed")
            break
    result["freshness"] = "stale" if reasons else "fresh"
    result["stale_reasons"] = reasons
    return result


@app.middleware("http")
async def local_only(request: Request, call_next):
    # Development identity is trusted only from loopback and cannot be chosen by a caller.
    if request.client and request.client.host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        return JSONResponse({"detail": "Local-only API"}, status_code=403)
    if request.url.hostname not in {"127.0.0.1", "::1", "localhost", "testserver"}:
        return JSONResponse({"detail": "Local-only host"}, status_code=403)
    if os.getenv("APP_DESKTOP") == "1" and request.url.path.startswith("/api/"):
        expected = os.getenv("APP_DESKTOP_TOKEN", "")
        supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not expected or not hmac.compare_digest(supplied.encode(), expected.encode()):
            return JSONResponse({"detail": "Desktop session authorization required"}, status_code=401)
        if (request.method not in {"GET", "HEAD", "OPTIONS"}
                and not request.url.path.startswith("/api/v1/desktop-updates/")):
            try:
                # Settings can initialize an empty local store lazily. A missing
                # database cannot contain a gate; this check must not create it
                # or introduce credential side effects before the route runs.
                if database_path().exists():
                    with connect(readonly=True) as db:
                        if update_is_draining(db):
                            return JSONResponse({"detail": {"code": "DESKTOP_UPDATE_PREPARING",
                                "message": "The application is preparing an update."}}, status_code=409)
            except (OSError, sqlite3.Error):
                return JSONResponse({"detail": {"code": "UPDATE_STATE_UNAVAILABLE",
                    "message": "Unable to verify the desktop update status."}}, status_code=503)
    return await call_next(request)


@app.get("/api/v1/health")
def health():
    return {"status": "ok", "mode": "local"}


@app.get("/api/v1/session")
def session():
    # The cloud account is display-only here; it grants nothing in the local app.
    return {"mode": "local", "user_id": local_user_id(), "cloud_account": signed_in_profile()}


@app.get("/api/v1/events")
def events():
    return event_snapshot(datetime.now(UTC))


@app.get("/api/v1/news")
def news(market_id: str | None = None):
    if market_id is not None:
        try:
            validate_market_id(market_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    return news_snapshot(datetime.now(UTC), market_id)


@app.get("/api/v1/markets")
def markets(response: Response):
    catalog = get_catalog()
    response.headers["X-Market-Catalog-Status"] = catalog.status
    response.headers["X-Market-Catalog-Updated-At"] = catalog.updated_at
    response.headers["Cache-Control"] = "no-cache"
    return catalog.markets


@app.get("/api/v1/candles")
def candles(market_id: str, timeframe: str = "1h", limit: int = Query(200, ge=60, le=500)):
    try:
        return {"market_id": market_id, "timeframe": timeframe, "source": "Binance USDⓈ-M perpetual public API", "candles": fetch_candles(market_id, timeframe, limit)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, "Market data is unavailable") from exc


@app.get("/api/v1/quotes")
def quotes(market_id: str):
    try:
        return fetch_quote(market_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(503, "Market data is unavailable") from exc


@app.post("/api/v1/analyses", status_code=202)
def create_analysis(body: AnalysisRequest, idempotency_key: str = Header(..., min_length=8, max_length=128)):
    user_id = local_user_id()
    payload = body.model_dump(mode="json")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    # Hash the submitted input before applying settings. Retrying an omitted
    # selection must return the same job even if settings have since changed.
    request_hash = hashlib.sha256(canonical.encode()).hexdigest()
    with connect() as db:
        require_task_start_allowed(db)
        existing = db.execute("SELECT * FROM analyses WHERE user_id=? AND idempotency_key=?", (user_id, idempotency_key)).fetchone()
        if existing:
            if not _same_analysis_input(existing, payload, request_hash):
                raise HTTPException(409, "Idempotency key was used for different input")
            return job_with_freshness(db, existing)
        frozen_payload = dict(payload)
        if frozen_payload["initial_indicators"] is None:
            frozen_payload["initial_indicators"] = initial_indicators(db)
        frozen_payload["initial_indicator_parameters"] = {
            name: deepcopy(INITIAL_INDICATOR_DEFAULT_PARAMETERS[name])
            for name in frozen_payload["initial_indicators"]
        }
        frozen_payload["initial_indicator_catalog_version"] = INITIAL_INDICATOR_CATALOG_VERSION
        frozen_canonical = json.dumps(frozen_payload, sort_keys=True, separators=(",", ":"))
        position_snapshot = []
        if body.kind == "positions":
            rows = db.execute(
                f"SELECT * FROM positions WHERE user_id=? AND status='open' AND id IN ({','.join('?' for _ in body.position_ids)})",
                (user_id, *body.position_ids),
            ).fetchall()
            if len(rows) != len(body.position_ids) or any(row["market_id"] != body.market_id for row in rows):
                raise HTTPException(404, "One or more positions were not found in this market")
            by_id = {row["id"]: dict(row) for row in rows}
            position_snapshot = with_exchange_estimates(
                db, [by_id[position_id] for position_id in body.position_ids])
        job_id = new_id("ana")
        from .prompts import resolve_prompt
        from .prompts_artifacts import save_prompt_artifact

        task = "strategy_positions" if body.kind == "positions" else "strategy_market"
        bundle = resolve_prompt(task, response_locale=body.output_locale, inputs=frozen_payload)
        artifact_id = save_prompt_artifact(db, bundle)
        inserted = db.execute(
            "INSERT INTO analyses(id,user_id,idempotency_key,request_hash,request_json,positions_json,status,phase,created_at,prompt_artifact_id) VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT (user_id,idempotency_key) DO NOTHING",
            (job_id, user_id, idempotency_key, request_hash, frozen_canonical, json.dumps(position_snapshot), "queued", "waiting", utc_now(), artifact_id),
        )
        if inserted.rowcount == 0:
            existing = db.execute(
                "SELECT * FROM analyses WHERE user_id=? AND idempotency_key=?",
                (user_id, idempotency_key),
            ).fetchone()
            if not _same_analysis_input(existing, payload, request_hash):
                raise HTTPException(409, "Idempotency key was used for different input")
            return job_with_freshness(db, existing)
        db.commit()
        return job_with_freshness(db, db.execute("SELECT * FROM analyses WHERE id=?", (job_id,)).fetchone())


def _same_analysis_input(row: dict, payload: dict, request_hash: str) -> bool:
    if row["request_hash"] == request_hash:
        return True
    # Replaying an old request after upgrading must keep its original job.
    # Legacy requests predate the response locale and indicator selection.
    saved = json.loads(row["request_json"])
    saved.setdefault("output_locale", "zh-TW")
    saved.setdefault("initial_indicators", None)
    # Server-frozen numerical defaults are not submitted controls. They cannot
    # change whether an otherwise identical client request is a retry.
    saved.pop("initial_indicator_parameters", None)
    saved.pop("initial_indicator_catalog_version", None)
    return saved == payload


@app.get("/api/v1/analyses")
def list_analyses():
    with connect(readonly=True) as db:
        rows = db.execute("SELECT * FROM analyses WHERE user_id=? ORDER BY created_at DESC LIMIT 50", (local_user_id(),)).fetchall()
        return [job_with_freshness(db, row) for row in rows]


@app.get("/api/v1/analyses/latest")
def latest_analysis(
    response: Response,
    market_id: str,
    kind: Literal["market", "positions"] = "positions",
):
    # Saved reports remain readable after a market is delisted. Checking their
    # identity must not refresh the exchange catalog or start another analysis.
    if not valid_market_id_format(market_id):
        raise HTTPException(422, "Unsupported market ID")
    response.headers["Cache-Control"] = "no-store"
    with connect(readonly=True) as db:
        row = db.execute(
            """SELECT * FROM analyses
               WHERE user_id=? AND status='completed'
                 AND CASE WHEN json_valid(request_json) AND json_valid(report_json)
                     THEN json_type(request_json)='object'
                      AND json_type(report_json)='object'
                      AND json_extract(request_json,'$.kind')=?
                      AND json_extract(request_json,'$.market_id')=?
                      AND (json_type(report_json,'$.analysis_kind') IS NULL
                           OR json_extract(report_json,'$.analysis_kind')=?)
                      AND json_extract(report_json,'$.market_id')=?
                      AND json_extract(report_json,'$.timeframe')=
                          json_extract(request_json,'$.timeframe')
                     ELSE 0 END
               ORDER BY created_at DESC, completed_at DESC, id DESC LIMIT 1""",
            (local_user_id(), kind, market_id, kind, market_id),
        ).fetchone()
        # The newest submitted analysis wins even if an older, slower job
        # finishes later. Existing freshness flags still expose changed positions.
        return job_with_freshness(db, row) if row else None


@app.get("/api/v1/analyses/{analysis_id}")
def get_analysis(analysis_id: str):
    with connect(readonly=True) as db:
        row = db.execute("SELECT * FROM analyses WHERE id=? AND user_id=?", (analysis_id, local_user_id())).fetchone()
        if not row:
            raise HTTPException(404, "Analysis not found")
        return job_with_freshness(db, row)


@app.get("/api/v1/analyses/{analysis_id}/level-shadow")
def get_level_shadow(analysis_id: str):
    with connect(readonly=True) as db:
        row = db.execute("SELECT shadow_json FROM analyses WHERE id=? AND user_id=?",
                         (analysis_id, local_user_id())).fetchone()
        if not row:
            raise HTTPException(404, "Analysis not found")
        return json.loads(row["shadow_json"]) if row["shadow_json"] else {"status": "pending"}


@app.get("/api/v1/analyses/{analysis_id}/level-shadow/v4")
def get_level_shadow_v4(analysis_id: str):
    with connect(readonly=True) as db:
        row = db.execute("SELECT v4_status,v4_json FROM analyses WHERE id=? AND user_id=?",
                         (analysis_id, local_user_id())).fetchone()
        if not row:
            raise HTTPException(404, "Analysis not found")
        return json.loads(row["v4_json"]) if row["v4_json"] else {"status": row["v4_status"] or "not_requested"}


def check_positive(value: str, field: str) -> str:
    try:
        number = Decimal(value)
        if not number.is_finite() or number <= 0:
            raise ValueError()
    except (InvalidOperation, ValueError) as exc:
        raise HTTPException(422, f"{field} must be a positive number") from exc
    return str(number)


def position_values(body: PositionInput) -> dict:
    result = body.model_dump()
    for field in ("entry_price", "quantity", "stop_loss", "take_profit",
                  "exchange_liquidation_price"):
        if result[field] is not None:
            result[field] = check_positive(result[field], field)
    result["entry_time"] = body.entry_time.astimezone(UTC).isoformat() if body.entry_time else None
    return result


@app.get("/api/v1/positions")
def list_positions():
    with connect(readonly=True) as db:
        rows = db.execute("SELECT * FROM positions WHERE user_id=? AND status='open' AND market_id LIKE 'binance:perp:%' ORDER BY created_at DESC", (local_user_id(),)).fetchall()
        return with_exchange_estimates(db, rows)


@app.get("/api/v1/integrations/bingx")
def bingx_status():
    return {**integration_status("bingx"), "contracts": ["perpetual", "standard"],
            "last_sync": last_sync("bingx")}


@app.get("/api/v1/integrations/binance")
def binance_status():
    # Status is a local metadata read. Loading the page must never decrypt keys
    # or make a private exchange request.
    scope = preferences().get("binance_account_scope")
    return {**integration_status("binance"), "contracts": ["perpetual"],
            "last_sync": last_sync("binance", scope)}


def _binance_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, BinanceError):
        return HTTPException(503, exc.as_dict())
    if isinstance(exc, BinanceAccountChangedError):
        return HTTPException(409, {"code": "BINANCE_ACCOUNT_CHANGED",
                                  "message": "Binance account changed; synchronize again",
                                  "exchange_code": None, "retry_after": None})
    if isinstance(exc, BinanceSyncSupersededError):
        return HTTPException(409, {"code": "BINANCE_SYNC_SUPERSEDED",
                                  "message": "A newer Binance synchronization has completed",
                                  "exchange_code": None, "retry_after": None})
    return HTTPException(503, {"code": "BINANCE_CREDENTIALS_UNAVAILABLE",
                              "message": str(exc), "exchange_code": None, "retry_after": None})


@app.post("/api/v1/integrations/binance/test")
def test_binance():
    try:
        credentials, scope = binance_sync_credentials()
        result = test_binance_connection(credentials=credentials)
        from .local_settings import binance_connection_is_current

        with connect(readonly=True) as db:
            if not binance_connection_is_current(db, credentials, scope):
                raise BinanceAccountChangedError("Binance account changed; test the connection again")
        return result
    except (BinanceError, CredentialStoreError, BinanceAccountChangedError) as exc:
        raise _binance_http_error(exc) from exc


@app.post("/api/v1/positions/binance/sync")
def sync_binance():
    try:
        return sync_binance_positions()
    except (BinanceError, CredentialStoreError, BinanceAccountChangedError,
            BinanceSyncSupersededError) as exc:
        raise _binance_http_error(exc) from exc


@app.post("/api/v1/positions/bingx/sync")
def sync_bingx():
    try:
        return sync_bingx_positions()
    except BingXError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/v1/positions", status_code=201)
def create_position(body: PositionInput):
    values = position_values(body)
    position_id, now = new_id("pos"), utc_now()
    with connect() as db:
        db.execute(
            "INSERT INTO positions(id,user_id,market_id,version,side,leverage,margin_mode,entry_price,quantity,stop_loss,take_profit,entry_time,exchange_liquidation_price,notes,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (position_id, local_user_id(), values["market_id"], 1, values["side"], values["leverage"], values["margin_mode"], values["entry_price"], values["quantity"], values["stop_loss"], values["take_profit"], values["entry_time"], values["exchange_liquidation_price"], values["notes"], "open", now, now),
        )
        db.commit()
        return dict(db.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone())


@app.patch("/api/v1/positions/{position_id}")
def update_position(position_id: str, body: PositionUpdate):
    values = position_values(body)
    with connect() as db:
        old = db.execute(
            "SELECT * FROM positions WHERE id=? AND user_id=? AND status='open'",
            (position_id, local_user_id()),
        ).fetchone()
        if old is None:
            raise HTTPException(409, "Position is missing or version changed")
        if old["source"] != "manual":
            protected = ("market_id", "side", "leverage", "margin_mode", "entry_price",
                         "quantity", "entry_time", "exchange_liquidation_price")
            for field in protected:
                values[field] = old[field]
        previous_stop = (old["stop_loss"] if old["stop_loss"] != values["stop_loss"] else None)
        result = db.execute(
            "UPDATE positions SET market_id=?, side=?, leverage=?, margin_mode=?, entry_price=?, quantity=?, stop_loss=?, take_profit=?, entry_time=?, exchange_liquidation_price=?, notes=?, previous_stop_loss=?, version=version+1, updated_at=? WHERE id=? AND user_id=? AND status='open' AND version=?",
            (values["market_id"], values["side"], values["leverage"], values["margin_mode"], values["entry_price"], values["quantity"], values["stop_loss"], values["take_profit"], values["entry_time"], values["exchange_liquidation_price"], values["notes"], previous_stop, utc_now(), position_id, local_user_id(), body.expected_version),
        )
        if result.rowcount == 0:
            raise HTTPException(409, "Position is missing or version changed")
        db.commit()
        return with_exchange_estimates(
            db, [db.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()])[0]


@app.post("/api/v1/positions/{position_id}/close")
def close_position(position_id: str):
    with connect() as db:
        row = db.execute("SELECT source FROM positions WHERE id=? AND user_id=? AND status='open'", (position_id, local_user_id())).fetchone()
        if row and row["source"] != "manual":
            message = ("BingX 持倉只能由交易所同步平倉狀態" if row["source"] == "bingx" else
                       "Imported positions can only be closed by synchronizing the exchange")
            raise HTTPException(409, message)
        result = db.execute("UPDATE positions SET status='closed', version=version+1, updated_at=? WHERE id=? AND user_id=? AND status='open'", (utc_now(), position_id, local_user_id()))
        if result.rowcount == 0:
            raise HTTPException(404, "Position not found")
        db.commit()
    return {"status": "closed"}


@app.delete("/api/v1/positions/{position_id}")
def delete_position(position_id: str):
    """Erase a personal position and reports containing its copied snapshot."""
    user_id = local_user_id()
    with connect() as db:
        position = db.execute(
            "SELECT id FROM positions WHERE id=? AND user_id=?",
            (position_id, user_id),
        ).fetchone()
        if not position:
            raise HTTPException(404, "Position not found")
        jobs = db.execute(
            "SELECT id,positions_json FROM analyses WHERE user_id=?",
            (user_id,),
        ).fetchall()
        for job in jobs:
            if any(saved["id"] == position_id for saved in json.loads(job["positions_json"])):
                db.execute("DELETE FROM analyses WHERE id=? AND user_id=?", (job["id"], user_id))
        db.execute("DELETE FROM positions WHERE id=? AND user_id=?", (position_id, user_id))
        db.commit()
    return {"status": "deleted"}
