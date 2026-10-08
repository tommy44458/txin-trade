import base64
import gzip
import json
import secrets
import threading
import time

import httpx
import pytest
from websockets.sync.server import serve

from trade_helper import cloud_connector
from trade_helper.auth_metadata import write_metadata
from trade_helper.cloud_connector import Connector
from trade_helper.credential_store import load_credentials, save_credentials

SESSION = "st_" + "s" * 64
DEVICE_TOKEN = "dt_" + "d" * 64
DEVICE_ID = "dev_00000000-0000-4000-8000-000000000001"


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class FakeRelay:
    """A local WebSocket server standing in for the cloud UserRelay."""

    def __init__(self, jobs: list[dict], accept_token=DEVICE_TOKEN, requests: list[dict] | None = None):
        self.jobs, self.accept_token, self.requests = jobs, accept_token, requests or []
        self.received: list[dict] = []
        self.pings = 0
        self.connections = 0
        # A link that went away unnoticed: the socket stays open but nothing comes back.
        self.silent = False
        self.headers: list[dict] = []
        self.server = serve(self.handle, "127.0.0.1", 0, process_request=self.authorize)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def authorize(self, connection, request):
        self.headers.append({key.lower(): value for key, value in request.headers.raw_items()})
        if request.headers.get("Authorization") != f"Bearer {self.accept_token}" or "Origin" in request.headers:
            return connection.respond(401, "invalid_device_token")
        return None

    def handle(self, ws):
        self.connections += 1
        ws.send(json.dumps({"v": 1, "type": "relay.ready", "role": "desktop", "device_id": DEVICE_ID,
                            "expires_at": int(time.time() * 1000) + 900_000}))
        for job in self.jobs:
            ws.send(json.dumps({"v": 1, "type": "job.dispatch", "job": job,
                                "local_idempotency_key": f"cloud:{job['id']}"}))
        for request in self.requests:
            ws.send(json.dumps({"v": 1, "type": "request.dispatch", "request": request}))
        for message in ws:
            if message == "ping":
                self.pings += 1
                if not self.silent:
                    ws.send("pong")
            else:
                self.received.append(json.loads(message))

    def close(self):
        self.server.shutdown()


@pytest.fixture
def cloud(monkeypatch):
    calls = {"register": 0, "delete": 0, "register_status": 201}
    relay_holder = {}

    def session():
        return {"session_token": SESSION, "expires_at": int(time.time() * 1000) + 86_400_000,
                "origin": f"http://127.0.0.1:{relay_holder['relay'].port}"}

    def post(url, json=None, headers=None, timeout=None):
        assert url.endswith("/api/v1/devices") and headers["Authorization"] == f"Bearer {SESSION}"
        assert json["label"].startswith("txinTrade · ")
        calls["register"] += 1
        status = calls["register_status"]
        if status != 201:
            return httpx.Response(status, json={"error": {"code": "subscription_required"}},
                                  request=httpx.Request("POST", url))
        return httpx.Response(201, request=httpx.Request("POST", url), json={
            "device": {"id": DEVICE_ID, "label": json["label"],
                       "token_expires_at": int(time.time() * 1000) + 30 * 86_400_000},
            "device_token": DEVICE_TOKEN})

    def delete(url, headers=None, timeout=None):
        calls["delete"] += 1
        calls["deleted_url"] = url
        return httpx.Response(200, request=httpx.Request("DELETE", url))

    monkeypatch.setattr(cloud_connector, "session_record", session)
    monkeypatch.setattr(cloud_connector.httpx, "post", post)
    monkeypatch.setattr(cloud_connector.httpx, "delete", delete)
    monkeypatch.setattr(cloud_connector, "PING_SECONDS", 0.2)
    connector = Connector()
    yield {"calls": calls, "relay": relay_holder, "connector": connector}
    connector.stop()
    if "relay" in relay_holder:
        relay_holder["relay"].close()


def job(operation: dict, number: int) -> dict:
    return {"id": f"00000000-0000-4000-8000-{number:012d}", "device_id": DEVICE_ID, "command": operation,
            "status": "dispatched", "created_at": 0, "expires_at": 0}


def test_registers_once_connects_outbound_and_answers_allowlisted_commands(cloud):
    relay = cloud["relay"]["relay"] = FakeRelay([job({"operation": "status.read"}, 1),
                                                job({"operation": "files.read", "path": "/etc"}, 2)])
    write_metadata("txintrade_remote", {"enabled": True})
    connector = cloud["connector"]
    connector.start()
    assert wait_for(lambda: len(relay.received) >= 4 and relay.pings >= 1)
    status = connector.status()
    assert status["state"] == "connected" and status["device_id"] == DEVICE_ID and status["enabled"] is True
    events = {(event["job_id"][-1], event["type"]): event for event in relay.received}
    assert events[("1", "job.completed")]["result"]["app"] == "txinTrade"
    assert events[("2", "job.failed")]["code"] == "unsupported_operation"
    assert ("1", "job.accepted") in events and ("2", "job.accepted") in events
    assert cloud["calls"]["register"] == 1
    assert load_credentials("txintrade_device", allow_interaction=True)["device_token"] == DEVICE_TOKEN
    # Native client: bearer device token, never a browser Origin.
    assert relay.headers[0]["authorization"] == f"Bearer {DEVICE_TOKEN}"
    assert "origin" not in relay.headers[0]


def test_a_rejected_device_token_is_replaced_by_a_fresh_registration(cloud):
    save_credentials("txintrade_device", {"device_id": DEVICE_ID, "device_token": "dt_" + "0" * 64,
                                          "expires_at": int(time.time() * 1000) + 86_400_000,
                                          "origin": "placeholder"}, allow_interaction=True)
    relay = cloud["relay"]["relay"] = FakeRelay([])
    stale = load_credentials("txintrade_device", allow_interaction=True)
    stale["origin"] = f"http://127.0.0.1:{relay.port}"
    save_credentials("txintrade_device", stale, allow_interaction=True)
    write_metadata("txintrade_remote", {"enabled": True})
    cloud["connector"].start()
    assert wait_for(lambda: cloud["connector"].status()["state"] == "connected")
    assert cloud["calls"]["register"] == 1
    assert load_credentials("txintrade_device", allow_interaction=True)["device_token"] == DEVICE_TOKEN


def test_without_a_subscription_it_waits_instead_of_retrying_rapidly(cloud):
    cloud["relay"]["relay"] = FakeRelay([])
    cloud["calls"]["register_status"] = 403
    write_metadata("txintrade_remote", {"enabled": True})
    cloud["connector"].start()
    assert wait_for(lambda: cloud["connector"].status()["state"] == "subscription_required")
    time.sleep(0.5)
    assert cloud["calls"]["register"] == 1


def test_disabled_or_signed_out_stays_idle_without_network(cloud, monkeypatch):
    cloud["relay"]["relay"] = FakeRelay([])
    connector = cloud["connector"]
    connector.start()
    assert wait_for(lambda: connector.status()["state"] == "disabled")
    monkeypatch.setattr(cloud_connector, "session_record", lambda: None)
    write_metadata("txintrade_remote", {"enabled": True})
    connector.wake()
    assert wait_for(lambda: connector.status()["state"] == "signed_out")
    assert cloud["calls"]["register"] == 0


def test_signing_out_disables_remote_access_and_revokes_this_device(cloud):
    relay = cloud["relay"]["relay"] = FakeRelay([])
    write_metadata("txintrade_remote", {"enabled": True})
    connector = cloud["connector"]
    connector.start()
    assert wait_for(lambda: connector.status()["state"] == "connected")
    monkeypatch_connector = cloud_connector.connector
    cloud_connector.connector = connector
    try:
        cloud_connector.forget_device({"session_token": SESSION})
    finally:
        cloud_connector.connector = monkeypatch_connector
    assert cloud["calls"]["delete"] == 1 and cloud["calls"]["deleted_url"].endswith(f"/api/v1/devices/{DEVICE_ID}")
    assert load_credentials("txintrade_device", allow_interaction=True) is None
    assert wait_for(lambda: connector.status()["state"] == "disabled")
    assert relay.port


def test_switching_reports_the_new_intent_immediately(cloud, monkeypatch):
    relay = cloud["relay"]["relay"] = FakeRelay([])
    monkeypatch.setattr(cloud_connector, "connector", cloud["connector"])
    assert cloud_connector.set_remote_enabled(True)["state"] in {"connecting", "registering", "connected"}
    assert wait_for(lambda: cloud["connector"].status()["state"] == "connected")
    switched_off = cloud_connector.set_remote_enabled(False)
    assert (switched_off["enabled"], switched_off["state"]) == (False, "disabled")
    assert wait_for(lambda: cloud["connector"].status()["state"] == "disabled")
    assert relay.port


def test_relays_allowlisted_requests_to_the_local_api_and_refuses_the_rest(cloud, monkeypatch):
    calls = []
    # Random text, so compression still leaves several chunks.
    payload = secrets.token_hex(50_000)

    def local(method, url, headers=None, timeout=None, **kwargs):
        calls.append((method, url, headers, kwargs))
        return httpx.Response(200, json={"items": [payload]}, request=httpx.Request(method, url))

    monkeypatch.setattr(cloud_connector.httpx, "request", local)
    monkeypatch.setattr(cloud_connector, "CHUNK_CHARS", 20_000)
    monkeypatch.setenv("APP_DESKTOP_TOKEN", "local-token")
    monkeypatch.setenv("APP_API_PORT", "45678")
    ok_id, refused_id = "00000000-0000-4000-8000-0000000000a1", "00000000-0000-4000-8000-0000000000a2"
    relay = cloud["relay"]["relay"] = FakeRelay([], requests=[
        {"id": ok_id, "method": "POST", "path": "/api/v1/analyses", "query": "",
         "body": {"kind": "market"}, "idempotency_key": "remote-0001"},
        {"id": refused_id, "method": "POST", "path": "/api/v1/auth/codex/login", "query": ""},
    ])
    write_metadata("txintrade_remote", {"enabled": True})
    cloud["connector"].start()
    assert wait_for(lambda: any(event.get("type") == "request.completed" for event in relay.received)
                    and any(event.get("type") == "request.failed" for event in relay.received))
    failed = next(event for event in relay.received if event.get("type") == "request.failed")
    assert (failed["request_id"], failed["code"]) == (refused_id, "unsupported_operation")
    assert len(calls) == 1
    method, url, headers, kwargs = calls[0]
    assert (method, url) == ("POST", "http://127.0.0.1:45678/api/v1/analyses")
    assert headers == {"Authorization": "Bearer local-token", "Idempotency-Key": "remote-0001"}
    assert kwargs == {"json": {"kind": "market"}}
    done = next(event for event in relay.received if event.get("type") == "request.completed")
    chunks = sorted((event for event in relay.received if event.get("type") == "request.chunk"),
                    key=lambda event: event["index"])
    assert done["status"] == 200 and done["chunks"] == len(chunks) > 1
    body = json.loads(gzip.decompress(base64.b64decode("".join(event["data"] for event in chunks))))
    assert body["items"][0] == payload


class FakeStream:
    """A local SSE response: yields lines, optionally waiting for a cancel between them."""

    def __init__(self, lines, gate=None):
        self.lines, self.gate, self.status_code = lines, gate, 200

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def iter_lines(self):
        for line in self.lines:
            if line == "WAIT":
                self.gate.wait(5)
                continue
            yield line


def stream_request(number: int) -> dict:
    return {"id": f"00000000-0000-4000-8000-{number:012d}", "method": "GET", "query": "",
            "path": "/api/v1/discussions/analysis/ana_1/stream", "stream": True,
            "connection_id": "00000000-0000-4000-8000-00000000c0de"}


def test_streams_a_reply_to_the_relay_and_ends_it(cloud, monkeypatch):
    lines = [": heartbeat", "", "event: state", 'data: {"busy":true}', "", "event: state", 'data: {"busy":false}', ""]
    monkeypatch.setattr(cloud_connector, "STREAM_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(cloud_connector.httpx, "stream", lambda *_args, **_kwargs: FakeStream(lines))
    relay = cloud["relay"]["relay"] = FakeRelay([], requests=[stream_request(1)])
    write_metadata("txintrade_remote", {"enabled": True})
    cloud["connector"].start()
    assert wait_for(lambda: any(event.get("type") == "stream.end" for event in relay.received))
    events = [event for event in relay.received if event.get("type", "").startswith("stream.")]
    assert [json.loads(event["data"]) for event in events if event["type"] == "stream.event"] == [
        {"event": "state", "data": '{"busy":true}'}, {"event": "state", "data": '{"busy":false}'}]
    assert events[-1]["type"] == "stream.end"


def test_a_cancelled_stream_stops_forwarding(cloud, monkeypatch):
    gate = threading.Event()
    lines = ["event: state", 'data: {"busy":true}', "", "WAIT", "event: state", 'data: {"busy":true,"more":1}', ""]
    monkeypatch.setattr(cloud_connector, "STREAM_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(cloud_connector.httpx, "stream", lambda *_args, **_kwargs: FakeStream(lines, gate))
    request = stream_request(2)
    relay = cloud["relay"]["relay"] = FakeRelay([], requests=[request])
    write_metadata("txintrade_remote", {"enabled": True})
    connector = cloud["connector"]
    connector.start()
    assert wait_for(lambda: any(event.get("type") == "stream.event" for event in relay.received))
    with connector._lock:
        connector._streams[request["id"]].set()
    gate.set()
    assert wait_for(lambda: any(event.get("type") == "stream.end" for event in relay.received))
    assert sum(event.get("type") == "stream.event" for event in relay.received) == 1


def test_a_link_that_goes_silent_is_replaced_instead_of_held(cloud, monkeypatch):
    # Above the loop's one-second receive wait, far below the real 75 seconds.
    monkeypatch.setattr(cloud_connector, "LINK_SILENCE_SECONDS", 2.5)
    relay = cloud["relay"]["relay"] = FakeRelay([])
    write_metadata("txintrade_remote", {"enabled": True})
    cloud["connector"].start()
    assert wait_for(lambda: relay.pings >= 2)
    assert relay.connections == 1  # Answered pings keep the link.
    relay.silent = True  # Pings now go unanswered, as after sleep or a network change.
    assert wait_for(lambda: relay.connections >= 2, timeout=15)
