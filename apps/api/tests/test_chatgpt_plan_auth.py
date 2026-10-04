import base64
import hashlib
import subprocess
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from trade_helper import chatgpt_plan_auth as plan
from trade_helper import codex_bridge
from trade_helper.auth_metadata import read_metadata, write_metadata
from trade_helper.credential_store import load_credentials, save_credentials

from .test_codex_bridge import fake_codex  # noqa: F401 - reused fixture


def token_body(**changes):
    return {"access_token": "access-1", "refresh_token": "refresh-1", "id_token": "id-token",
            "expires_in": 3600, "scope": f"openid email offline_access resource.invoke {plan.PLAN_SCOPE}"} | changes


@pytest.fixture
def login(monkeypatch):
    """A pending sign-in whose OpenAI calls are answered locally."""
    requests = []

    def exchange(data, *, post=None):
        requests.append(data)
        return token_body()

    monkeypatch.setattr(plan, "_token_request", exchange)
    monkeypatch.setattr(plan, "_validated_identity",
                        lambda _token, client_id, nonce: {"sub": "user-1", "email": "a@example.com", "aud": client_id})
    pending = plan._Login()
    yield pending, requests
    pending.stop()


def test_first_sign_in_registers_the_app_with_pkce_and_a_loopback_callback(login):
    pending, _ = login
    url = urlparse(pending.auth_url)
    query = {key: values[0] for key, values in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}{url.path}" == plan.AUTHORIZE_URL
    assert query["client_id"] == plan.REGISTRATION_CLIENT
    assert query["agent_name_hint"] == "txinTrade"
    assert query["ext_agent_host_id"].startswith("urn:uuid:")
    assert query["redirect_uri"].startswith("http://127.0.0.1:") and query["redirect_uri"].endswith("/callback")
    assert query["scope"].split() == plan.SCOPES.split() and query["resource"] == plan.RESOURCE
    assert query["code_challenge_method"] == "S256"
    expected = base64.urlsafe_b64encode(hashlib.sha256(pending.verifier.encode()).digest()).rstrip(b"=").decode()
    assert query["code_challenge"] == expected
    assert query["state"] == pending.state and query["nonce"] == pending.nonce


def test_returning_sign_in_reuses_the_issued_client_and_host():
    write_metadata("chatgpt_plan", {"client_id": "oaiapp_saved", "host_id": "urn:uuid:saved-host"})
    pending = plan._Login()
    try:
        query = parse_qs(urlparse(pending.auth_url).query)
        assert query["client_id"] == ["oaiapp_saved"]
        assert "agent_name_hint" not in query and "ext_agent_host_id" not in query
    finally:
        pending.stop()


def test_callback_exchanges_the_code_and_saves_tokens(login):
    pending, requests = login
    response = httpx.get(pending.redirect_uri, params={"code": "code-1", "state": pending.state,
                                                       "client_id": "oaiapp_new"}, timeout=5)
    assert response.status_code == 200 and "ChatGPT" in response.text
    assert pending.done.wait(5) and pending.error is None
    assert requests == [{"grant_type": "authorization_code", "code": "code-1", "client_id": "oaiapp_new",
                         "code_verifier": pending.verifier, "redirect_uri": pending.redirect_uri,
                         "resource": plan.RESOURCE}]
    saved = load_credentials("chatgpt_plan")
    assert saved["access_token"] == "access-1" and saved["refresh_token"] == "refresh-1"
    assert saved["client_id"] == "oaiapp_new" and saved["email"] == "a@example.com"
    metadata = read_metadata("chatgpt_plan")
    assert metadata["client_id"] == "oaiapp_new" and metadata["connected"] is True
    assert "access-1" not in str(metadata)


def test_callback_with_a_wrong_state_saves_nothing(login):
    pending, requests = login
    httpx.get(pending.redirect_uri, params={"code": "code-1", "state": "forged", "client_id": "x"}, timeout=5)
    assert pending.done.wait(5)
    assert pending.error == "ChatGPT 授權回應不符，請重新連線。"
    assert requests == [] and load_credentials("chatgpt_plan") is None


def test_a_plan_without_the_sharing_scope_is_refused(login, monkeypatch):
    pending, _ = login
    monkeypatch.setattr(plan, "_token_request", lambda data, post=None: token_body(scope="openid email"))
    httpx.get(pending.redirect_uri, params={"code": "c", "state": pending.state, "client_id": "x"}, timeout=5)
    assert pending.done.wait(5)
    assert "Plus 或 Pro" in pending.error


def save_tokens(expires_in_minutes):
    save_credentials("chatgpt_plan", {
        "client_id": "oaiapp_saved", "access_token": "old-access", "refresh_token": "old-refresh",
        "expires_at": (datetime.now(UTC) + timedelta(minutes=expires_in_minutes)).isoformat(),
        "scopes": [plan.PLAN_SCOPE], "email": "a@example.com"})
    write_metadata("chatgpt_plan", {"client_id": "oaiapp_saved", "connected": True})


def test_a_current_token_is_used_and_an_expiring_one_is_refreshed():
    save_tokens(30)
    assert plan.access_token(post=lambda *_a, **_k: pytest.fail("no refresh needed")) == "old-access"
    save_tokens(1)
    sent = []

    def post(url, data, timeout):
        sent.append((url, data))
        return httpx.Response(200, json=token_body(access_token="new-access", refresh_token="new-refresh"))

    assert plan.access_token(post=post) == "new-access"
    assert sent == [(plan.TOKEN_URL, {"grant_type": "refresh_token", "refresh_token": "old-refresh",
                                      "client_id": "oaiapp_saved", "resource": plan.RESOURCE})]
    assert load_credentials("chatgpt_plan")["refresh_token"] == "new-refresh"


def test_a_revoked_refresh_token_signs_out_but_keeps_the_issued_client():
    save_tokens(1)
    with pytest.raises(plan.ChatGPTPlanError, match="重新連線"):
        plan.access_token(post=lambda *_a, **_k: httpx.Response(400, json={"error": "invalid_grant"}))
    assert load_credentials("chatgpt_plan") is None
    assert read_metadata("chatgpt_plan")["client_id"] == "oaiapp_saved"
    assert plan.status()["authenticated"] is False


def test_without_tokens_analysis_asks_to_connect(monkeypatch):
    monkeypatch.setattr(codex_bridge, "cli_installed", lambda: True)
    with pytest.raises(plan.ChatGPTPlanError, match="連線 ChatGPT"):
        plan.require_authorized()


def test_codex_runs_as_the_documented_responses_provider_with_the_plan_token(fake_codex):  # noqa: F811
    captured = []

    def start(*args, **kwargs):
        captured.append((args[0], kwargs))
        return subprocess.Popen(*args, **kwargs)

    rpc = codex_bridge.CodexRpc(plan_token="plan-access", process_factory=start)
    try:
        command, options = captured[0]
        assert options["env"]["ACCESS_TOKEN"] == "plan-access"
        assert options["env"]["CODEX_HOME"].endswith("codex-home")
        flags = " ".join(command)
        for setting in ('model_provider="openai_chatgpt_plan"',
                        'model_providers.openai_chatgpt_plan.base_url="https://api.openai.com/v1"',
                        'model_providers.openai_chatgpt_plan.env_key="ACCESS_TOKEN"',
                        'model_providers.openai_chatgpt_plan.wire_api="responses"',
                        "model_providers.openai_chatgpt_plan.requires_openai_auth=false",
                        "model_providers.openai_chatgpt_plan.supports_websockets=false"):
            assert setting in flags
    finally:
        rpc.close()


def test_ordinary_codex_keeps_its_own_provider(fake_codex):  # noqa: F811
    captured = []

    def start(*args, **kwargs):
        captured.append(SimpleNamespace(command=args[0], env=kwargs["env"]))
        return subprocess.Popen(*args, **kwargs)

    rpc = codex_bridge.CodexRpc(process_factory=start)
    try:
        assert "openai_chatgpt_plan" not in " ".join(captured[0].command)
    finally:
        rpc.close()


def test_without_codex_settings_ask_for_it_before_sign_in_and_analysis_explains(monkeypatch):
    # The plan token only authorizes; analysis itself runs in the local Codex app-server.
    monkeypatch.setattr(codex_bridge, "cli_installed", lambda: False)
    status = plan.status()
    assert status["cli_installed"] is False and status["available"] is False
    started = plan.login()
    assert started["auth_url"] is None and started["status"]["cli_installed"] is False
    assert plan._pending is None or plan._pending.done.is_set()
    with pytest.raises(plan.ChatGPTPlanError, match="找不到 Codex"):
        plan.require_authorized()


def test_with_codex_installed_the_status_says_so(monkeypatch):
    monkeypatch.setattr(codex_bridge, "cli_installed", lambda: True)
    assert plan.status()["cli_installed"] is True
