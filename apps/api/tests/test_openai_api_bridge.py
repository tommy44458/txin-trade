from types import SimpleNamespace

import httpx
import openai

from trade_helper import openai_api_bridge as bridge


class FakeClient:
    def __init__(self, ids=(), error=None, **_options):
        self.ids, self.error, self.closed = ids, error, False
        self.models = SimpleNamespace(list=self.list)

    def list(self):
        if self.error:
            raise self.error
        return [SimpleNamespace(id=item) for item in self.ids]

    def close(self):
        self.closed = True


def with_key(monkeypatch, key="sk-test"):
    monkeypatch.setattr(bridge, "integration_credentials", lambda _name: {"api_key": key} if key else None)


def test_catalog_keeps_text_models_and_becomes_the_default(monkeypatch):
    with_key(monkeypatch)
    client = FakeClient(ids=("gpt-6.1", "gpt-6.1-mini", "o5", "text-embedding-4", "gpt-6.1-realtime",
                             "gpt-image-2", "whisper-2", "tts-2"))
    result, catalog = bridge._live(client_factory=lambda **_: client)
    assert result["authenticated"] is True and client.closed
    assert [item["id"] for item in catalog] == ["o5", "gpt-6.1-mini", "gpt-6.1"]
    assert bridge.cached_models()["data"] == catalog
    assert bridge.default_model() == "o5"


def test_invalid_key_is_reported_without_provider_text(monkeypatch):
    with_key(monkeypatch)
    request = httpx.Request("GET", "https://api.openai.com/v1/models")
    denied = openai.AuthenticationError("Incorrect API key sk-test", response=httpx.Response(401, request=request), body=None)
    result, catalog = bridge._live(client_factory=lambda **_: FakeClient(error=denied))
    assert catalog is None and result["authenticated"] is False
    assert result["error"] == "OpenAI API 金鑰無效或已撤銷，請到設定重新輸入。"
    assert "sk-test" not in str(result)


def test_without_a_key_settings_ask_for_one(monkeypatch):
    with_key(monkeypatch, key=None)
    assert bridge.status()["authenticated"] is False
    assert bridge.check_status()["error"] == bridge.KEY_HINT
    assert bridge.default_model() is None
