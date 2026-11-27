"""Persistent, user-submitted discussion of one frozen analysis version.

Only the worker opens a model session. Claims and commits use short SQLite
transactions; interrupted calls require an explicit retry because their account
usage cannot safely be inferred from a missing response.
"""

import asyncio
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from threading import Lock, Timer
from time import monotonic
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from openai import APIConnectionError, APITimeoutError, AuthenticationError, OpenAI, RateLimitError
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from .config import local_user_id
from .db import connect, new_id, utc_now
from .discussion_context import DISCUSSION_INSTRUCTIONS, build_discussion_input, load_subject
from .discussion_market import (
    NOT_REFRESHED,
    SNAPSHOT_VERSION,
    SOURCE,
    VERSION,
    fetch_discussion_market,
    unavailable_snapshot,
)
from .model_providers import ModelSession, analysis_timeout_seconds
from .prompts.registry import resolve_prompt
from .prompts_artifacts import load_prompt_artifact, save_prompt_artifact
from .task_locale_errors import localized_task_error

router = APIRouter(prefix="/api/v1/discussions", tags=["Analysis discussion"])
SubjectType = Literal["analysis", "macro", "fund_flows"]
HISTORY_TURNS = 40
HISTORY_MAX_CHARS = 60_000
STREAM_POLL_SECONDS = 0.3
STREAM_HEARTBEAT_SECONDS = 10
PREFIX_FLUSH_SECONDS = 0.2
PREFIX_FLUSH_CHARS = 128
LIVE_MARKET_MAX_SECONDS = 8.0
# A follow-up under professional_discussion_v9 or later also gets a fresh market snapshot.
LIVE_SNAPSHOT_MAX_SECONDS = 25.0
LIVE_SNAPSHOT_PROMPT_VERSION = 9
INTERRUPTED_ERROR = (
    "DISCUSSION_RESULT_UNKNOWN",
    "模型回覆中斷，無法確認這次呼叫的用量；請檢查後手動重試。",
)


class DiscussionMessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=6000)
    request_id: UUID
    output_locale: Literal["zh-TW", "en-US"] | None = None

    @field_validator("message")
    @classmethod
    def nonblank_message(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("請輸入追問內容。")
        return value


def _session(db, subject_type: str, subject_id: str, user_id: str) -> dict | None:
    return db.execute(
        "SELECT * FROM discussion_sessions WHERE user_id=? AND subject_type=? AND subject_id=?",
        (user_id, subject_type, subject_id),
    ).fetchone()


def _public_message(row: dict) -> dict:
    if row["status"] == "running" and (not row["lease_until"] or row["lease_until"] <= utc_now()):
        # GET exposes interrupted work immediately without mutating storage or
        # requiring a worker process to be alive. A POST retry persists it.
        row = dict(row, status="failed", error_code=INTERRUPTED_ERROR[0],
                   error_message=INTERRUPTED_ERROR[1], error_retryable=1)
    return {
        "id": row["id"], "sequence": row["sequence"], "role": row["role"],
        "content": row["content"], "status": row["status"],
        "created_at": row["created_at"], "completed_at": row["completed_at"],
        "provider": row["provider"], "model": row["model"],
        "output_locale": row.get("output_locale") or "zh-TW",
        "prompt_artifact_id": row.get("prompt_artifact_id"),
        "prompt_bundle": json.loads(row.get("prompt_bundle_json") or "null"),
        "live_market": json.loads(row.get("live_market_json") or "null"),
        "request_id": row.get("turn_request_id") or row["request_id"],
        "error": {"code": row["error_code"], "message": localized_task_error(
            row["error_code"], row.get("output_locale") or "zh-TW", row["error_message"]),
                  "retryable": bool(row["error_retryable"])} if row["error_code"] else None,
    }


def _state(db, subject: dict, session: dict | None, *, limit: int = 50,
           before_sequence: int | None = None) -> dict:
    rows, busy = [], False
    if session:
        rows = db.execute(
            """SELECT m.*, COALESCE(m.request_id,u.request_id) AS turn_request_id
               FROM discussion_messages m LEFT JOIN discussion_messages u ON u.id=m.reply_to_id
               WHERE m.session_id=? AND (? IS NULL OR m.sequence<?)
               ORDER BY m.sequence DESC LIMIT ?""",
            (session["id"], before_sequence, before_sequence, limit + 1),
        ).fetchall()
        busy = db.execute(
            """SELECT 1 FROM discussion_messages WHERE session_id=?
               AND (status='queued' OR (status='running' AND lease_until>?))""",
            (session["id"], utc_now()),
        ).fetchone() is not None
    has_more = len(rows) > limit
    rows = list(reversed(rows[:limit]))
    return {
        "subject": json.loads(session["subject_json"]) if session else subject,
        "session": {key: session[key] for key in ("id", "created_at", "updated_at", "output_locale")}
        if session else None,
        "messages": [_public_message(row) for row in rows], "busy": busy,
        "has_more": has_more,
        "before_sequence": rows[0]["sequence"] if has_more else None,
    }


@router.get("/{subject_type}/{subject_id}")
def get_discussion(subject_type: SubjectType, subject_id: str,
                   limit: int = Query(50, ge=1, le=100),
                   before_sequence: int | None = Query(None, ge=1)):
    with connect(readonly=True) as db:
        user_id = local_user_id()
        subject, _ = load_subject(db, subject_type, subject_id, user_id)
        return _state(db, subject, _session(db, subject_type, subject_id, user_id),
                      limit=limit, before_sequence=before_sequence)


def _stream_state(subject_type: str, subject_id: str, user_id: str, *, limit: int = 50) -> dict:
    # Return a detached snapshot. No SQLite transaction survives an SSE yield.
    with connect(readonly=True) as db:
        subject, _ = load_subject(db, subject_type, subject_id, user_id)
        return _state(db, subject, _session(db, subject_type, subject_id, user_id), limit=limit)


def _state_event(state: dict) -> str:
    return "event: state\ndata: " + json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n\n"


async def _discussion_events(request: Request, subject_type: str, subject_id: str,
                             user_id: str, initial: dict, *, limit: int = 50):
    state, previous, heartbeat = initial, None, monotonic()
    while not await request.is_disconnected():
        event = _state_event(state)
        if event != previous:
            yield event
            previous = event
            heartbeat = monotonic()
        if not state["busy"]:
            return
        if monotonic() - heartbeat >= STREAM_HEARTBEAT_SECONDS:
            yield ": heartbeat\n\n"
            heartbeat = monotonic()
        await asyncio.sleep(STREAM_POLL_SECONDS)
        if await request.is_disconnected():
            return
        try:
            state = await run_in_threadpool(
                _stream_state, subject_type, subject_id, user_id, limit=limit)
        except HTTPException as exc:
            # The source can be deleted while connected. Emit only its safe,
            # application-authored ownership/readiness error, then close.
            detail = exc.detail if isinstance(exc.detail, dict) else {"code": "DISCUSSION_UNAVAILABLE"}
            yield "event: error\ndata: " + json.dumps(detail, ensure_ascii=False) + "\n\n"
            return


@router.get("/{subject_type}/{subject_id}/stream")
def stream_discussion(request: Request, subject_type: SubjectType, subject_id: str,
                      limit: int = Query(50, ge=1, le=100)):
    user_id = local_user_id()
    # Validate before sending HTTP 200; missing/foreign subjects return 404 and
    # unfinished source reports return 409 just like the normal read endpoint.
    initial = _stream_state(subject_type, subject_id, user_id, limit=limit)
    return StreamingResponse(
        _discussion_events(request, subject_type, subject_id, user_id, initial, limit=limit),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{subject_type}/{subject_id}/messages", status_code=202)
def send_message(subject_type: SubjectType, subject_id: str, body: DiscussionMessageInput):
    user_id, request_id = local_user_id(), str(body.request_id)
    with connect() as db:
        from .desktop_updates import require_task_start_allowed

        require_task_start_allowed(db)
        _expire_claims(db, utc_now())
        subject, context = load_subject(db, subject_type, subject_id, user_id,
                                        output_locale=body.output_locale
                                        if subject_type in ("macro", "fund_flows") else None)
        session = _session(db, subject_type, subject_id, user_id)
        if session:
            existing = db.execute(
                "SELECT * FROM discussion_messages WHERE session_id=? AND request_id=?",
                (session["id"], request_id),
            ).fetchone()
            if existing:
                if existing["content"] != body.message:
                    raise HTTPException(409, "此 request_id 已用於不同追問。")
                return _state(db, subject, session)
            if db.execute(
                "SELECT 1 FROM discussion_messages WHERE session_id=? AND status IN ('queued','running')",
                (session["id"],),
            ).fetchone():
                raise HTTPException(409, "此分析的追問仍在處理，請等待完成。")
        now = utc_now()
        if not session:
            session_id = new_id("dis")
            db.execute(
                """INSERT INTO discussion_sessions
                   (id,user_id,subject_type,subject_id,analysis_id,macro_id,fund_flow_id,subject_json,
                    context_json,created_at,updated_at,output_locale) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (session_id, user_id, subject_type, subject_id,
                 subject_id if subject_type == "analysis" else None,
                 subject_id if subject_type == "macro" else None,
                 subject_id if subject_type == "fund_flows" else None,
                 json.dumps(subject, ensure_ascii=False), json.dumps(context, ensure_ascii=False),
                 now, now, context.get("output_locale") or "zh-TW"),
            )
            session = _session(db, subject_type, subject_id, user_id)
        bundle = resolve_prompt("discussion", response_locale=session["output_locale"])
        artifact_id = save_prompt_artifact(db, bundle)
        bundle_json = json.dumps(bundle.metadata(), ensure_ascii=False)
        next_sequence = db.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 AS next FROM discussion_messages WHERE session_id=?",
            (session["id"],),
        ).fetchone()["next"]
        user_message_id = new_id("msg")
        db.execute(
            """INSERT INTO discussion_messages
               (id,session_id,sequence,role,content,status,request_id,created_at,completed_at,output_locale,
                prompt_artifact_id,prompt_bundle_json)
               VALUES(?,?,?,'user',?,'completed',?,?,?,?,?,?)""",
            (user_message_id, session["id"], next_sequence, body.message, request_id, now, now,
             session["output_locale"], artifact_id, bundle_json),
        )
        db.execute(
            """INSERT INTO discussion_messages
               (id,session_id,sequence,role,status,reply_to_id,created_at,output_locale,prompt_artifact_id,
                prompt_bundle_json)
               VALUES(?,?,?,'assistant','queued',?,?,?,?,?)""",
            (new_id("msg"), session["id"], next_sequence + 1, user_message_id, now,
             session["output_locale"], artifact_id, bundle_json),
        )
        db.execute("UPDATE discussion_sessions SET updated_at=? WHERE id=?", (now, session["id"]))
        return _state(db, subject, _session(db, subject_type, subject_id, user_id))


@router.post("/{subject_type}/{subject_id}/messages/{message_id}/retry", status_code=202)
def retry_message(subject_type: SubjectType, subject_id: str, message_id: str):
    user_id = local_user_id()
    with connect() as db:
        from .desktop_updates import require_task_start_allowed

        require_task_start_allowed(db)
        _expire_claims(db, utc_now())
        subject, _ = load_subject(db, subject_type, subject_id, user_id)
        session = _session(db, subject_type, subject_id, user_id)
        message = db.execute(
            "SELECT * FROM discussion_messages WHERE id=? AND session_id=? AND role='assistant'",
            (message_id, session["id"] if session else ""),
        ).fetchone()
        if not message:
            raise HTTPException(404, "找不到此追問回覆。")
        if message["status"] in {"queued", "running"}:
            return _state(db, subject, session)
        if message["status"] != "failed" or not message["error_retryable"]:
            raise HTTPException(409, "此回覆目前不能重試。")
        if db.execute(
            "SELECT 1 FROM discussion_messages WHERE session_id=? AND status IN ('queued','running')",
            (session["id"],),
        ).fetchone():
            raise HTTPException(409, "此分析的追問仍在處理，請等待完成。")
        now = utc_now()
        db.execute(
            """UPDATE discussion_messages SET status='queued',content='',completed_at=NULL,
               error_code=NULL,error_message=NULL,error_retryable=NULL,claim_token=NULL,
               lease_until=NULL,provider=NULL,model=NULL,live_market_json=NULL WHERE id=?""", (message_id,),
        )
        db.execute("UPDATE discussion_sessions SET updated_at=? WHERE id=?", (now, session["id"]))
        return _state(db, subject, _session(db, subject_type, subject_id, user_id))


def _expire_claims(db, now: str) -> None:
    expired = db.execute(
        """SELECT DISTINCT session_id FROM discussion_messages WHERE status='running'
           AND (lease_until IS NULL OR lease_until<=?)""",
        (now,),
    ).fetchall()
    db.execute(
        """UPDATE discussion_messages SET status='failed',completed_at=?,claim_token=NULL,
           lease_until=NULL,error_code=?,error_retryable=1,
           error_message=CASE WHEN output_locale='en-US' THEN ? ELSE ? END
           WHERE status='running' AND (lease_until IS NULL OR lease_until<=?)""",
        (now, INTERRUPTED_ERROR[0], localized_task_error(INTERRUPTED_ERROR[0], "en-US", ""),
         INTERRUPTED_ERROR[1], now),
    )
    for session in expired:
        db.execute("UPDATE discussion_sessions SET updated_at=? WHERE id=?",
                   (now, session["session_id"]))


def claim_next(*, timeout: float) -> dict | None:
    """Claim at most one job atomically, without initializing a model transport."""
    with connect() as db:
        from .desktop_updates import register_desktop_execution, update_is_draining

        if update_is_draining(db):
            return None
        now = utc_now()
        _expire_claims(db, now)
        row = db.execute(
            "SELECT * FROM discussion_messages WHERE status='queued' ORDER BY created_at,id LIMIT 1",
        ).fetchone()
        if not row:
            return None
        token = new_id("claim")
        lease_until = (datetime.now(UTC) + timedelta(seconds=timeout + 60)).isoformat()
        db.execute(
            """UPDATE discussion_messages SET status='running',claim_token=?,lease_until=?,
               attempts=attempts+1 WHERE id=? AND status='queued'""",
            (token, lease_until, row["id"]),
        )
        db.execute("UPDATE discussion_sessions SET updated_at=? WHERE id=?", (now, row["session_id"]))
        return dict(row, status="running", claim_token=token,
                    update_execution_id=register_desktop_execution(db, "discussions", row["id"]))


def _model_input(job: dict) -> tuple[dict, list[dict]]:
    with connect(readonly=True) as db:
        session = db.execute("SELECT * FROM discussion_sessions WHERE id=?",
                             (job["session_id"],)).fetchone()
        if not session:
            raise ValueError("Discussion subject was deleted")
        # Keep complete recent pairs. Previous failed or unfinished turns never
        # masquerade as model answers; the current user's turn is always present.
        turns = db.execute(
            """SELECT u.id,u.sequence,LENGTH(u.content)+
                      CASE WHEN a.status='completed' THEN LENGTH(a.content) ELSE 0 END AS chars
               FROM discussion_messages u
               JOIN discussion_messages a ON a.reply_to_id=u.id
               WHERE u.session_id=? AND a.sequence<=?
               AND (a.status='completed' OR a.id=?)
               ORDER BY u.sequence DESC LIMIT ?""",
            (session["id"], job["sequence"], job["id"], HISTORY_TURNS + 1),
        ).fetchall()
        ids, total_chars = [], 0
        for turn in turns[:HISTORY_TURNS]:
            if ids and total_chars + turn["chars"] > HISTORY_MAX_CHARS:
                break
            ids.append(turn["id"])
            total_chars += turn["chars"]
        truncated = len(ids) < len(turns)
        placeholders = ",".join("?" for _ in ids)
        messages = db.execute(
            f"""SELECT role,content,sequence,live_market_json FROM discussion_messages WHERE session_id=?
                AND (id IN ({placeholders}) OR
                    (reply_to_id IN ({placeholders}) AND status='completed'))
                ORDER BY sequence""", (session["id"], *ids, *ids),
        ).fetchall() if ids else []
        messages = [
            {"role": row["role"], "content": row["content"], "sequence": row["sequence"]}
            | ({"live_market": json.loads(row["live_market_json"])}
               if row["live_market_json"] is not None else {})
            for row in messages
        ]
        context = json.loads(session["context_json"])
        context["output_locale"] = session.get("output_locale") or "zh-TW"
        english = context["output_locale"] == "en-US"
        context["discussion_history_window"] = {
            "max_turns": HISTORY_TURNS, "max_chars": HISTORY_MAX_CHARS,
            "earlier_history_omitted": truncated,
            "notice": (("Earlier discussion was omitted from this model context; the complete record remains "
                        "in this analysis discussion.") if truncated else
                       "All successful discussion turns up to this question are included.") if english else (
                "更早的對話未帶入本次模型上下文，完整記錄仍保存在此分析的對話中。"
                if truncated else "本次已帶入這份分析截至追問時的完整成功對話。"),
        }
        if truncated:
            context["history_notice"] = context["discussion_history_window"]["notice"]
        return context, messages


def generate_reply(context: dict, messages: list[dict], *, timeout: float,
                   instructions: str | None = None,
                   on_text: Callable[[str], None] | None = None) -> tuple[str, str, str]:
    deadline = monotonic() + timeout
    instructions = instructions or resolve_prompt(
        "discussion", response_locale=context.get("output_locale") or "zh-TW",
    ).instructions
    session = ModelSession(openai_factory=OpenAI)
    try:
        supplied = build_discussion_input(context, messages)
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("Discussion model setup exceeded its budget")
        response = session.stream_text(
            instructions=instructions, context=supplied, timeout=remaining,
            on_text=on_text,
        )
        text = getattr(response, "output_text", None)
        if getattr(response, "status", "completed") not in {None, "completed"}:
            raise ValueError("Incomplete model response")
        if any(getattr(item, "type", None) in {"function_call", "tool_call", "web_search_call"}
               for item in getattr(response, "output", []) or []):
            raise ValueError("Unexpected model tool request")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Empty model response")
        return text.strip(), session.provider, session.model
    finally:
        session.close()


def _safe_failure(exc: Exception, output_locale: str = "zh-TW") -> tuple[str, str]:
    if isinstance(exc, (TimeoutError, APITimeoutError)):
        code = "DISCUSSION_TIMEOUT"
    elif isinstance(exc, AuthenticationError):
        code = "DISCUSSION_AUTH_REQUIRED"
    elif isinstance(exc, RateLimitError):
        code = "DISCUSSION_RATE_LIMITED"
    elif isinstance(exc, APIConnectionError):
        code = "DISCUSSION_CONNECTION_FAILED"
    else:
        code = "DISCUSSION_FAILED"
    return code, localized_task_error(code, output_locale, "")


def _persist_prefix(job: dict, content: str) -> bool:
    with connect() as db:
        now = utc_now()
        result = db.execute(
            """UPDATE discussion_messages SET content=? WHERE id=? AND status='running'
               AND claim_token=? AND lease_until>=?""",
            (content, job["id"], job["claim_token"], now),
        )
        if result.rowcount:
            db.execute("UPDATE discussion_sessions SET updated_at=? WHERE id=?",
                       (now, job["session_id"]))
        return bool(result.rowcount)


def _persist_live_market(job: dict, market: dict | None) -> bool:
    """Save this attempt's evidence only while its claim is still active."""
    with connect() as db:
        now = utc_now()
        result = db.execute(
            """UPDATE discussion_messages SET live_market_json=?
               WHERE id=? AND status='running' AND claim_token=? AND lease_until>=?""",
            (json.dumps(market, ensure_ascii=False, allow_nan=False) if market is not None else None,
             job["id"], job["claim_token"], now),
        )
        if result.rowcount:
            db.execute("UPDATE discussion_sessions SET updated_at=? WHERE id=?",
                       (now, job["session_id"]))
        return bool(result.rowcount)


def _discussion_prompt_version(artifact) -> int:
    version = re.fullmatch(r"professional_discussion_v(\d+)", artifact.prompt_version) if artifact else None
    return int(version.group(1)) if version else 0


def _supports_live_market(artifact) -> bool:
    return _discussion_prompt_version(artifact) >= 4


def _supports_live_snapshot(artifact) -> bool:
    return _discussion_prompt_version(artifact) >= LIVE_SNAPSHOT_PROMPT_VERSION


def _live_market_unavailable(context: dict, requested_at: str, snapshot: bool = False) -> dict:
    subject = context.get("subject") or {}
    extra = {"version": SNAPSHOT_VERSION, "snapshot": unavailable_snapshot("LIVE_MARKET_UNAVAILABLE")} if snapshot else {}
    return extra | {
        "version": SNAPSHOT_VERSION if snapshot else VERSION, "status": "unavailable",
        "market_id": subject.get("market_id"), "timeframe": subject.get("timeframe"),
        "source": SOURCE, "requested_at": requested_at,
        "observed_at": None, "quote": None, "forming_candle": None,
        "recent_closed_candles": [], "comparison": None,
        "errors": {"market": "LIVE_MARKET_UNAVAILABLE"},
        "not_refreshed": list(NOT_REFRESHED),
    }


def _claim_is_current(job: dict) -> bool:
    with connect(readonly=True) as db:
        return db.execute("""SELECT 1 FROM discussion_messages WHERE id=?
            AND status='running' AND claim_token=? AND lease_until>=?""",
                          (job["id"], job["claim_token"], utc_now())).fetchone() is not None


class _ReplyPrefix:
    """Persist full text snapshots, including a small last chunk during a pause."""

    def __init__(self, job: dict):
        self.job = job
        self.latest = ""
        self._saved = ""
        self._written_at = 0.0
        self._lock = Lock()
        self._timer: Timer | None = None
        self._closed = False
        self._active = True

    def _write_locked(self) -> None:
        if self._closed or not self._active or self.latest == self._saved:
            return
        self._active = _persist_prefix(self.job, self.latest)
        if self._active:
            self._saved, self._written_at = self.latest, monotonic()

    def _flush_timer(self) -> None:
        with self._lock:
            self._timer = None
            try:
                self._write_locked()
            except Exception:  # noqa: BLE001 -- final worker commit handles storage failures safely
                self._active = False

    def __call__(self, text: str) -> None:
        if not isinstance(text, str):
            raise TypeError("Discussion stream text must be a string")
        with self._lock:
            if self._closed or not text.strip():
                return
            self.latest = text
            if not self._active or self.latest == self._saved:
                return
            elapsed = monotonic() - self._written_at
            if not self._saved or abs(len(self.latest) - len(self._saved)) >= PREFIX_FLUSH_CHARS or elapsed >= PREFIX_FLUSH_SECONDS:
                if self._timer:
                    self._timer.cancel()
                    self._timer = None
                self._write_locked()
            elif self._timer is None:
                self._timer = Timer(PREFIX_FLUSH_SECONDS - elapsed, self._flush_timer)
                self._timer.daemon = True
                self._timer.start()

    def close(self) -> str:
        with self._lock:
            self._closed = True
            if self._timer:
                self._timer.cancel()
                self._timer = None
            return self.latest


def _finish(job: dict, *, content: str | None = None, provider: str | None = None,
            model: str | None = None, error: tuple[str, str] | None = None) -> bool:
    with connect() as db:
        now = utc_now()
        result = db.execute(
            """UPDATE discussion_messages SET status=?,content=COALESCE(?,content),provider=?,model=?,
               completed_at=?,error_code=?,error_message=?,error_retryable=?,
               claim_token=NULL,lease_until=NULL WHERE id=? AND status='running'
               AND claim_token=? AND lease_until>=?""",
            ("failed" if error else "completed", content, provider, model, now,
             error[0] if error else None, error[1] if error else None,
             1 if error else None, job["id"], job["claim_token"], now),
        )
        if result.rowcount:
            db.execute("UPDATE discussion_sessions SET updated_at=? WHERE id=?",
                       (now, job["session_id"]))
        return bool(result.rowcount)


def run_once() -> bool:
    timeout = analysis_timeout_seconds()
    deadline = monotonic() + timeout
    job = claim_next(timeout=timeout)
    if not job:
        return False
    prefix = _ReplyPrefix(job)
    try:
        context, messages = _model_input(job)
        with connect(readonly=True) as db:
            artifact = load_prompt_artifact(db, job["prompt_artifact_id"]) if job.get("prompt_artifact_id") else None
        # Network activity starts only for a new claimed analysis turn. Older
        # queued prompts retain their original rules and do not receive new data.
        if context.get("subject", {}).get("type") == "analysis" and _supports_live_market(artifact):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Discussion setup exceeded its budget")
            requested_at = utc_now()
            snapshot = _supports_live_snapshot(artifact)
            budget = LIVE_SNAPSHOT_MAX_SECONDS if snapshot else LIVE_MARKET_MAX_SECONDS
            try:
                market = fetch_discussion_market(
                    context, timeout=min(budget, remaining), **({"snapshot": True} if snapshot else {}))
            except Exception:  # noqa: BLE001 -- never publish upstream payloads
                market = _live_market_unavailable(context, requested_at, snapshot)
            if not _persist_live_market(job, market):
                return True
            if market is not None:
                context["live_market"] = market
        if not _claim_is_current(job):
            return True
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError("Discussion setup exceeded its budget")
        content, provider, model = generate_reply(
            context, messages, timeout=remaining,
            instructions=artifact.instructions if artifact else DISCUSSION_INSTRUCTIONS,
            on_text=prefix,
        )
        prefix.close()
        _finish(job, content=content, provider=provider, model=model)
    except Exception as exc:  # noqa: BLE001 -- persist only fixed safe messages
        partial = prefix.close()
        _finish(job, content=partial or None,
                error=_safe_failure(exc, job.get("output_locale") or "zh-TW"))
    finally:
        prefix.close()
        from .desktop_updates import finish_desktop_execution

        finish_desktop_execution(job.get("update_execution_id"))
    return True
