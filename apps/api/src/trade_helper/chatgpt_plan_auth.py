"""Sign in with ChatGPT, using OpenAI's open-source token sharing.

OpenAI lets open-source apps that run locally use a ChatGPT Plus or Pro plan
through an official OAuth flow (developers.openai.com/siwc): the app registers
itself on first sign-in, the user approves plan usage for it (with a weekly cap
they set in ChatGPT), and requests go to the Responses API with that access
token. txinTrade passes the token to the local Codex app-server, configured as
a Responses provider, so analysis keeps using Codex's tool loop.

Tokens are kept in the encrypted credential store; the issued client ID and the
host ID are not secret and live in the auth metadata.
"""

import base64
import hashlib
import secrets
import threading
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import jwt
from fastapi import APIRouter, HTTPException

from .auth_metadata import read_metadata, write_metadata
from .credential_store import (
    CredentialStoreError,
    delete_credentials,
    load_credentials,
    save_credentials,
)

router = APIRouter(prefix="/api/v1/auth/chatgpt_plan", tags=["ChatGPT plan authorization"])

ISSUER = "https://auth.openai.com"
AUTHORIZE_URL = f"{ISSUER}/api/accounts/authorize"
TOKEN_URL = f"{ISSUER}/api/accounts/oauth/token"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"
RESOURCE = "https://api.openai.com/v1"
REGISTRATION_CLIENT = "dynamic_agent_client"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = f"openid profile email offline_access resource.invoke {PLAN_SCOPE}"
AGENT_NAME = "txinTrade"
USAGE_SETTINGS_URL = "https://chatgpt.com/settings/usage"
LOGIN_WINDOW = timedelta(minutes=10)
_METADATA = "chatgpt_plan"
_CREDENTIALS = "chatgpt_plan"
SIGN_IN_HINT = "請在設定按「連線 ChatGPT」，以你的 ChatGPT Plus 或 Pro 帳號授權。"
EXPIRED_HINT = "ChatGPT 授權已失效，請到設定重新連線。"


class ChatGPTPlanError(RuntimeError):
    """Credential-free operational error suitable for display in the app."""


def _host_id() -> str:
    """One stable identifier per computer, as OpenAI asks for each host."""
    saved = read_metadata(_METADATA)
    host = saved.get("host_id")
    if not isinstance(host, str) or not host.startswith("urn:uuid:"):
        host = f"urn:uuid:{uuid.uuid4()}"
        write_metadata(_METADATA, {**saved, "host_id": host})
    return host


def _client_id() -> str | None:
    value = read_metadata(_METADATA).get("client_id")
    return value if isinstance(value, str) and value else None


def _pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _token_request(data: dict, *, post=httpx.post) -> dict:
    try:
        response = post(TOKEN_URL, data=data, timeout=20)
    except httpx.HTTPError as exc:
        raise ChatGPTPlanError("無法連線到 OpenAI 授權服務，請檢查網路後重試。") from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code >= 400 or not isinstance(body, dict):
        error = body.get("error") if isinstance(body, dict) else None
        if error in {"invalid_grant", "invalid_refresh_token", "token_expired", "invalid_token"}:
            raise ChatGPTPlanError(EXPIRED_HINT)
        raise ChatGPTPlanError("OpenAI 授權服務拒絕了這次請求，請重新連線 ChatGPT。")
    return body


def _validated_identity(id_token: str, client_id: str, nonce: str, *, jwks_client=None) -> dict:
    """Check the ID token's signature, issuer, audience, expiry and this attempt's nonce."""
    try:
        signing_key = (jwks_client or jwt.PyJWKClient(JWKS_URL)).get_signing_key_from_jwt(id_token)
        claims = jwt.decode(id_token, signing_key.key, algorithms=["RS256", "ES256"],
                            audience=client_id, issuer=ISSUER)
    except jwt.PyJWTError as exc:
        raise ChatGPTPlanError("無法驗證 ChatGPT 登入身分，請重新連線。") from exc
    if claims.get("nonce") != nonce:
        raise ChatGPTPlanError("無法驗證 ChatGPT 登入身分，請重新連線。")
    return claims


def _save_tokens(body: dict, *, client_id: str, claims: dict | None = None, previous: dict | None = None):
    scopes = str(body.get("scope") or "").split()
    if PLAN_SCOPE not in scopes:
        raise ChatGPTPlanError("這個 ChatGPT 帳號沒有授權方案用量；需要 ChatGPT Plus 或 Pro，並在授權頁允許。")
    access, refresh = body.get("access_token"), body.get("refresh_token") or (previous or {}).get("refresh_token")
    if not isinstance(access, str) or not isinstance(refresh, str):
        raise ChatGPTPlanError("OpenAI 沒有回傳完整的授權資料，請重新連線 ChatGPT。")
    lifetime = body.get("expires_in") if isinstance(body.get("expires_in"), int) else 3600
    saved = {
        "client_id": client_id, "ext_agent_host_id": _host_id(),
        "subject": (claims or {}).get("sub") or (previous or {}).get("subject"),
        "email": (claims or {}).get("email") or (previous or {}).get("email"),
        "access_token": access, "refresh_token": refresh,
        "expires_at": (datetime.now(UTC) + timedelta(seconds=lifetime)).isoformat(),
        "scopes": scopes,
    }
    save_credentials(_CREDENTIALS, saved)
    write_metadata(_METADATA, {**read_metadata(_METADATA), "client_id": client_id,
                               "email": saved["email"], "connected": True})
    return saved


def access_token(*, post=httpx.post) -> str:
    """A current access token, refreshed when it is about to expire."""
    try:
        saved = load_credentials(_CREDENTIALS)
    except CredentialStoreError as exc:
        raise ChatGPTPlanError(str(exc)) from exc
    if not saved or not saved.get("refresh_token"):
        raise ChatGPTPlanError(SIGN_IN_HINT)
    try:
        expires = datetime.fromisoformat(saved.get("expires_at", ""))
    except ValueError:
        expires = datetime.min.replace(tzinfo=UTC)
    if expires - datetime.now(UTC) > timedelta(minutes=5):
        return saved["access_token"]
    try:
        body = _token_request({"grant_type": "refresh_token", "refresh_token": saved["refresh_token"],
                               "client_id": saved["client_id"], "resource": RESOURCE}, post=post)
    except ChatGPTPlanError as exc:
        if str(exc) == EXPIRED_HINT:
            # Keep the issued client ID so signing in again does not register a new app.
            delete_credentials(_CREDENTIALS)
            write_metadata(_METADATA, {**read_metadata(_METADATA), "connected": False})
        raise
    return _save_tokens(body, client_id=saved["client_id"], previous=saved)["access_token"]


class _Login:
    """One pending browser sign-in, answered on a loopback callback."""

    def __init__(self):
        self.state, self.nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.verifier, challenge = _pkce()
        self.client_id = _client_id()
        self.error: str | None = None
        self.done = threading.Event()
        self.started = datetime.now(UTC)
        login = self

        class Callback(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlparse(self.path)
                if url.path != "/callback":
                    self.send_error(404)
                    return
                login.finish(parse_qs(url.query))
                body = ("<!doctype html><meta charset=utf-8><title>txinTrade</title>"
                        "<p style='font:16px -apple-system,sans-serif;margin:3em'>"
                        + ("ChatGPT 已連線，可以關閉這個視窗。ChatGPT is connected; you can close this window."
                           if login.error is None else
                           "ChatGPT 連線未完成，請回到 txinTrade 查看說明。Return to txinTrade for details.")
                        + "</p>").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        # OpenAI requires the literal 127.0.0.1 loopback host and the /callback path.
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Callback)
        self.redirect_uri = f"http://127.0.0.1:{self.server.server_address[1]}/callback"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        query = {
            "client_id": self.client_id or REGISTRATION_CLIENT, "response_type": "code",
            "redirect_uri": self.redirect_uri, "scope": SCOPES, "resource": RESOURCE,
            "state": self.state, "nonce": self.nonce,
            "code_challenge_method": "S256", "code_challenge": challenge,
        }
        if self.client_id is None:
            query |= {"agent_name_hint": AGENT_NAME, "ext_agent_host_id": _host_id()}
        self.auth_url = f"{AUTHORIZE_URL}?{urlencode(query)}"

    def finish(self, params: dict):
        if self.done.is_set():
            return
        value = {key: items[0] for key, items in params.items() if items}
        try:
            if value.get("state") != self.state:
                raise ChatGPTPlanError("ChatGPT 授權回應不符，請重新連線。")
            if value.get("error"):
                raise ChatGPTPlanError("ChatGPT 授權已取消或被拒絕。")
            client_id = self.client_id or value.get("client_id")
            if not client_id or not value.get("code"):
                raise ChatGPTPlanError("OpenAI 沒有回傳完整的授權資料，請重新連線 ChatGPT。")
            body = _token_request({"grant_type": "authorization_code", "code": value["code"],
                                   "client_id": client_id, "code_verifier": self.verifier,
                                   "redirect_uri": self.redirect_uri, "resource": RESOURCE})
            claims = _validated_identity(str(body.get("id_token") or ""), client_id, self.nonce)
            _save_tokens(body, client_id=client_id, claims=claims)
        except (ChatGPTPlanError, CredentialStoreError) as exc:
            self.error = str(exc)
        finally:
            self.done.set()
            threading.Thread(target=self.server.shutdown, daemon=True).start()

    def expired(self) -> bool:
        return datetime.now(UTC) - self.started > LOGIN_WINDOW

    def stop(self):
        self.done.set()
        threading.Thread(target=self.server.shutdown, daemon=True).start()


_lock = threading.RLock()
_pending: _Login | None = None


def status() -> dict:
    with _lock:
        if _pending is not None and not _pending.done.is_set() and _pending.expired():
            _pending.stop()
            _pending.error = "ChatGPT 授權逾時，請重新連線。"
        pending = _pending is not None and not _pending.done.is_set()
        error = _pending.error if _pending is not None and _pending.done.is_set() else None
    saved = read_metadata(_METADATA)
    authenticated = bool(saved.get("connected")) and not pending
    result = {"authenticated": authenticated, "available": True,
              "email": saved.get("email") if authenticated else None,
              "auth_source": "chatgpt_plan" if authenticated else None,
              "login_pending": pending, "status_known": True}
    if error:
        result["error"] = error
    return result


def require_authorized() -> str:
    return access_token()


@router.get("")
@router.get("/status")
def get_status():
    return status()


@router.post("/check")
def check_status():
    try:
        access_token()
    except ChatGPTPlanError as exc:
        return status() | {"authenticated": False, "error": str(exc)}
    return status()


@router.post("/login")
def login():
    global _pending
    with _lock:
        if _pending is not None and not _pending.done.is_set():
            _pending.stop()
        try:
            _pending = _Login()
        except OSError as exc:
            raise HTTPException(503, "無法開啟本機授權回呼，請稍後重試。") from exc
        return {"auth_url": _pending.auth_url, "status": status()}


@router.post("/cancel")
def cancel():
    with _lock:
        if _pending is not None and not _pending.done.is_set():
            _pending.stop()
    return status()


@router.post("/logout")
def logout():
    # Removes this app's tokens; the ChatGPT account itself stays signed in.
    delete_credentials(_CREDENTIALS)
    write_metadata(_METADATA, {**read_metadata(_METADATA), "connected": False, "email": None})
    return status()


@router.post("/models")
def models():
    from .codex_bridge import CodexError, CodexRpc  # Imported here to avoid an import cycle.

    try:
        rpc = CodexRpc(plan_token=access_token())
        try:
            result = rpc.request("model/list", {"includeHidden": False})
        finally:
            rpc.close()
    except (ChatGPTPlanError, CodexError, TimeoutError) as exc:
        raise HTTPException(503, str(exc)) from exc
    catalog = [{key: item.get(key) for key in ("id", "model", "displayName", "isDefault")}
               for item in result.get("data", [])]
    write_metadata(_METADATA, {**read_metadata(_METADATA), "models": catalog})
    return {"data": catalog, "nextCursor": result.get("nextCursor")}


@router.get("/models")
def cached_models():
    return {"data": read_metadata(_METADATA).get("models", []), "nextCursor": None}
