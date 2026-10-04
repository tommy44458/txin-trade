"""Local API routes a signed-in remote browser may reach through the cloud relay.

The cloud checks the same list first; this copy is the computer's own decision.
Keys, AI sign-in, cloud sign-in, updates and raw streams are never listed.
"""

import json
import re

# A segment of dots alone would walk up this computer's path ("/positions/../close").
_ID = r"(?!\.+(?:/|$))[A-Za-z0-9_.:-]{1,128}"
# Every AI the computer can analyze with; their status and check never return keys.
_PROVIDERS = "(codex|claude_code|chatgpt_plan|anthropic|openai)"
_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = tuple((method, re.compile(pattern)) for method, pattern in (
    ("GET", r"/api/v1/(session|settings|markets|market-context|candles|quotes|news|events|positions|analyses)"),
    ("GET", r"/api/v1/events/macro-interpretation"),
    ("GET", r"/api/v1/integrations/(binance|bingx)"),
    ("GET", rf"/api/v1/auth/{_PROVIDERS}/status"),
    # Checks the AI sign-in before an analysis; it never signs in or out.
    ("POST", rf"/api/v1/auth/{_PROVIDERS}/check"),
    ("GET", r"/api/v1/analyses/latest"),
    ("GET", rf"/api/v1/analyses/{_ID}(/chart-snapshot|/level-shadow|/level-shadow/v4)?"),
    ("GET", rf"/api/v1/discussions/(analysis|macro|fund_flows)/{_ID}"),
    # Fund-flow follow-ups: the conversation's frozen data lives on the computer.
    ("GET", r"/api/v1/smart-money/snapshots/latest"),
    ("POST", r"/api/v1/smart-money/snapshots"),
    ("POST", r"/api/v1/(analyses|positions)"),
    ("POST", r"/api/v1/positions/(binance|bingx)/sync"),
    ("POST", rf"/api/v1/positions/{_ID}/close"),
    ("PATCH", rf"/api/v1/positions/{_ID}"),
    ("DELETE", rf"/api/v1/positions/{_ID}"),
    ("POST", r"/api/v1/events/macro-interpretation/ensure"),
    ("POST", rf"/api/v1/events/macro-interpretation/{_ID}/translate"),
    ("POST", rf"/api/v1/discussions/(analysis|macro|fund_flows)/{_ID}/messages(/{_ID}/retry)?"),
    ("PATCH", r"/api/v1/settings"),
))
# A discussion reply may stream to the browser that asked for it.
STREAM_ROUTE = re.compile(rf"/api/v1/discussions/(analysis|macro|fund_flows)/{_ID}/stream")
REMOTE_SETTINGS_FIELDS = frozenset({"favorite_market_ids", "trading_preferences", "initial_indicators"})
MAX_BODY_BYTES = 65_536
_QUERY = re.compile(r"[A-Za-z0-9_.~%&=+,:-]{0,2048}")
_KEY = re.compile(r"[A-Za-z0-9:_-]{8,128}")


def allowed(request: dict) -> bool:
    """True only for an allowlisted method and path with an acceptable query and body."""
    method, path, query = request.get("method"), request.get("path"), request.get("query", "")
    if not isinstance(method, str) or not isinstance(path, str) or not isinstance(query, str):
        return False
    if request.get("stream") is True:
        return method == "GET" and bool(STREAM_ROUTE.fullmatch(path)) and not query and "body" not in request
    if not any(method == allowed_method and pattern.fullmatch(path) for allowed_method, pattern in _ROUTES):
        return False
    if not _QUERY.fullmatch(query):
        return False
    key = request.get("idempotency_key")
    if key is not None and (not isinstance(key, str) or not _KEY.fullmatch(key)):
        return False
    if "body" not in request:
        return True
    body = request["body"]
    if method in {"GET", "DELETE"}:
        return False
    if path == "/api/v1/settings" and not (isinstance(body, dict) and set(body) <= REMOTE_SETTINGS_FIELDS):
        return False
    return len(json.dumps(body).encode()) <= MAX_BODY_BYTES
