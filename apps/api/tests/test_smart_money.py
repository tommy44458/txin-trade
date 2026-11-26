import httpx
import pytest
from fastapi.testclient import TestClient

from trade_helper import smart_money
from trade_helper.api import app

ORIGIN = "https://cloud.test"


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setenv("TXINTRADE_CLOUD_ORIGIN", ORIGIN)
    smart_money.clear_cache()
    state = {"down": False, "missing": False, "calls": []}
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"].append(str(request.url))
        assert "Authorization" not in request.headers
        if state["down"]:
            return httpx.Response(503)
        if state["missing"]:  # A cloud that has not been given smart money yet.
            return httpx.Response(404)
        if request.url.path == "/smart-money/overview":
            return httpx.Response(200, json={"assets": [{"asset": "BTC"}, {"asset": "ETH"}]})
        if request.url.path == "/smart-money/assets/LINK":
            return httpx.Response(200, json={"asset": "LINK", "window": request.url.params["window"]})
        if request.url.path == "/smart-money/reserves":
            return httpx.Response(200, json={"through": 1, "assets": {"BTC": {"amount": 1}}})
        if request.url.path == "/smart-money/reserves/BTC":
            return httpx.Response(200, json={"asset": "BTC", "window": request.url.params["window"]})
        if state.get("old_cloud") and request.url.path.startswith("/smart-money/reserves"):
            return httpx.Response(404, json={"error": {"code": "not_found"}})
        return httpx.Response(404, json={"error": {"code": "asset_not_tracked"}})

    monkeypatch.setattr(smart_money.httpx, "Client",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    yield state
    smart_money.clear_cache()


def test_overview_is_relayed_from_the_cloud_without_signing_in(cloud):
    response = TestClient(app).get("/api/v1/smart-money/overview")
    assert response.status_code == 200
    assert [a["asset"] for a in response.json()["assets"]] == ["BTC", "ETH"]
    assert cloud["calls"] == [f"{ORIGIN}/smart-money/overview"]


def test_answers_are_cached_for_a_minute(cloud, monkeypatch):
    client = TestClient(app)
    clock = [1000.0]
    monkeypatch.setattr(smart_money.time, "monotonic", lambda: clock[0])
    client.get("/api/v1/smart-money/assets/LINK?window=7d")
    client.get("/api/v1/smart-money/assets/LINK?window=7d")
    assert len(cloud["calls"]) == 1
    clock[0] += 61
    client.get("/api/v1/smart-money/assets/LINK?window=7d")
    assert len(cloud["calls"]) == 2


def test_the_last_good_answer_is_kept_while_the_cloud_is_unreachable(cloud, monkeypatch):
    client = TestClient(app)
    clock = [1000.0]
    monkeypatch.setattr(smart_money.time, "monotonic", lambda: clock[0])
    assert "stale" not in client.get("/api/v1/smart-money/overview").json()
    cloud["down"] = True
    clock[0] += 120
    stale = client.get("/api/v1/smart-money/overview")
    assert stale.status_code == 200
    assert stale.json()["stale"] is True
    smart_money.clear_cache()
    failed = client.get("/api/v1/smart-money/overview")
    assert failed.status_code == 503
    assert failed.json()["detail"] == {"code": "smart_money_unavailable"}


def test_untracked_assets_are_404_and_malformed_requests_never_reach_the_cloud(cloud):
    client = TestClient(app)
    assert client.get("/api/v1/smart-money/assets/DOGE").status_code == 404
    assert client.get("/api/v1/smart-money/assets/link").status_code == 404
    assert client.get("/api/v1/smart-money/assets/LINK?window=2y").status_code == 422
    assert client.get("/api/v1/smart-money/assets/LINK?window=1d&x=../../me").json()["window"] == "1d"
    assert cloud["calls"] == [f"{ORIGIN}/smart-money/assets/DOGE?window=1d",
                              f"{ORIGIN}/smart-money/assets/LINK?window=1d"]


def test_remote_pages_reach_fund_flow_follow_ups_but_read_public_flows_from_the_cloud():
    from trade_helper.cloud_routes import STREAM_ROUTE, allowed
    # The public data never needs the computer; the remote page reads it from the cloud.
    assert not allowed({"method": "GET", "path": "/api/v1/smart-money/overview"})
    assert allowed({"method": "GET", "path": "/api/v1/smart-money/snapshots/latest"})
    assert allowed({"method": "POST", "path": "/api/v1/smart-money/snapshots",
                    "body": {"asset": "ETH", "window": "1d"}})
    assert allowed({"method": "GET", "path": "/api/v1/discussions/fund_flows/flow_abc"})
    assert allowed({"method": "POST", "path": "/api/v1/discussions/fund_flows/flow_abc/messages",
                    "body": {"message": "hi", "request_id": "00000000-0000-4000-8000-000000000001"}})
    assert STREAM_ROUTE.fullmatch("/api/v1/discussions/fund_flows/flow_abc/stream")
    assert not allowed({"method": "DELETE", "path": "/api/v1/smart-money/snapshots"})



def test_a_cloud_without_smart_money_is_unavailable_not_untracked(cloud):
    cloud["missing"] = True
    response = TestClient(app).get("/api/v1/smart-money/overview")
    assert response.status_code == 503
    assert response.json()["detail"] == {"code": "smart_money_unavailable"}


def test_reserves_are_relayed_and_an_older_cloud_is_not_mistaken_for_an_untracked_coin(cloud):
    client = TestClient(app)
    assert client.get("/api/v1/smart-money/reserves").json()["assets"]["BTC"]["amount"] == 1
    assert client.get("/api/v1/smart-money/reserves/BTC").json() == {"asset": "BTC", "window": "30d"}
    assert client.get("/api/v1/smart-money/reserves/BTC?window=1d").status_code == 422
    assert client.get("/api/v1/smart-money/reserves/TON?window=7d").status_code == 404
    cloud["old_cloud"] = True
    smart_money.clear_cache()
    assert client.get("/api/v1/smart-money/reserves/ETH?window=7d").status_code == 503
