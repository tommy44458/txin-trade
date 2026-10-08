"""Local SQLite preferences and on-demand encrypted integration credentials."""

import json
import os
from pathlib import Path
from threading import RLock
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .config import local_user_id
from .credential_store import (
    CredentialStoreError,
    credential_needs_reentry,
    credential_status,
    delete_credentials,
    load_credentials,
    load_credentials_with_revision,
    save_credentials,
)
from .db import connect, database_path, init_db, utc_now
from .indicator_preferences import (
    InitialIndicatorName,
    initial_indicator_catalog,
    normalize_initial_indicators,
)
from .market_catalog import valid_market_id_format
from .models import OutputLocale, Timeframe

router = APIRouter(prefix="/api/v1/settings", tags=["Local settings"])
_PREFERENCES_LOCK = RLock()
_INITIALIZED_DATABASES: set[tuple[int, str]] = set()
_INITIALIZED_PREFERENCES: set[tuple[int, str, str]] = set()
INTEGRATION_NAMES = ("bingx", "binance", "jev", "jblanked", "openai", "anthropic")
_API_KEY_ENVIRONMENT = {"jev": "TYPESAFE_API_KEY", "jblanked": "JBLANKED_API_KEY", "openai": "OPENAI_API_KEY",
                        "anthropic": "ANTHROPIC_API_KEY"}
UiTheme = Literal["system", "light", "dark"]
ModelProvider = Literal["claude_code", "codex", "openai", "anthropic", "chatgpt_plan"]
MODEL_PROVIDERS = frozenset({"claude_code", "codex", "openai", "anthropic", "chatgpt_plan"})


def desktop_mode() -> bool:
    return os.getenv("APP_DESKTOP") == "1"


def data_directory() -> Path:
    directory = os.getenv("APP_DATA_DIR")
    return (Path(directory).expanduser() if directory else
            Path.home() / "Library" / "Application Support" / "txinTrade")


def _ensure_database() -> None:
    """Initialize a new local file once per process, including isolated tests."""
    path = database_path()
    key = (os.getpid(), str(path))
    with _PREFERENCES_LOCK:
        if key not in _INITIALIZED_DATABASES or not path.exists():
            init_db()
            _INITIALIZED_DATABASES.add(key)
            _INITIALIZED_PREFERENCES.difference_update(
                item for item in list(_INITIALIZED_PREFERENCES) if item[:2] == key)


def _nonsecret_object(value: dict) -> dict:
    """Do not import misplaced tokens or API secrets from an old metadata file."""
    sensitive = {"apikey", "apisecret", "accesstoken", "refreshtoken", "idtoken",
                 "password", "passwords", "secret", "token", "tokens", "clientsecret",
                 "authorization", "apitoken", "privatekey", "secretkey", "bingxapikey",
                 "bingxapisecret", "binanceapikey", "binanceapisecret", "binancesecretkey",
                 "jevapikey", "typesafeapikey", "jblankedapikey", "openaiapikey", "anthropicapikey"}

    def clean(item):
        if isinstance(item, dict):
            return {key: clean(child) for key, child in item.items()
                    if "".join(character for character in str(key).casefold()
                               if character.isalnum()) not in sensitive}
        if isinstance(item, list):
            return [clean(child) for child in item]
        return item

    return clean(value)


def _legacy_preferences() -> dict:
    try:
        data = json.loads((data_directory() / "settings.json").read_text())
        return _nonsecret_object(data) if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _ensure_preferences_store() -> None:
    _ensure_database()
    key = (os.getpid(), str(database_path()), local_user_id())
    with _PREFERENCES_LOCK:
        if key not in _INITIALIZED_PREFERENCES:
            with connect() as db:
                row = db.execute("SELECT value_json FROM app_preferences WHERE user_id=?",
                                 (local_user_id(),)).fetchone()
                if row is None:
                    db.execute("INSERT INTO app_preferences(user_id,value_json,updated_at) VALUES (?,?,?)",
                               (local_user_id(), json.dumps(_legacy_preferences(), ensure_ascii=False), utc_now()))
            _INITIALIZED_PREFERENCES.add(key)


def _read_preferences(db) -> dict:
    row = db.execute("SELECT value_json FROM app_preferences WHERE user_id=?",
                     (local_user_id(),)).fetchone()
    value = json.loads(row["value_json"]) if row else {}
    return value if isinstance(value, dict) else {}


def _save_preferences(db, value: dict) -> None:
    db.execute("""INSERT INTO app_preferences(user_id,value_json,updated_at) VALUES (?,?,?)
                  ON CONFLICT(user_id) DO UPDATE SET value_json=excluded.value_json,
                  updated_at=excluded.updated_at""",
               (local_user_id(), json.dumps(value, ensure_ascii=False), utc_now()))


def web_search_allowed() -> bool:
    """Whether the analysis model may search the web: on unless turned off in Settings.

    TRADE_WEB_SEARCH_ENABLED=0 turns it off for replays and tests, which must not see later news.
    """
    if os.getenv("TRADE_WEB_SEARCH_ENABLED", "1") == "0":
        return False
    return preferences().get("allow_web_search") is not False


def preferences() -> dict:
    _ensure_preferences_store()
    with connect(readonly=True) as db:
        return _read_preferences(db)


def save_preferences(value: dict) -> None:
    """Replace a complete document; partial writers must use patch_preferences."""
    _ensure_preferences_store()
    with connect() as db:
        _save_preferences(db, value)


def provider_configuration() -> tuple[str, str]:
    saved = preferences()
    provider = saved.get("model_provider", "codex" if desktop_mode() else "openai")
    if provider not in MODEL_PROVIDERS:
        # Includes the retired "chatgpt" provider: fall back to the default.
        provider = "codex" if desktop_mode() else "openai"
    model = saved.get("models", {}).get(provider)
    if model is None:
        model = os.getenv("OPENAI_MODEL", "") if provider == "openai" else ""
    return provider, str(model)


def patch_preferences(updates: dict | None = None, *, remove: tuple[str, ...] = ()) -> dict:
    """Merge a preference change without overwriting a concurrent settings update."""
    _ensure_preferences_store()
    with connect() as db:
        saved = _read_preferences(db)
        for key in remove:
            saved.pop(key, None)
        saved.update(updates or {})
        _save_preferences(db, saved)
        return saved


def favorite_market_ids() -> list[str]:
    saved = preferences().get("favorite_market_ids", [])
    return list(dict.fromkeys(item for item in saved if isinstance(item, str))) if isinstance(saved, list) else []


def initial_indicators(db=None) -> list[InitialIndicatorName]:
    """Read the selection in the caller's transaction when creating a job."""
    saved = _read_preferences(db) if db is not None else preferences()
    return normalize_initial_indicators(saved.get("initial_indicators", []))


def integration_status(name: str) -> dict:
    """Public configuration flags from local metadata, without decrypting keys."""
    if name not in INTEGRATION_NAMES:
        return {"configured": False}
    saved = preferences()
    managed = name in saved.get("managed_integrations", [])
    if desktop_mode() or managed:
        state = saved.get("integration_status", {}).get(name)
        has_state = isinstance(state, dict) and isinstance(state.get("configured"), bool)
        if credential_status(name):
            return {"configured": state["configured"] if has_state else True}
        if credential_needs_reentry(name) or (state["configured"] if has_state else managed):
            return {"configured": False, "needs_reentry": True, "needs_migration": True}
        return {"configured": False}
    if name == "bingx":
        return {"configured": bool(os.getenv("BINGX_API_KEY") and os.getenv("BINGX_API_SECRET"))}
    if name in _API_KEY_ENVIRONMENT:
        return {"configured": bool(os.getenv(_API_KEY_ENVIRONMENT[name]))}
    return {"configured": False}


def _record_integration_status(saved: dict, name: str, configured: bool) -> None:
    saved.setdefault("integration_status", {})[name] = {
        "configured": configured, "revision": uuid4().hex,
    }


def mark_integrations_configured(updates: dict[str, bool]) -> None:
    """Commit verified migration flags without replacing concurrent preferences."""
    if any(name not in INTEGRATION_NAMES or not isinstance(value, bool)
           for name, value in updates.items()):
        raise ValueError("Invalid integration configuration")
    _ensure_preferences_store()
    with connect() as db:
        saved = _read_preferences(db)
        managed = set(saved.get("managed_integrations", []))
        for name, configured in updates.items():
            managed.add(name)
            _record_integration_status(saved, name, configured)
        saved["managed_integrations"] = sorted(managed)
        _save_preferences(db, saved)


def integration_credentials(name: str, *, allow_interaction: bool = False) -> dict | None:
    if name not in INTEGRATION_NAMES:
        return None
    if desktop_mode() or name in preferences().get("managed_integrations", []):
        state = integration_status(name)
        if not state["configured"]:
            return None
        saved = load_credentials(name)
        return None if not saved or saved.get("disabled") else saved
    # Desktop integrations are explicitly opt-in in Settings. A developer's .env
    # must not enable optional remote services or resurrect a removed key.
    if desktop_mode():
        return None
    if name == "bingx":
        key, secret = os.getenv("BINGX_API_KEY"), os.getenv("BINGX_API_SECRET")
        return {"api_key": key, "api_secret": secret} if key and secret else None
    if name in _API_KEY_ENVIRONMENT:
        key = os.getenv(_API_KEY_ENVIRONMENT[name])
        return {"api_key": key} if key else None
    return None


def public_settings() -> dict:
    provider, model = provider_configuration()
    return {"model_provider": provider, "model": model, "desktop": desktop_mode(),
            "ui_locale": ui_locale(),
            "ui_theme": ui_theme(),
            "favorite_market_ids": favorite_market_ids(),
            "trading_preferences": trading_preferences(),
            "initial_indicators": initial_indicators(),
            "initial_indicator_catalog": initial_indicator_catalog(),
            "share_outcomes": preferences().get("share_outcomes") is True,
            "allow_web_search": preferences().get("allow_web_search") is not False,
            "integrations": {name: integration_status(name)
                             for name in INTEGRATION_NAMES}}


def binance_sync_credentials() -> tuple[dict, str]:
    """Freeze keys and their account identity for one explicitly requested read."""
    with _PREFERENCES_LOCK:
        _ensure_preferences_store()
        saved, revision = load_credentials_with_revision("binance")
        scope = saved.get("account_scope") if saved else None
        metadata = preferences()
        if (not saved or not saved.get("api_key") or not saved.get("api_secret")
                or saved.get("disabled") or not revision):
            raise CredentialStoreError("請先在設定輸入 Binance API Key 與 Secret")
        if (not isinstance(scope, str) or len(scope) != 32
                or metadata.get("binance_account_scope") != scope):
            raise CredentialStoreError("Binance 連線已變更；請重新儲存金鑰後再同步持倉")
        return {**saved, "_credential_revision": revision}, scope


def binance_connection_is_current(db, credentials: dict, scope: str) -> bool:
    """Check the frozen connection inside the existing writer transaction.

    No initialization or credential decryption here: the writer lock serializes
    this check and the position update with credential replacement/removal.
    """
    row = db.execute("SELECT updated_at FROM credential_records WHERE name='binance'").fetchone()
    return bool(row and row["updated_at"] == credentials.get("_credential_revision")
                and _read_preferences(db).get("binance_account_scope") == scope
                and credentials.get("account_scope") == scope)


def ui_locale() -> OutputLocale:
    value = preferences().get("ui_locale", "zh-TW")
    return value if isinstance(value, str) and value in {"zh-TW", "en-US"} else "zh-TW"


def ui_theme() -> UiTheme:
    value = preferences().get("ui_theme", "system")
    return value if isinstance(value, str) and value in {"system", "light", "dark"} else "system"


class TradingPreferencesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    directional_bias: Literal["bullish", "bearish"] | None = None
    risk_tolerance: Literal["high", "medium", "low"] | None = None
    trading_style: Literal["left", "right"] | None = None
    timeframe: Timeframe = "1h"
    leverage: int = Field(default=5, ge=1, le=125)
    market_id: str | None = Field(default=None, max_length=80)

    @field_validator("market_id")
    @classmethod
    def validate_market(cls, value: str | None) -> str | None:
        if value is not None and not valid_market_id_format(value):
            raise ValueError("交易對格式不正確")
        return value


def trading_preferences() -> dict:
    defaults = TradingPreferencesUpdate().model_dump()
    saved = preferences().get("trading_preferences", {})
    if not isinstance(saved, dict):
        return defaults
    # Legacy metadata must not turn a missing preference into an aggressive one.
    for key, value in saved.items():
        if key in defaults:
            try:
                validated = TradingPreferencesUpdate.model_validate({key: value})
            except ValueError:
                continue
            defaults[key] = getattr(validated, key)
    return defaults


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_provider: ModelProvider | None = None
    model: str | None = Field(default=None, max_length=120)
    ui_locale: OutputLocale | None = None
    ui_theme: UiTheme | None = None
    favorite_market_ids: list[str] | None = Field(default=None, max_length=1000)
    trading_preferences: TradingPreferencesUpdate | None = None
    initial_indicators: list[InitialIndicatorName] | None = Field(default=None, max_length=100)
    # Opt-in sharing of reconciled outcomes with the txinTrade cloud; never set remotely.
    share_outcomes: bool | None = None
    # Lets the analysis model search the web for recent news; on by default, never set remotely.
    allow_web_search: bool | None = None
    bingx_api_key: SecretStr | None = None
    bingx_api_secret: SecretStr | None = None
    binance_api_key: SecretStr | None = None
    binance_api_secret: SecretStr | None = None
    jev_api_key: SecretStr | None = None
    jblanked_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    clear_bingx: bool = False
    clear_binance: bool = False
    clear_jev: bool = False
    clear_jblanked: bool = False
    clear_openai: bool = False
    clear_anthropic: bool = False

    @field_validator("initial_indicators")
    @classmethod
    def validate_initial_indicators(cls, value):
        return normalize_initial_indicators(value) if value is not None else None

    @field_validator("favorite_market_ids")
    @classmethod
    def validate_favorites(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        for market_id in value:
            if not valid_market_id_format(market_id):
                raise ValueError("最愛交易對格式不正確")
        # Preserve user ordering and unavailable favorites while the catalog is offline.
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def validate_keys(self):
        for provider, label in (("bingx", "BingX"), ("binance", "Binance")):
            key, secret = getattr(self, f"{provider}_api_key"), getattr(self, f"{provider}_api_secret")
            has_key = bool(key and key.get_secret_value().strip())
            has_secret = bool(secret and secret.get_secret_value().strip())
            if has_key != has_secret:
                raise ValueError(f"請一起輸入 {label} API Key 與 Secret")
            if getattr(self, f"clear_{provider}") and has_key:
                raise ValueError(f"清除與更新 {label} 金鑰不能同時進行")
            if provider == "binance" and has_key:
                for value in (key, secret):
                    text = value.get_secret_value().strip()
                    if (not text.isascii() or not text.isprintable()
                            or any(character.isspace() for character in text)):
                        raise ValueError("Binance 金鑰不可包含非 ASCII 字元、空白或控制字元")
        for name, label in (("jev", "Jev"), ("jblanked", "JBlanked"), ("openai", "OpenAI"), ("anthropic", "Anthropic")):
            value = getattr(self, f"{name}_api_key")
            if getattr(self, f"clear_{name}") and value and value.get_secret_value().strip():
                raise ValueError(f"清除與更新 {label} 金鑰不能同時進行")
        for value in (self.bingx_api_key, self.bingx_api_secret,
                      self.binance_api_key, self.binance_api_secret,
                      self.jev_api_key, self.jblanked_api_key, self.openai_api_key,
                      self.anthropic_api_key):
            if value and len(value.get_secret_value()) > 4096:
                raise ValueError("金鑰長度超過限制")
        return self


@router.get("")
def get_settings():
    try:
        return public_settings()
    except CredentialStoreError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.put("")
@router.patch("")
def update_settings(body: SettingsUpdate):
    with _PREFERENCES_LOCK:
        return _write_settings(body)


def _write_settings(body: SettingsUpdate):
    try:
        # Credential and preference writes each use a short SQLite transaction;
        # encryption never opens an OS credential dialog.
        credential_updates = {}
        binance_scope = None
        if body.clear_binance:
            delete_credentials("binance")
            credential_updates["binance"] = False
        elif body.binance_api_key and body.binance_api_key.get_secret_value().strip():
            # The scope travels atomically with its encrypted key record. A
            # replacement can represent a different account, even with the same
            # symbol; never silently reconcile or close the old connection.
            binance_scope = uuid4().hex
            save_credentials("binance", {
                "api_key": body.binance_api_key.get_secret_value().strip(),
                "api_secret": body.binance_api_secret.get_secret_value().strip(),
                "account_scope": binance_scope,
            })
            credential_updates["binance"] = True
        if body.clear_bingx:
            delete_credentials("bingx")
            credential_updates["bingx"] = False
        elif body.bingx_api_key and body.bingx_api_key.get_secret_value().strip():
            save_credentials("bingx", {
                "api_key": body.bingx_api_key.get_secret_value().strip(),
                "api_secret": body.bingx_api_secret.get_secret_value().strip(),
            })
            credential_updates["bingx"] = True
        for name in ("jev", "jblanked", "openai", "anthropic"):
            key = getattr(body, f"{name}_api_key")
            if getattr(body, f"clear_{name}"):
                delete_credentials(name)
                credential_updates[name] = False
            elif key and key.get_secret_value().strip():
                save_credentials(name, {"api_key": key.get_secret_value().strip()})
                credential_updates[name] = True
        _ensure_preferences_store()
        with connect() as db:
            saved = _read_preferences(db)
            if body.clear_binance:
                saved.pop("binance_account_scope", None)
            elif binance_scope is not None:
                saved["binance_account_scope"] = binance_scope
            managed = set(saved.get("managed_integrations", []))
            for name, configured in credential_updates.items():
                managed.add(name)
                _record_integration_status(saved, name, configured)
            if managed:
                saved["managed_integrations"] = sorted(managed)
            if body.model_provider is not None:
                saved["model_provider"] = body.model_provider
            if body.ui_locale is not None:
                saved["ui_locale"] = body.ui_locale
            if body.ui_theme is not None:
                saved["ui_theme"] = body.ui_theme
            if body.model is not None:
                provider = saved.get("model_provider", "codex" if desktop_mode() else "openai")
                saved.setdefault("models", {})[provider] = body.model.strip()
            if body.favorite_market_ids is not None:
                saved["favorite_market_ids"] = body.favorite_market_ids
            if body.trading_preferences is not None:
                target = saved.setdefault("trading_preferences", {})
                target.update(body.trading_preferences.model_dump(exclude_unset=True))
            if body.initial_indicators is not None:
                saved["initial_indicators"] = body.initial_indicators
            if body.share_outcomes is not None:
                saved["share_outcomes"] = body.share_outcomes
            if body.allow_web_search is not None:
                saved["allow_web_search"] = body.allow_web_search
            _save_preferences(db, saved)
        return public_settings()
    except CredentialStoreError as exc:
        raise HTTPException(503, str(exc)) from exc
