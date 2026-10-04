"""Key check and model catalog for the OpenAI API provider (the user's own API key).

Analysis with this provider already runs through ModelSession's Responses API
client; this module only lets Settings confirm the saved key works and choose a
model the key can use. The key never leaves the credential store.
"""

import re

import openai
from fastapi import APIRouter, HTTPException

from .auth_metadata import read_metadata, write_metadata
from .local_settings import integration_credentials

router = APIRouter(prefix="/api/v1/auth/openai", tags=["OpenAI API authorization"])

_METADATA = "openai_api"
KEY_HINT = "請在「AI 分析帳號」輸入並儲存 OpenAI API 金鑰。"
# Text models for the Responses API; audio, image, embedding and moderation models are left out.
_TEXT_MODEL = re.compile(r"^(gpt-|o\d)")
_NOT_TEXT = re.compile(r"(audio|realtime|transcribe|tts|image|embedding|moderation|search|dall-e|whisper|codex-mini)")


def api_key() -> str | None:
    saved = integration_credentials("openai")
    key = saved.get("api_key") if saved else None
    return key if isinstance(key, str) and key.strip() else None


def _message(exc: Exception) -> str:
    if isinstance(exc, openai.AuthenticationError):
        return "OpenAI API 金鑰無效或已撤銷，請到設定重新輸入。"
    if isinstance(exc, openai.PermissionDeniedError):
        return "這把 OpenAI API 金鑰沒有讀取模型的權限。"
    if isinstance(exc, openai.RateLimitError):
        return "OpenAI API 用量已達上限，請稍後重試或檢查帳戶額度。"
    if isinstance(exc, openai.APIConnectionError):
        return "無法連線到 OpenAI API，請檢查網路後重試。"
    return "OpenAI API 暫時無法使用，請稍後重試。"


def status() -> dict:
    configured = api_key() is not None
    return {"authenticated": configured, "available": True,
            "auth_source": "openai_api_key" if configured else None,
            "login_pending": False, "status_known": True}


def _catalog(client) -> list[dict]:
    ids = sorted({item.id for item in client.models.list()
                  if _TEXT_MODEL.match(item.id) and not _NOT_TEXT.search(item.id)}, reverse=True)
    return [{"id": model, "model": model, "displayName": model, "isDefault": False} for model in ids]


def _live(client_factory=openai.OpenAI) -> tuple[dict, list[dict] | None]:
    key = api_key()
    if key is None:
        return status() | {"error": KEY_HINT}, None
    client = client_factory(api_key=key, timeout=15, max_retries=0)
    try:
        catalog = _catalog(client)
    except openai.OpenAIError as exc:
        return status() | {"authenticated": False,
                           "available": not isinstance(exc, openai.APIConnectionError),
                           "error": _message(exc)}, None
    finally:
        client.close()
    write_metadata(_METADATA, {**read_metadata(_METADATA), "models": catalog})
    return status(), catalog


def default_model() -> str | None:
    """The model to use when none was chosen: the first saved catalog entry."""
    saved = read_metadata(_METADATA).get("models") or []
    first = saved[0] if saved and isinstance(saved[0], dict) else None
    return (first.get("model") or first.get("id")) if first else None


@router.get("")
@router.get("/status")
def get_status():
    return status()


@router.post("/check")
def check_status():
    return _live()[0]


@router.post("/login")
def login():
    # No sign-in flow: the key is saved in Settings and checked here.
    return {"auth_url": None, "status": _live()[0]}


@router.post("/cancel")
def cancel():
    return status()


@router.post("/logout")
def logout():
    return status()


@router.post("/models")
def models():
    result, catalog = _live()
    if catalog is None:
        raise HTTPException(503, result.get("error") or KEY_HINT)
    return {"data": catalog, "nextCursor": None}


@router.get("/models")
def cached_models():
    return {"data": read_metadata(_METADATA).get("models", []), "nextCursor": None}
