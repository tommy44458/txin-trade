import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from trade_helper import anthropic_api_bridge as bridge


def block(kind, **fields):
    return SimpleNamespace(type=kind, **fields)


def message(content, stop_reason, model="claude-opus-5-5"):
    return SimpleNamespace(content=content, stop_reason=stop_reason, model=model,
                           usage=SimpleNamespace(input_tokens=100, cache_creation_input_tokens=0,
                                                 cache_read_input_tokens=20, output_tokens=30))


class FakeStream:
    def __init__(self, final, texts=()):
        self.final, self.texts = final, texts

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def __iter__(self):
        return iter(SimpleNamespace(type="text", text=text) for text in self.texts)

    def get_final_message(self):
        return self.final


class FakeClient:
    def __init__(self, turns, **_options):
        self.turns, self.requests, self.closed = list(turns), [], False
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self.stream))

    def stream(self, **request):
        self.requests.append(request)
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn

    def close(self):
        self.closed = True


TOOL = {"type": "function", "name": "rsi", "description": "RSI",
        "parameters": {"type": "object", "properties": {"timeframe": {"type": "string"}}, "required": ["timeframe"]}}


def session(turns):
    client = FakeClient(turns)
    return bridge.AnthropicApiSession("sk-ant-test", client_factory=lambda **_: client), client


def test_tool_loop_runs_registered_tools_and_returns_the_final_report():
    calls = []
    report_session, client = session([
        FakeStream(message([block("tool_use", id="t1", name="rsi", input={"timeframe": "1h"})], "tool_use")),
        FakeStream(message([block("text", text='{"market":"ok"}')], "end_turn")),
    ])
    result = report_session.analyze("instructions", "context", "", [TOOL],
                                    lambda name, args: calls.append((name, args)) or {"value": 61},
                                    timeout=30, effort="high")
    assert result["text"] == '{"market":"ok"}'
    assert result["usage"] == {"inputTokens": 240, "outputTokens": 60}
    assert calls == [("rsi", {"timeframe": "1h"})]
    first, second = client.requests
    assert first["model"] == bridge.DEFAULT_MODEL
    assert first["output_config"] == {"effort": "high"}
    assert first["fallbacks"] == "default" and first["betas"] == [bridge.FALLBACK_BETA]
    assert first["tools"] == [{"name": "rsi", "description": "RSI", "input_schema": TOOL["parameters"],
                               "eager_input_streaming": True}]
    assert first["system"].startswith("instructions")
    # The tool result answers the tool_use in the next user turn.
    result_turn = second["messages"][-1]
    assert result_turn["role"] == "user"
    assert result_turn["content"][0]["tool_use_id"] == "t1"
    assert json.loads(result_turn["content"][0]["content"]) == {"value": 61}


def test_failed_optional_tool_is_reported_as_unavailable_not_as_a_failed_report():
    report_session, client = session([
        FakeStream(message([block("tool_use", id="t1", name="rsi", input={"timeframe": "9h"})], "tool_use")),
        FakeStream(message([block("text", text="{}")], "end_turn")),
    ])

    def reject(_name, _args):
        raise ValueError("Tool timeframe differs from snapshot")

    report_session.analyze("i", "c", "claude-sonnet-5-5", [TOOL], reject, timeout=30)
    error = client.requests[1]["messages"][-1]["content"][0]
    assert error["is_error"] is True
    assert json.loads(error["content"])["status"] == "unavailable"
    assert client.requests[0]["model"] == "claude-sonnet-5-5"


def test_text_discussions_stream_the_visible_reply_without_tools():
    seen = []
    discussion, client = session([FakeStream(message([block("text", text="Hold.")], "end_turn"), texts=("Ho", "ld."))])
    result = discussion.analyze("i", "c", "", [], None, timeout=30, response_format="text", on_text=seen.append)
    assert result["text"] == "Hold."
    assert seen == ["Ho", "Hold."]
    assert "tools" not in client.requests[0]


def test_refusal_and_api_errors_become_plain_messages():
    refused, _ = session([FakeStream(message([], "refusal"))])
    with pytest.raises(bridge.AnthropicApiError, match="拒絕"):
        refused.analyze("i", "c", "", [], None, timeout=30)
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    denied, _ = session([anthropic.AuthenticationError("bad key", response=httpx.Response(401, request=request), body=None)])
    with pytest.raises(bridge.AnthropicApiError, match="金鑰無效"):
        denied.analyze("i", "c", "", [], None, timeout=30)


def test_unparseable_tool_input_is_retried_then_gives_up():
    flaky, client = session([ValueError("bad json"), FakeStream(message([block("text", text="{}")], "end_turn"))])
    assert flaky.analyze("i", "c", "", [TOOL], lambda *_: {}, timeout=30)["text"] == "{}"
    assert len(client.requests) == 2
    broken, _ = session([ValueError("bad json")] * 3)
    with pytest.raises(bridge.AnthropicApiError, match="無法解析"):
        broken.analyze("i", "c", "", [TOOL], lambda *_: {}, timeout=30)


def test_status_needs_a_saved_key(monkeypatch):
    monkeypatch.setattr(bridge, "integration_credentials", lambda _name: None)
    assert bridge.status()["authenticated"] is False
    assert bridge.check_status()["error"] == bridge.KEY_HINT
    with pytest.raises(bridge.AnthropicApiError):
        bridge.require_authorized()
    monkeypatch.setattr(bridge, "integration_credentials", lambda _name: {"api_key": "sk-ant-test"})
    assert bridge.status() == {"authenticated": True, "available": True, "auth_source": "anthropic_api_key",
                               "login_pending": False, "status_known": True}
    assert bridge.require_authorized() == "sk-ant-test"
