"""Read-only BingX futures position import for the local app."""

import hashlib
import hmac
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

import httpx

from .local_settings import integration_credentials, integration_status
from .market_catalog import get_catalog, valid_market_id_format

BASE_URL = "https://open-api.bingx.com"
ENDPOINTS = {
    "perpetual": "/openApi/swap/v2/user/positions",
    "standard": "/openApi/contract/v1/allPosition",
}


class BingXError(Exception):
    """Safe, credential-free explanation for the local UI."""


def configured() -> bool:
    return integration_status("bingx")["configured"]


def signed_query(secret: str, timestamp_ms: int) -> str:
    query = urlencode({"recvWindow": 5000, "timestamp": timestamp_ms})
    signature = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    return f"{query}&signature={signature}"


def fetch_positions(kind: str) -> list[dict]:
    credentials = integration_credentials("bingx") or {}
    key, secret = credentials.get("api_key"), credentials.get("api_secret")
    if not key or not secret:
        raise BingXError("請先在設定輸入 BingX API Key 與 Secret")
    path = ENDPOINTS[kind]
    query = signed_query(secret, int(time.time() * 1000))
    try:
        response = httpx.get(
            f"{BASE_URL}{path}?{query}",
            headers={"X-BX-APIKEY": key, "Accept": "application/json"},
            timeout=12.0,
        )
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        # httpx exception text can contain the signed URL; never return it.
        raise BingXError(f"BingX {kind} 持倉查詢失敗；請檢查網路及 API 金鑰權限") from exc
    if not isinstance(payload, dict) or str(payload.get("code")) != "0":
        raise BingXError(f"BingX {kind} 拒絕持倉查詢；請確認金鑰、IP 白名單及系統時間")
    data = payload.get("data")
    if not isinstance(data, list):
        raise BingXError(f"BingX {kind} 回應格式異常，沒有更新本地紀錄")
    return data


def _decimal(value: object, field: str, *, positive: bool = True) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BingXError(f"BingX 持倉的 {field} 格式異常，沒有更新本地紀錄") from exc
    if not number.is_finite() or (positive and number <= 0):
        raise BingXError(f"BingX 持倉的 {field} 格式異常，沒有更新本地紀錄")
    return number


def normalize_positions(kind: str, rows: list[dict]) -> tuple[list[dict], int]:
    result, skipped = [], 0
    seen: set[str] = set()
    # A BingX symbol that matches a Binance stock perpetual may be an unrelated token.
    supported = {market["binance_symbol"]: market for market in get_catalog().markets
                 if market.get("asset_class", "crypto") == "crypto"} if rows else {}
    for row in rows:
        if not isinstance(row, dict):
            raise BingXError("BingX 持倉回應格式異常，沒有更新本地紀錄")
        # BingX uses both SOLUSDT (standard) and SOL-USDT (perpetual).
        compact_symbol = str(row.get("symbol") or "").replace("/", "").replace("-", "").upper()
        if (not valid_market_id_format(f"binance:perp:{compact_symbol}") or
                compact_symbol not in supported):
            skipped += 1
            continue
        market = supported[compact_symbol]
        symbol = f"{market['base_asset']}-{market['quote_asset']}"
        amount = _decimal(row.get("positionAmt"), "數量", positive=False)
        if amount == 0:
            continue
        raw_side = row.get("positionSide")
        if raw_side not in {"LONG", "SHORT", "BOTH"}:
            raise BingXError("BingX 持倉方向格式異常，沒有更新本地紀錄")
        side = ("long" if amount > 0 else "short") if raw_side == "BOTH" else raw_side.lower()
        if kind == "perpetual":
            position_id = str(row.get("positionId") or "").strip()
            if not position_id:
                raise BingXError("BingX 永續持倉缺少 positionId，沒有更新本地紀錄")
            external_id = f"perpetual:{symbol}:{position_id}:{raw_side}"
        else:
            # Standard contract's allPosition response has no positionId.
            opened = row.get("time")
            if opened is None or not str(opened).isdigit():
                raise BingXError("BingX 標準合約缺少開倉時間，沒有更新本地紀錄")
            external_id = f"standard:{symbol}:{raw_side}:{opened}"
        if external_id in seen:
            raise BingXError("BingX 回傳重複持倉，沒有更新本地紀錄")
        seen.add(external_id)
        leverage = _decimal(row.get("leverage"), "槓桿")
        if leverage != int(leverage) or not 1 <= leverage <= 125:
            raise BingXError("BingX 持倉槓桿不在目前支援範圍 1–125 倍")
        isolated = row.get("isolated")
        if not isinstance(isolated, bool):
            raise BingXError("BingX 持倉保證金模式格式異常，沒有更新本地紀錄")
        liquidation = row.get("liquidationPrice") if kind == "perpetual" else None
        liquidation_price = None
        if liquidation not in (None, "") and _decimal(liquidation, "強平價", positive=False) != 0:
            liquidation_price = str(_decimal(liquidation, "強平價"))
        entry_time = None
        if kind == "standard":
            try:
                entry_time = datetime.fromtimestamp(int(row["time"]) / 1000, UTC).isoformat()
            except (OverflowError, OSError, ValueError) as exc:
                raise BingXError("BingX 標準合約開倉時間格式異常") from exc
        result.append({
            "external_position_id": external_id,
            "exchange_symbol": symbol,
            "contract_type": kind,
            "market_id": market["id"],
            "side": side,
            "leverage": int(leverage),
            "margin_mode": "isolated" if isolated else "cross",
            "entry_price": str(_decimal(row.get("avgPrice" if kind == "perpetual" else "entryPrice"), "平均進場價")),
            "quantity": str(abs(amount)),
            "exchange_liquidation_price": liquidation_price,
            "entry_time": entry_time,
        })
    return result, skipped
