import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Event, Timer
from time import monotonic, sleep
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from trade_helper import discussions
from trade_helper.api import app
from trade_helper.db import connect

from .test_discussions import analysis, macro, send


@pytest.fixture
def client():
    with TestClient(app) as value:
        yield value


class RequestConnection:
    def __init__(self):
        self.disconnected = False

    async def is_disconnected(self):
        return self.disconnected


def event_state(event):
    assert event.startswith("event: state\ndata: ")
    return json.loads(event.removeprefix("event: state\ndata: ").strip())


def assistant(client):
    return client.get("/api/v1/discussions/analysis/analysis-one").json()["messages"][-1]


def test_first_chunk_is_saved_before_completion_and_disconnect_does_not_cancel_worker(
        client, monkeypatch):
    analysis()
    send(client)
    emitted, finish = Event(), Event()
    closed = []

    def stream_text(**kwargs):
        # The expensive model call never owns a SQLite transaction.
        with connect() as db:
            db.execute("SELECT count(*) AS n FROM discussion_messages")
        kwargs["on_text"]("First visible sentence.")
        emitted.set()
        if not finish.wait(30):
            raise TimeoutError("test model was never released")
        kwargs["on_text"]("First visible sentence. Final answer.")
        return SimpleNamespace(output_text="First visible sentence. Final answer.",
                               output=[], status="completed")

    monkeypatch.setattr(discussions, "ModelSession", lambda **_: SimpleNamespace(
        provider="codex", model="fixture", stream_text=stream_text,
        close=lambda: closed.append(True)))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(discussions.run_once)
        try:
            # Upper bounds only: a slow Windows runner may take seconds to reach the model.
            assert emitted.wait(15)
            partial = assistant(client)
            assert partial["content"] == "First visible sentence."
            assert partial["status"] == "running" and not pending.done()

            async def reconnect():
                initial = discussions._stream_state("analysis", "analysis-one", "local-demo")
                request = RequestConnection()
                events = discussions._discussion_events(
                    request, "analysis", "analysis-one", "local-demo", initial)
                assert event_state(await anext(events))["messages"][-1]["content"] == partial["content"]
                request.disconnected = True
                with pytest.raises(StopAsyncIteration):
                    await anext(events)
                assert not pending.done()
                # A new connection resumes the same full prefix, without a
                # second model request or joining repeated deltas.
                reconnected = discussions._discussion_events(
                    RequestConnection(), "analysis", "analysis-one", "local-demo",
                    discussions._stream_state("analysis", "analysis-one", "local-demo"))
                state = event_state(await anext(reconnected))
                assert state["messages"][-1]["content"] == partial["content"]
                finish.set()
                assert pending.result(timeout=15)
                state = event_state(await anext(reconnected))
                assert not state["busy"]
                assert state["messages"][-1]["status"] == "completed"
                assert state["messages"][-1]["content"] == "First visible sentence. Final answer."
                with pytest.raises(StopAsyncIteration):
                    await anext(reconnected)

            asyncio.run(reconnect())
        finally:
            finish.set()
    assert closed == [True]


@pytest.mark.parametrize("subject_type", ["analysis", "macro"])
def test_sse_endpoint_is_read_only_owned_and_uses_normal_readiness_errors(
        client, monkeypatch, subject_type):
    factory = analysis if subject_type == "analysis" else macro
    factory("owned")
    factory("foreign", owner="another-user")
    factory("pending", status="running")
    monkeypatch.setattr(discussions, "ModelSession", lambda **_: pytest.fail("SSE cannot call a model"))
    path = f"/api/v1/discussions/{subject_type}"
    with connect(readonly=True) as db:
        before = db.execute("SELECT count(*) AS n FROM discussion_messages").fetchone()["n"]
    for identifier, code in (("foreign", 404), ("missing", 404), ("pending", 409)):
        assert client.get(f"{path}/{identifier}/stream").status_code == code
    response = client.get(f"{path}/owned/stream")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert event_state(response.text) == client.get(f"{path}/owned").json()
    with connect(readonly=True) as db:
        assert db.execute("SELECT count(*) AS n FROM discussion_messages").fetchone()["n"] == before


def test_sse_endpoint_emits_full_prefix_then_terminal_state(client, monkeypatch):
    analysis()
    send(client)
    job = discussions.claim_next(timeout=240)
    assert discussions._persist_prefix(job, "A live prefix")
    monkeypatch.setattr(discussions, "ModelSession", lambda **_: pytest.fail("SSE cannot rerun a model"))
    complete = Timer(0.05, lambda: discussions._finish(job, content="A live prefix and the end."))
    complete.start()
    try:
        response = client.get("/api/v1/discussions/analysis/analysis-one/stream")
    finally:
        complete.join(timeout=2)
    states = [event_state(item + "\n\n") for item in response.text.strip().split("\n\n")]
    assert len(states) == 2
    assert states[0]["busy"] and states[0]["messages"][-1]["content"] == "A live prefix"
    assert not states[-1]["busy"]
    assert states[-1]["messages"][-1]["content"] == "A live prefix and the end."


def test_stream_yields_without_holding_transaction_and_sends_heartbeat(client, monkeypatch):
    analysis()
    send(client)
    monkeypatch.setattr(discussions, "STREAM_HEARTBEAT_SECONDS", 0)

    async def exercise():
        request = RequestConnection()
        events = discussions._discussion_events(
            request, "analysis", "analysis-one", "local-demo",
            discussions._stream_state("analysis", "analysis-one", "local-demo"))
        assert event_state(await anext(events))["busy"]
        # Another writer commits while the generator is suspended at yield.
        with connect() as db:
            db.execute("UPDATE discussion_sessions SET updated_at=updated_at")
        assert await anext(events) == ": heartbeat\n\n"
        request.disconnected = True
        with pytest.raises(StopAsyncIteration):
            await anext(events)

    asyncio.run(exercise())


def test_small_pending_chunk_flushes_during_model_pause_and_snapshots_replace(client):
    analysis()
    send(client)
    job = discussions.claim_next(timeout=240)
    prefix = discussions._ReplyPrefix(job)
    try:
        prefix("Hello")
        assert assistant(client)["content"] == "Hello"
        prefix("Hello there")
        prefix("Hello there")
        # The pause flush runs on a timer; a busy CI runner may fire it late.
        deadline = monotonic() + 3
        while assistant(client)["content"] != "Hello there" and monotonic() < deadline:
            sleep(0.05)
        assert assistant(client)["content"] == "Hello there"
        prefix("Hello there" + "x" * 128)
        assert assistant(client)["content"] == "Hello there" + "x" * 128
        # No delta appending: a full snapshot is stored exactly once.
        assert "HelloHello" not in assistant(client)["content"]
    finally:
        prefix.close()


@pytest.mark.parametrize("exception", [TimeoutError("secret-upstream"), ValueError("secret-upstream")])
def test_failed_partial_reply_is_kept_out_of_history_and_retry_clears_old_prefix(
        client, monkeypatch, exception):
    analysis()
    first = send(client, message="First question").json()["messages"][-1]

    def fail(_context, _messages, *, on_text, **_kwargs):
        on_text("An unfinished explanation")
        raise exception

    monkeypatch.setattr(discussions, "generate_reply", fail)
    assert discussions.run_once()
    failed = assistant(client)
    assert failed["status"] == "failed" and failed["content"] == "An unfinished explanation"
    assert failed["error"]["retryable"] and "secret" not in json.dumps(failed)
    path = f"/api/v1/discussions/analysis/analysis-one/messages/{first['id']}/retry"
    retried = client.post(path).json()["messages"][-1]
    assert retried["id"] == first["id"] and retried["status"] == "queued"
    assert retried["content"] == "" and retried["error"] is None
    # An explicit retry that also fails leaves no old prefix behind.
    monkeypatch.setattr(discussions, "generate_reply", lambda *_a, **_kw:
                        (_ for _ in ()).throw(TimeoutError("private")))
    assert discussions.run_once() and assistant(client)["content"] == ""
    send(client, message="Second question")
    observed = []
    monkeypatch.setattr(discussions, "generate_reply", lambda _context, messages, **_kwargs:
                        (observed.extend(messages) or "The successful answer", "fake", "model"))
    assert discussions.run_once()
    assert [item["content"] for item in observed] == ["Second question"]


def test_expired_partial_is_preserved_read_only_and_old_claim_cannot_pollute_retry(client):
    analysis()
    send(client)
    old = discussions.claim_next(timeout=240)
    prefix = discussions._ReplyPrefix(old)
    prefix("The old incomplete answer")
    with connect() as db:
        db.execute("UPDATE discussion_messages SET lease_until=? WHERE id=?",
                   ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), old["id"]))
    response = client.get("/api/v1/discussions/analysis/analysis-one/stream")
    state = event_state(response.text)
    assert not state["busy"] and state["messages"][-1]["status"] == "failed"
    assert state["messages"][-1]["content"] == "The old incomplete answer"
    with connect(readonly=True) as db:
        assert db.execute("SELECT status FROM discussion_messages WHERE id=?",
                          (old["id"],)).fetchone()["status"] == "running"
    retried = client.post(f"/api/v1/discussions/analysis/analysis-one/messages/{old['id']}/retry").json()
    assert retried["messages"][-1]["content"] == ""
    new = discussions.claim_next(timeout=240)
    assert new["claim_token"] != old["claim_token"]
    assert discussions._persist_prefix(new, "The new attempt")
    prefix("The old incomplete answer" + "x" * 128)
    prefix.close()
    assert not discussions._persist_prefix(old, "late old data")
    assert not discussions._finish(old, content="late old result")
    assert assistant(client)["content"] == "The new attempt"
    assert discussions._finish(new, content="The new complete answer")
    assert assistant(client)["content"] == "The new complete answer"


def test_stream_closes_with_safe_error_when_subject_is_deleted(client, monkeypatch):
    analysis()
    send(client)
    monkeypatch.setattr(discussions, "STREAM_POLL_SECONDS", 0)

    async def exercise():
        events = discussions._discussion_events(
            RequestConnection(), "analysis", "analysis-one", "local-demo",
            discussions._stream_state("analysis", "analysis-one", "local-demo"))
        assert event_state(await anext(events))["busy"]
        with connect() as db:
            db.execute("DELETE FROM analyses WHERE id='analysis-one'")
        event = await anext(events)
        assert event.startswith("event: error\ndata: ")
        assert "DISCUSSION_SUBJECT_NOT_FOUND" in event
        with pytest.raises(StopAsyncIteration):
            await anext(events)

    asyncio.run(exercise())
