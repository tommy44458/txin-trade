"""Optional background link from this computer to the txinTrade cloud relay.

When the user enables remote access while signed in, this thread registers the
computer as a device, keeps an outbound WebSocket to the relay and runs only
allowlisted commands (see cloud_commands). No port is opened on this computer,
and the local API token is never involved. Disabled or signed out, it is idle.
"""

import base64
import contextlib
import gzip
import json
import os
import random
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
from fastapi import APIRouter
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException
from websockets.sync.client import connect

from .auth_metadata import read_metadata, write_metadata
from .cloud_account import CloudAccountError, cloud_origin, session_record
from .cloud_commands import CommandFailed, run_command
from .cloud_routes import allowed
from .credential_store import (
    CredentialStoreError,
    delete_credentials,
    load_credentials,
    save_credentials,
)

_DEVICE = "txintrade_device"
_SETTING = "txintrade_remote"
PING_SECONDS = 30
# The relay answers every ping. Hearing nothing for this long means the link went away
# unnoticed (sleep, a network change): reconnect instead of waiting on a dead socket.
LINK_SILENCE_SECONDS = 75
MAX_BACKOFF_SECONDS = 60
WAIT_SECONDS = {"subscription_required": 300, "device_limit": 300, "device_already_connected": 30}
MAX_FRAME_BYTES = 262_144
# Remote screens issue several requests at once; answer them in parallel.
REQUEST_WORKERS = 4
REQUEST_TIMEOUT_SECONDS = 20
CHUNK_CHARS = 196_000
MAX_CHUNKS = 64
# Follow-up replies stream through the relay; repaint at most this often.
MAX_STREAMS = 2
STREAM_INTERVAL_SECONDS = 0.15
STREAM_LIFETIME_SECONDS = 600
MAX_STREAM_EVENT_CHARS = 200_000
router = APIRouter(prefix="/api/v1/cloud-account/remote", tags=["txinTrade remote access"])


class _Wait(Exception):
    """Pause with a visible state before retrying."""

    def __init__(self, state: str, seconds: float, error: str | None = None):
        super().__init__(state)
        self.state, self.seconds, self.error = state, seconds, error


def remote_enabled() -> bool:
    return read_metadata(_SETTING).get("enabled") is True


def _device_label() -> str:
    host = socket.gethostname().removesuffix(".local").strip() or "computer"
    return f"txinTrade · {host}"[:80]


def _relay_url(origin: str, device_id: str) -> str:
    scheme = "wss" if origin.startswith("https://") else "ws"
    return f"{scheme}://{origin.split('://', 1)[1]}/api/v1/devices/{device_id}/connect"


class Connector:
    def __init__(self):
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket = None
        self._send_lock = threading.Lock()
        self._streams: dict[str, threading.Event] = {}
        self._requests = ThreadPoolExecutor(REQUEST_WORKERS, thread_name_prefix="cloud-request")
        self._state = {"state": "disabled", "error": None, "device_id": None, "connected_since": None}

    # ---- lifecycle -------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="cloud-connector", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.wake()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=10)

    def wake(self) -> None:
        """Re-evaluate now: close any open link so settings changes apply at once."""
        self._wake.set()
        with self._lock:
            ws = self._socket
        if ws is not None:
            # Closing an already-broken link is fine.
            with contextlib.suppress(Exception):
                ws.close()

    def status(self) -> dict:
        with self._lock:
            return {"enabled": remote_enabled(), **self._state}

    def _set(self, state: str, error: str | None = None, **values) -> None:
        with self._lock:
            self._state = {"state": state, "error": error, "device_id": values.get("device_id"),
                           "connected_since": values.get("connected_since")}

    def _pause(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    # ---- main loop -------------------------------------------------------
    def _run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                if not remote_enabled():
                    self._set("disabled")
                    self._pause(3600)
                    continue
                session = session_record()
                if not session:
                    raise _Wait("signed_out", 3600)
                device = self._device(session)
                if not self._link(session, device):
                    raise ConnectionError("relay closed before it was ready")
                # A normal end (relay lifetime or settings change): reconnect promptly.
                failures = 0
                self._pause(1)
            except _Wait as wait:
                self._set(wait.state, wait.error)
                self._pause(wait.seconds)
            except (CloudAccountError, CredentialStoreError) as exc:
                self._set("error", str(exc) if isinstance(exc, CloudAccountError) else "storage_failed")
                self._pause(MAX_BACKOFF_SECONDS)
            except Exception:  # noqa: BLE001 - network faults retry with backoff
                failures += 1
                self._set("reconnecting", "service_unavailable")
                self._pause(min(MAX_BACKOFF_SECONDS, 2 ** min(failures, 6)) * random.uniform(0.5, 1.0))

    def _device(self, session: dict) -> dict:
        origin = session.get("origin") or cloud_origin()
        record = load_credentials(_DEVICE, allow_interaction=True)
        if record and record.get("origin") == origin and record.get("expires_at", 0) > time.time() * 1000 + 3_600_000:
            return record
        self._set("registering")
        response = httpx.post(f"{origin}/api/v1/devices", json={"label": _device_label()},
                              headers={"Authorization": f"Bearer {session['session_token']}"}, timeout=15)
        if response.status_code in {401, 403, 409}:
            code = (response.json().get("error") or {}).get("code") if response.headers.get(
                "content-type", "").startswith("application/json") else None
            if response.status_code == 401:
                raise _Wait("signed_out", 3600, "session_expired")
            raise _Wait(code if code in WAIT_SECONDS else "error", WAIT_SECONDS.get(code, 300), code)
        response.raise_for_status()
        body = response.json()
        record = {"device_id": body["device"]["id"], "device_token": body["device_token"],
                  "expires_at": body["device"]["token_expires_at"], "origin": origin}
        save_credentials(_DEVICE, record, allow_interaction=True)
        return record

    def _link(self, session: dict, device: dict) -> bool:
        """Hold one relay connection; return True when it was established."""
        with self._lock:
            # The relay ends links every 15 minutes; a prompt reconnect stays "connected" on screen.
            quiet = self._state["state"] == "connected" and self._state["device_id"] == device["device_id"]
        if not quiet:
            self._set("connecting", device_id=device["device_id"])
        try:
            with connect(_relay_url(device["origin"], device["device_id"]),
                         additional_headers={"Authorization": f"Bearer {device['device_token']}"},
                         open_timeout=15, close_timeout=5, ping_interval=None,
                         max_size=MAX_FRAME_BYTES) as ws:
                return self._hold(ws, device)
        except InvalidStatus as exc:
            status = exc.response.status_code
            if status == 401:
                # The device token was revoked or expired: register again.
                delete_credentials(_DEVICE)
                raise _Wait("registering", 1) from None
            if status == 403:
                raise _Wait("subscription_required", WAIT_SECONDS["subscription_required"],
                            "subscription_required") from None
            if status == 409:
                raise _Wait("device_already_connected", WAIT_SECONDS["device_already_connected"]) from None
            raise

    def _hold(self, ws, device: dict) -> bool:
        with self._lock:
            self._socket = ws
        established = False
        try:
            last_ping = last_heard = time.monotonic()
            while not self._stop.is_set() and remote_enabled():
                if time.monotonic() - last_heard > LINK_SILENCE_SECONDS:
                    break
                if time.monotonic() - last_ping >= PING_SECONDS:
                    self._send(ws, "ping")
                    last_ping = time.monotonic()
                try:
                    message = ws.recv(timeout=1)
                except TimeoutError:
                    continue
                last_heard = time.monotonic()
                if message == "pong" or not isinstance(message, str):
                    continue
                event = json.loads(message)
                if event.get("type") == "relay.ready":
                    established = True
                    self._set("connected", device_id=device["device_id"], connected_since=int(time.time() * 1000))
                elif event.get("type") == "job.dispatch":
                    # A slow command must not stop this loop from reading and pinging.
                    self._requests.submit(self._handle, ws, event)
                elif event.get("type") == "request.dispatch" and isinstance(event.get("request"), dict):
                    self._requests.submit(self._answer, ws, event["request"])
                elif event.get("type") == "request.cancel":
                    with self._lock:
                        stop = self._streams.get(str(event.get("request_id")))
                    if stop:
                        stop.set()
        except (ConnectionClosed, WebSocketException, OSError, ValueError):
            pass  # The relay closes links at least every 15 minutes; reconnect.
        finally:
            with self._lock:
                self._socket = None
        return established

    def _send(self, ws, message: str) -> None:
        # Request workers and the link loop share one socket.
        with self._send_lock:
            ws.send(message)

    def _handle(self, ws, event: dict) -> None:
        job = event.get("job") or {}
        job_id = job.get("id")
        if not isinstance(job_id, str):
            return
        self._send(ws, json.dumps({"v": 1, "type": "job.accepted", "job_id": job_id}))
        try:
            result = run_command(job.get("command") or {}, str(event.get("local_idempotency_key", "")))
            self._send(ws, json.dumps({"v": 1, "type": "job.completed", "job_id": job_id, "result": result},
                                      ensure_ascii=False))
        except CommandFailed as failed:
            self._send(ws, json.dumps({"v": 1, "type": "job.failed", "job_id": job_id, "code": failed.code}))

    def _answer(self, ws, request: dict) -> None:
        """Run one relayed request against this computer's own API and send the answer back."""
        request_id = request.get("id")
        if not isinstance(request_id, str):
            return
        if request.get("stream") is True:
            self._stream(ws, request)
            return
        try:
            try:
                chunks, status = self._local_response(request)
            except CommandFailed as failed:
                self._send(ws, json.dumps({"v": 1, "type": "request.failed", "request_id": request_id,
                                           "code": failed.code}))
                return
            for index, data in enumerate(chunks):
                self._send(ws, json.dumps({"v": 1, "type": "request.chunk", "request_id": request_id,
                                           "index": index, "data": data}))
            self._send(ws, json.dumps({"v": 1, "type": "request.completed", "request_id": request_id,
                                       "status": status, "chunks": len(chunks)}))
        except (ConnectionClosed, WebSocketException, OSError):
            pass  # The link ended; the cloud already answered the browser.

    def _stream(self, ws, request: dict) -> None:
        """Forward a discussion's local event stream to the browser that asked for it."""
        request_id = request["id"]
        stop = threading.Event()
        with self._lock:
            busy = len(self._streams) >= MAX_STREAMS
            if not busy:
                self._streams[request_id] = stop
        try:
            if busy or not allowed(request):
                return
            deadline = time.monotonic() + STREAM_LIFETIME_SECONDS
            pending: str | None = None
            sent_at = 0.0

            def flush() -> None:
                nonlocal pending, sent_at
                if pending is not None:
                    self._send(ws, json.dumps({"v": 1, "type": "stream.event", "request_id": request_id,
                                               "data": pending}, ensure_ascii=False))
                    pending, sent_at = None, time.monotonic()

            with httpx.stream("GET", self._local_url(request), headers=self._local_headers(request),
                              timeout=httpx.Timeout(10, read=STREAM_LIFETIME_SECONDS)) as response:
                if response.status_code != 200:
                    return
                name = "message"
                for line in response.iter_lines():
                    if stop.is_set() or self._stop.is_set() or time.monotonic() > deadline:
                        return
                    if line.startswith("event:"):
                        name = line[6:].strip()
                    elif line.startswith("data:") and name in {"state", "error"}:
                        data = json.dumps({"event": name, "data": line[5:].strip()}, ensure_ascii=False)
                        if len(data) > MAX_STREAM_EVENT_CHARS:
                            return  # Too large to relay; the page falls back to reading.
                        pending = data
                        # The newest state replaces any not yet sent; send at a steady pace.
                        if time.monotonic() - sent_at >= STREAM_INTERVAL_SECONDS or name == "error":
                            flush()
                    elif not line:
                        name = "message"
                flush()
        except (httpx.HTTPError, ConnectionClosed, WebSocketException, OSError):
            pass
        finally:
            with self._lock:
                self._streams.pop(request_id, None)
            with contextlib.suppress(ConnectionClosed, WebSocketException, OSError):
                self._send(ws, json.dumps({"v": 1, "type": "stream.end", "request_id": request_id}))

    @staticmethod
    def _local_url(request: dict) -> str:
        port = os.getenv("APP_API_PORT", "8000")
        query = request.get("query") or ""
        return f"http://127.0.0.1:{port}{request['path']}" + (f"?{query}" if query else "")

    @staticmethod
    def _local_headers(request: dict) -> dict:
        headers = {}
        token = os.getenv("APP_DESKTOP_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if request.get("idempotency_key"):
            headers["Idempotency-Key"] = request["idempotency_key"]
        return headers

    @classmethod
    def _local_response(cls, request: dict) -> tuple[list[str], int]:
        if not allowed(request):
            raise CommandFailed("unsupported_operation")
        headers, url = cls._local_headers(request), cls._local_url(request)
        try:
            # Through the real local server, so middleware, validation and background work match the app.
            response = httpx.request(request["method"], url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS,
                                     **({"json": request["body"]} if "body" in request else {}))
        except httpx.HTTPError:
            raise CommandFailed("local_unavailable") from None
        if not response.content:
            return [], response.status_code
        encoded = base64.b64encode(gzip.compress(response.content, compresslevel=6)).decode()
        chunks = [encoded[start:start + CHUNK_CHARS] for start in range(0, len(encoded), CHUNK_CHARS)]
        if len(chunks) > MAX_CHUNKS:
            raise CommandFailed("local_error")
        return chunks, response.status_code


connector = Connector()


def set_remote_enabled(enabled: bool) -> dict:
    write_metadata(_SETTING, {"enabled": enabled})
    # Report the new intent at once instead of the state from before the switch.
    connector._set("connecting" if enabled else "disabled")
    connector.start()
    connector.wake()
    return connector.status()


def forget_device(session: dict | None) -> None:
    """On sign-out: stop remote access, revoke this device in the cloud and remove its token."""
    write_metadata(_SETTING, {"enabled": False})
    connector.wake()
    try:
        record = load_credentials(_DEVICE, allow_interaction=True)
    except CredentialStoreError:
        record = None
    if record and session:
        try:
            httpx.delete(f"{record['origin']}/api/v1/devices/{record['device_id']}",
                         headers={"Authorization": f"Bearer {session['session_token']}"}, timeout=10)
        except httpx.HTTPError:
            pass
    delete_credentials(_DEVICE)


@router.get("")
def read_remote() -> dict:
    return connector.status()


@router.post("/enable")
def enable_remote() -> dict:
    return set_remote_enabled(True)


@router.post("/disable")
def disable_remote() -> dict:
    return set_remote_enabled(False)
