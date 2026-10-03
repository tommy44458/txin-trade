import json
import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from trade_helper import local_settings
from trade_helper.api import app
from trade_helper.db import connect, database_path
from trade_helper.news_classification_worker import run_once

ALL_FALSE_INTEGRATIONS = {name: {"configured": False} for name in ("bingx", "binance", "jev", "jblanked", "openai", "anthropic")}


@pytest.fixture
def credentials(monkeypatch):
    stored = {}
    monkeypatch.setattr(local_settings, "load_credentials", lambda name, **_kwargs: stored.get(name))
    monkeypatch.setattr(local_settings, "save_credentials", lambda name, value: stored.__setitem__(name, value))
    monkeypatch.setattr(local_settings, "delete_credentials", lambda name: stored.pop(name, None))
    monkeypatch.setattr(local_settings, "credential_status", lambda name: name in stored)
    return stored


def test_desktop_default_and_optional_keys_do_not_inherit_environment(monkeypatch, credentials):
    monkeypatch.setenv("APP_DESKTOP", "1")
    monkeypatch.setenv("BINGX_API_KEY", "environment-key")
    monkeypatch.setenv("BINGX_API_SECRET", "environment-secret")
    monkeypatch.setenv("TYPESAFE_API_KEY", "environment-jev")
    monkeypatch.setenv("JBLANKED_API_KEY", "environment-jblanked")
    monkeypatch.setenv("OPENAI_API_KEY", "environment-openai")
    settings = local_settings.public_settings()
    assert settings["model_provider"] == "codex"
    assert settings["model"] == ""
    assert settings["integrations"] == ALL_FALSE_INTEGRATIONS
    assert all(local_settings.integration_credentials(name) is None for name in ALL_FALSE_INTEGRATIONS)


def test_credentials_never_return_and_blank_fields_preserve_then_clear(credentials):
    client = TestClient(app)
    response = client.put("/api/v1/settings", json={"bingx_api_key": "private-key",
        "bingx_api_secret": "private-secret", "jev_api_key": "private-jev"})
    assert response.status_code == 200
    assert response.json()["integrations"] == {**ALL_FALSE_INTEGRATIONS,
        "bingx": {"configured": True}, "jev": {"configured": True}}
    assert "private" not in response.text
    saved = dict(credentials)
    assert client.put("/api/v1/settings", json={"bingx_api_key": "", "bingx_api_secret": "",
        "jev_api_key": ""}).status_code == 200
    assert credentials == saved
    response = client.put("/api/v1/settings", json={"clear_bingx": True, "clear_jev": True})
    assert response.json()["integrations"] == ALL_FALSE_INTEGRATIONS
    assert credentials == {}
    persisted = local_settings.preferences()
    assert "private" not in str(persisted)
    assert not (local_settings.data_directory() / "settings.json").exists()


def test_validation_response_does_not_echo_secret_even_for_pair_error(credentials):
    response = TestClient(app).put("/api/v1/settings", json={"bingx_api_key": "do-not-echo-this-key"})
    assert response.status_code == 422
    assert "do-not-echo" not in response.text
    assert all(set(error) <= {"loc", "type", "msg"} for error in response.json()["detail"])
    assert credentials == {}


def test_settings_models_are_separate_for_each_provider(credentials):
    client = TestClient(app)
    assert client.put("/api/v1/settings", json={"model_provider": "codex", "model": "codex-model"}).json()["model"] == "codex-model"
    assert client.put("/api/v1/settings", json={"model_provider": "claude_code"}).json()["model"] == ""
    assert client.put("/api/v1/settings", json={"model": "sonnet"}).json()["model"] == "sonnet"
    assert client.put("/api/v1/settings", json={"model_provider": "codex"}).json()["model"] == "codex-model"
    assert client.put("/api/v1/settings", json={"model": ""}).json()["model"] == ""
    assert client.put("/api/v1/settings", json={"model_provider": "claude_code"}).json()["model"] == "sonnet"


def test_retired_chatgpt_provider_is_rejected_and_falls_back(credentials):
    client = TestClient(app)
    assert client.put("/api/v1/settings", json={"model_provider": "chatgpt"}).status_code == 422
    local_settings.patch_preferences({"model_provider": "chatgpt"})
    assert client.get("/api/v1/settings").json()["model_provider"] in {"codex", "openai"}


def test_favorites_survive_reload_and_other_settings_updates(credentials):
    client = TestClient(app)
    favorites = ["binance:perp:DOGEUSDT", "binance:perp:币安人生USDT", "binance:perp:BTCUSDT"]
    response = client.put("/api/v1/settings", json={"favorite_market_ids": favorites + favorites[:1]})
    assert response.status_code == 200
    assert response.json()["favorite_market_ids"] == favorites
    client.put("/api/v1/settings", json={"model_provider": "codex", "model": "selected-model"})
    local_settings.patch_preferences({"codex_disconnected": True})
    local_settings.patch_preferences(remove=("codex_disconnected",))
    reloaded = TestClient(app).get("/api/v1/settings").json()
    assert reloaded["favorite_market_ids"] == favorites
    assert reloaded["model"] == "selected-model"
    assert local_settings.preferences()["favorite_market_ids"] == favorites
    assert credentials == {}
    response = client.put("/api/v1/settings", json={"favorite_market_ids": []})
    assert response.json()["favorite_market_ids"] == []
    assert response.json()["model"] == "selected-model"


@pytest.mark.parametrize("market_id", ["https://invalid.example", "binance:perp:BTC/USDT",
                                      "binance:perp:", "binance:perp:BTCUSDT\n",
                                      "binance:perp:../secret", "binance:perp:BTCUSDC"])
def test_favorite_ids_reject_unsafe_input_without_altering_preferences(credentials, market_id):
    client = TestClient(app)
    saved = ["binance:perp:BTCUSDT"]
    client.put("/api/v1/settings", json={"favorite_market_ids": saved})
    response = client.put("/api/v1/settings", json={"favorite_market_ids": [market_id]})
    assert response.status_code == 422
    assert client.get("/api/v1/settings").json()["favorite_market_ids"] == saved


def test_desktop_api_requires_session_token_and_rejects_rebinding_host(monkeypatch, credentials):
    monkeypatch.setenv("APP_DESKTOP", "1")
    monkeypatch.setenv("APP_DESKTOP_TOKEN", "session-token")
    client = TestClient(app)
    assert client.get("/api/v1/settings").status_code == 401
    assert client.get("/api/v1/settings", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/v1/settings", headers={"Authorization": "Bearer session-token"}).status_code == 200
    assert client.get("/api/v1/health", headers={"Authorization": "Bearer session-token", "Host": "evil.example"}).status_code == 403


def test_missing_jev_does_not_queue_or_claim_any_classification(monkeypatch, credentials):
    monkeypatch.setenv("APP_DESKTOP", "1")
    monkeypatch.setattr("trade_helper.news_classification_worker.enqueue_candidates", lambda *_: pytest.fail("disabled worker queued news"))
    monkeypatch.setattr("trade_helper.news_classification_worker._claim", lambda *_: pytest.fail("disabled worker claimed news"))
    assert run_once() == {"status": "disabled", "queued": 0}


def test_worker_reads_new_key_without_restart(monkeypatch, credentials):
    monkeypatch.setenv("APP_DESKTOP", "1")
    monkeypatch.setattr("trade_helper.news_classification_worker.enqueue_candidates", lambda *_: 3)
    monkeypatch.setattr("trade_helper.news_classification_worker._claim", lambda *_: (None, "idle"))
    assert run_once()["status"] == "disabled"
    local_settings.update_settings(local_settings.SettingsUpdate(jev_api_key="new-jev-key"))
    assert run_once() == {"status": "idle", "queued": 3}
    local_settings.update_settings(local_settings.SettingsUpdate(clear_jev=True))
    assert run_once() == {"status": "disabled", "queued": 0}


@pytest.mark.parametrize("legacy", [False, True])
def test_passive_settings_and_favorites_never_open_keychain(monkeypatch, legacy):
    monkeypatch.setenv("APP_DESKTOP", "1")
    monkeypatch.setenv("APP_DESKTOP_TOKEN", "test-desktop-token")
    if legacy:
        local_settings.patch_preferences({"managed_integrations": list(ALL_FALSE_INTEGRATIONS)})
    def no_vault(*_args, **_kwargs):
        pytest.fail("Browsing settings or favorites must not open the OS vault")
    monkeypatch.setattr(local_settings, "load_credentials", no_vault)
    client = TestClient(app, headers={"Authorization": "Bearer test-desktop-token"})
    assert client.get("/api/v1/settings").status_code == 200
    assert client.get("/api/v1/integrations/bingx").status_code == 200
    response = client.put("/api/v1/settings", json={"favorite_market_ids": ["binance:perp:BTCUSDT"]})
    assert response.status_code == 200
    assert response.json()["favorite_market_ids"] == ["binance:perp:BTCUSDT"]
    assert run_once() == {"status": "disabled", "queued": 0}
    assert local_settings.integration_credentials("jev") is None
    assert local_settings.integration_credentials("jblanked") is None
    assert local_settings.integration_credentials("openai") is None
    if legacy:
        assert response.json()["integrations"]["bingx"]["needs_migration"] is True
        assert response.json()["integrations"]["jblanked"]["needs_migration"] is True
        assert response.json()["integrations"]["openai"]["needs_migration"] is True


def test_legacy_flags_stay_pending_until_explicit_migration(monkeypatch, credentials):
    monkeypatch.setenv("APP_DESKTOP", "1")
    local_settings.patch_preferences({"managed_integrations": ["bingx", "jev"]})
    assert local_settings.integration_credentials("bingx") is None
    assert local_settings.integration_credentials("bingx", allow_interaction=True) is None
    assert local_settings.integration_status("bingx") == {"configured": False, "needs_migration": True, "needs_reentry": True}
    credentials["bingx"] = {"api_key": "private-key", "api_secret": "private-secret"}
    credentials["jev"] = {"disabled": True}
    local_settings.mark_integrations_configured({"bingx": True, "jev": False})
    assert local_settings.integration_credentials("bingx", allow_interaction=True) == credentials["bingx"]
    assert local_settings.integration_status("bingx") == {"configured": True}
    assert local_settings.integration_credentials("jev", allow_interaction=True) is None
    assert local_settings.integration_status("jev") == {"configured": False}
    assert len(credentials) == 2
    assert "private" not in json.dumps(local_settings.preferences())


def test_vault_save_failure_does_not_claim_configured(monkeypatch):
    def fail(*_args):
        raise local_settings.CredentialStoreError("儲存失敗")
    monkeypatch.setattr(local_settings, "save_credentials", fail)
    response = TestClient(app).put("/api/v1/settings", json={"jev_api_key": "private-key"})
    assert response.status_code == 503
    assert local_settings.integration_status("jev") == {"configured": False}


def test_clearing_unconfigured_integrations_never_creates_master_key(monkeypatch):
    monkeypatch.setenv("APP_DESKTOP", "1")
    monkeypatch.setenv("APP_DESKTOP_TOKEN", "test-desktop-token")
    monkeypatch.setattr(local_settings, "save_credentials", lambda *_: pytest.fail("clear must not encrypt a disabled marker"))
    response = TestClient(app).put("/api/v1/settings", json={f"clear_{name}": True for name in ALL_FALSE_INTEGRATIONS},
                                   headers={"Authorization": "Bearer test-desktop-token"})
    assert response.status_code == 200
    assert response.json()["integrations"] == ALL_FALSE_INTEGRATIONS
    with connect(readonly=True) as db:
        assert db.execute("SELECT COUNT(*) AS count FROM credential_local_keys").fetchone()["count"] == 0
        assert db.execute("SELECT COUNT(*) AS count FROM credential_records").fetchone()["count"] == 0


def test_preferences_import_legacy_json_once_and_leave_original_untouched(credentials):
    directory = local_settings.data_directory()
    directory.mkdir(parents=True)
    legacy = {"model_provider": "codex", "models": {"codex": "legacy-model"},
              "favorite_market_ids": ["binance:perp:BTCUSDT"],
              "managed_integrations": ["bingx"], "custom_display": {"compact": True},
              "api_key": "never-import-this", "nested": {"refresh_token": "never-import-token",
                "clientSecret": "never-import-registration", "access-Token": "never-import-camel-token"}}
    original = json.dumps(legacy)
    (directory / "settings.json").write_text(original)
    actual = local_settings.preferences()
    assert actual["models"] == legacy["models"]
    assert actual["custom_display"] == legacy["custom_display"]
    assert "never-import" not in json.dumps(actual)
    assert (directory / "settings.json").read_text() == original
    local_settings.update_settings(local_settings.SettingsUpdate(favorite_market_ids=[]))
    local_settings._INITIALIZED_PREFERENCES.clear()
    assert local_settings.favorite_market_ids() == []
    assert (directory / "settings.json").read_text() == original
    with connect(readonly=True) as db:
        row = db.execute("SELECT value_json FROM app_preferences WHERE user_id=?", ("local-demo",)).fetchone()
    assert json.loads(row["value_json"])["favorite_market_ids"] == []


def test_trading_preferences_defaults_and_partial_merge(credentials):
    client = TestClient(app)
    assert client.get("/api/v1/settings").json()["trading_preferences"] == {
        "directional_bias": None, "risk_tolerance": None, "trading_style": None,
        "timeframe": "1h", "leverage": 5, "market_id": None,
    }
    assert client.put("/api/v1/settings", json={"trading_preferences": {
        "directional_bias": "bearish", "risk_tolerance": "high", "trading_style": "left",
        "timeframe": "4h", "leverage": 25, "market_id": "binance:perp:DOGEUSDT",
    }}).status_code == 200
    client.put("/api/v1/settings", json={"trading_preferences": {"risk_tolerance": "low"},
                                         "favorite_market_ids": ["binance:perp:BTCUSDT"]})
    local_settings.mark_integrations_configured({"jev": False})
    local_settings.patch_preferences({"codex_disconnected": True})
    actual = client.put("/api/v1/settings", json={"trading_preferences": {"directional_bias": None}}).json()
    assert actual["trading_preferences"] == {
        "directional_bias": None, "risk_tolerance": "low", "trading_style": "left",
        "timeframe": "4h", "leverage": 25, "market_id": "binance:perp:DOGEUSDT",
    }
    assert actual["favorite_market_ids"] == ["binance:perp:BTCUSDT"]
    assert local_settings.preferences()["codex_disconnected"] is True
    assert "private" not in database_path().read_bytes().decode(errors="replace")


@pytest.mark.parametrize("update", [
    {"risk_tolerance": "aggressive"}, {"directional_bias": "long"}, {"trading_style": "center"},
    {"timeframe": "1m"}, {"leverage": 0}, {"leverage": 126}, {"market_id": "../secrets"},
    {"market_id": "binance:perp:BTCUSDC"}, {"timeframe": None}, {"leverage": None},
])
def test_invalid_trading_preferences_do_not_alter_persisted_settings(credentials, update):
    client = TestClient(app)
    original = client.get("/api/v1/settings").json()["trading_preferences"]
    assert client.put("/api/v1/settings", json={"trading_preferences": update}).status_code == 422
    assert client.get("/api/v1/settings").json()["trading_preferences"] == original


def test_preference_writers_merge_changes_across_processes(credentials):
    local_settings.preferences()
    program = """
import sys
from trade_helper.local_settings import SettingsUpdate, patch_preferences, update_settings
number = int(sys.argv[1])
for index in range(12):
    patch_preferences({f'process_{number}_{index}': index})
if number == 0:
    update_settings(SettingsUpdate(favorite_market_ids=['binance:perp:BTCUSDT']))
elif number == 1:
    update_settings(SettingsUpdate(trading_preferences={'risk_tolerance': 'high'}))
elif number == 2:
    update_settings(SettingsUpdate(trading_preferences={'directional_bias': 'bearish'}))
else:
    update_settings(SettingsUpdate(model_provider='codex', model='saved-model'))
"""
    workers = [subprocess.Popen([sys.executable, "-c", program, str(index)], env=os.environ.copy(),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
               for index in range(4)]
    for worker in workers:
        stdout, stderr = worker.communicate(timeout=30)
        assert worker.returncode == 0, stdout + stderr
    saved = local_settings.preferences()
    for number in range(4):
        assert all(saved[f"process_{number}_{index}"] == index for index in range(12))
    assert saved["favorite_market_ids"] == ["binance:perp:BTCUSDT"]
    assert saved["trading_preferences"] == {"risk_tolerance": "high", "directional_bias": "bearish"}
    assert saved["models"]["codex"] == "saved-model"


def test_user_preferences_are_isolated_by_local_user_id(monkeypatch, credentials):
    local_settings.patch_preferences({"favorite_market_ids": ["binance:perp:BTCUSDT"]})
    monkeypatch.setenv("APP_LOCAL_USER_ID", "second-local-user")
    assert local_settings.favorite_market_ids() == []
    local_settings.patch_preferences({"favorite_market_ids": ["binance:perp:ETHUSDT"]})
    monkeypatch.setenv("APP_LOCAL_USER_ID", "local-demo")
    assert local_settings.favorite_market_ids() == ["binance:perp:BTCUSDT"]


@pytest.mark.parametrize("name,env_key", [("jblanked", "JBLANKED_API_KEY"), ("openai", "OPENAI_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY")])
def test_optional_calendar_and_legacy_model_keys_save_without_changing_context_and_clear_without_resurrection(
        monkeypatch, credentials, name, env_key):
    monkeypatch.setenv("APP_DESKTOP", "1")
    monkeypatch.setenv("APP_DESKTOP_TOKEN", "test-desktop-token")
    monkeypatch.setenv(env_key, "environment-key")
    client = TestClient(app, headers={"Authorization": "Bearer test-desktop-token"})
    initial = {"model_provider": "codex", "model": "retained-model",
               "favorite_market_ids": ["binance:perp:SOLUSDT"],
               "trading_preferences": {"risk_tolerance": "high", "trading_style": "left", "leverage": 20}}
    assert client.put("/api/v1/settings", json=initial).status_code == 200
    assert not client.get("/api/v1/settings").json()["integrations"][name]["configured"]
    response = client.put("/api/v1/settings", json={f"{name}_api_key": f" private-{name}-key "})
    assert response.status_code == 200
    assert credentials[name] == {"api_key": f"private-{name}-key"}
    assert response.json()["integrations"][name] == {"configured": True}
    assert "private" not in response.text
    assert local_settings.integration_credentials(name) == credentials[name]
    assert client.get("/api/v1/settings").json()["model_provider"] == "codex"
    assert client.get("/api/v1/settings").json()["model"] == "retained-model"
    assert client.put("/api/v1/settings", json={f"{name}_api_key": "   "}).status_code == 200
    assert credentials[name] == {"api_key": f"private-{name}-key"}
    response = client.put("/api/v1/settings", json={f"clear_{name}": True})
    assert response.json()["integrations"][name] == {"configured": False}
    assert name not in credentials
    assert local_settings.integration_credentials(name) is None
    monkeypatch.setenv("APP_DESKTOP", "0")
    assert local_settings.integration_credentials(name) is None
    actual = client.get("/api/v1/settings").json()
    assert actual["favorite_market_ids"] == initial["favorite_market_ids"]
    assert actual["trading_preferences"]["risk_tolerance"] == "high"
    assert actual["trading_preferences"]["trading_style"] == "left"
    assert "private" not in json.dumps(local_settings.preferences())


@pytest.mark.parametrize("name", ["jblanked", "openai"])
@pytest.mark.parametrize("conflict", [False, True])
def test_calendar_and_legacy_model_key_validation_never_echoes_secrets(credentials, name, conflict):
    body = {f"{name}_api_key": "do-not-echo-this-secret" if conflict else "do-not-echo-" + "x" * 4100}
    if conflict:
        body[f"clear_{name}"] = True
    response = TestClient(app).put("/api/v1/settings", json=body)
    assert response.status_code == 422
    assert "do-not-echo" not in response.text
    assert credentials == {}


@pytest.mark.parametrize("name,env_key", [("jblanked", "JBLANKED_API_KEY"), ("openai", "OPENAI_API_KEY"), ("anthropic", "ANTHROPIC_API_KEY")])
def test_browser_optional_key_environment_fallback_is_never_imported_into_sqlite(monkeypatch, credentials, name, env_key):
    monkeypatch.setenv("APP_DESKTOP", "0")
    monkeypatch.setenv(env_key, "developer-key")
    assert local_settings.integration_status(name) == {"configured": True}
    assert local_settings.integration_credentials(name) == {"api_key": "developer-key"}
    assert credentials == {}
    assert local_settings.preferences() == {}
    monkeypatch.setenv("APP_DESKTOP", "1")
    assert local_settings.integration_status(name) == {"configured": False}
    assert local_settings.integration_credentials(name) is None


def test_migration_flags_for_all_integrations_preserve_preferences(credentials):
    local_settings.patch_preferences({"favorite_market_ids": ["binance:perp:SOLUSDT"],
                                       "trading_preferences": {"risk_tolerance": "high"},
                                       "model_provider": "codex"})
    for name in ALL_FALSE_INTEGRATIONS:
        credentials[name] = {"api_key": "private-key"}
    local_settings.mark_integrations_configured({name: True for name in ALL_FALSE_INTEGRATIONS})
    assert all(item["configured"] for item in local_settings.public_settings()["integrations"].values())
    saved = local_settings.preferences()
    assert set(saved["managed_integrations"]) == set(ALL_FALSE_INTEGRATIONS)
    assert saved["favorite_market_ids"] == ["binance:perp:SOLUSDT"]
    assert saved["trading_preferences"] == {"risk_tolerance": "high"}
    assert saved["model_provider"] == "codex"
    assert "private" not in json.dumps(saved)
