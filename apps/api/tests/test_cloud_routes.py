import pytest

from trade_helper.cloud_routes import allowed


@pytest.mark.parametrize("request_", [
    {"method": "GET", "path": "/api/v1/analyses", "query": "limit=20"},
    {"method": "GET", "path": "/api/v1/settings", "query": ""},
    {"method": "POST", "path": "/api/v1/auth/codex/check"},
    {"method": "GET", "path": "/api/v1/analyses/ana_1/chart-snapshot"},
    {"method": "GET", "path": "/api/v1/market-context", "query": "market_id=binance%3Aperp%3ABTCUSDT&timeframe=1h"},
    {"method": "POST", "path": "/api/v1/analyses", "body": {"kind": "market"}, "idempotency_key": "remote-12345"},
    {"method": "POST", "path": "/api/v1/discussions/analysis/ana_1/messages/msg_1/retry", "body": {}},
    {"method": "PATCH", "path": "/api/v1/settings", "body": {"favorite_market_ids": ["binance:perp:BTCUSDT"]}},
    {"method": "DELETE", "path": "/api/v1/positions/pos_1"},
    {"method": "POST", "path": "/api/v1/positions/pos_1.2/close"},
])
def test_screens_reach_their_routes(request_):
    assert allowed(request_)


@pytest.mark.parametrize("request_", [
    {"method": "POST", "path": "/api/v1/auth/codex/login"},
    {"method": "POST", "path": "/api/v1/auth/claude_code/logout"},
    {"method": "GET", "path": "/api/v1/cloud-account"},
    {"method": "POST", "path": "/api/v1/cloud-account/remote/disable"},
    {"method": "GET", "path": "/api/v1/discussions/analysis/ana_1/stream"},
    {"method": "POST", "path": "/api/v1/integrations/binance/test"},
    {"method": "GET", "path": "/api/v1/analyses/../settings"},
    # A segment of dots alone would be resolved to a route one level up.
    {"method": "POST", "path": "/api/v1/positions/../close"},
    {"method": "POST", "path": "/api/v1/positions/./close"},
    {"method": "DELETE", "path": "/api/v1/positions/.."},
    {"method": "GET", "path": "/api/v1/analyses/.."},
    {"method": "GET", "path": "/api/v1/analyses/ana_1/"},
    {"method": "PUT", "path": "/api/v1/settings", "body": {"favorite_market_ids": []}},
    {"method": "PATCH", "path": "/api/v1/settings", "body": {"binance_api_key": "k"}},
    {"method": "PATCH", "path": "/api/v1/settings", "body": {"model_provider": "codex"}},
    {"method": "GET", "path": "/api/v1/positions", "body": {}},
    {"method": "GET", "path": "/api/v1/positions", "query": "a=<script>"},
    {"method": "POST", "path": "/api/v1/analyses", "idempotency_key": "short"},
    {"method": "POST", "path": "/api/v1/positions", "body": {"notes": "x" * 70_000}},
])
def test_everything_else_is_refused(request_):
    assert not allowed(request_)


def test_only_a_discussion_reply_may_stream():
    path = "/api/v1/discussions/analysis/ana_1/stream"
    assert allowed({"method": "GET", "path": path, "stream": True, "connection_id": "c"})
    assert not allowed({"method": "GET", "path": path})
    assert not allowed({"method": "GET", "path": "/api/v1/positions", "stream": True})
    assert not allowed({"method": "GET", "path": path, "stream": True, "query": "limit=100"})
    assert not allowed({"method": "GET", "path": "/api/v1/discussions/analysis/../stream", "stream": True,
                        "connection_id": "c"})


def test_every_ai_provider_can_be_checked_remotely_but_never_signed_in_or_keyed():
    for provider in ("codex", "claude_code", "chatgpt_plan", "anthropic", "openai"):
        assert allowed({"method": "GET", "path": f"/api/v1/auth/{provider}/status"})
        assert allowed({"method": "POST", "path": f"/api/v1/auth/{provider}/check"})
        for action in ("login", "logout", "cancel", "models"):
            assert not allowed({"method": "POST", "path": f"/api/v1/auth/{provider}/{action}"})
    assert not allowed({"method": "POST", "path": "/api/v1/auth/other/check"})
