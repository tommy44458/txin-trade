"""Claude through the Anthropic API, with the user's own API key.

This is the route Anthropic's terms allow for products built on Claude: an API
key from the Claude Console, billed per use to the user's own account. The key
is stored with the other integration credentials and never returned to the
interface. Analysis runs the same registered Python tools as the CLI providers.
"""

import json
from collections.abc import Callable
from time import monotonic

import anthropic
from fastapi import APIRouter, HTTPException

from .local_settings import integration_credentials

router = APIRouter(prefix="/api/v1/auth/anthropic", tags=["Anthropic API authorization"])

DEFAULT_MODEL = "claude-opus-5-5"
WEB_SEARCH_USES = 3
# Models that take the dynamic-filtering web search; older ones take the basic tool.
_CURRENT_SEARCH = ("claude-opus-5", "claude-opus-4-6", "claude-opus-4-7", "claude-opus-4-8",
                   "claude-sonnet-5", "claude-sonnet-4-6", "claude-fable", "claude-mythos")


def search_tool(model: str) -> dict:
    """Anthropic's server-side web search, capped per analysis."""
    version = "web_search_20260209" if model.startswith(_CURRENT_SEARCH) else "web_search_20250305"
    return {"type": version, "name": "web_search", "max_uses": WEB_SEARCH_USES}
# On a policy decline the API re-runs the request on a fallback model it picks.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_OUTPUT_TOKENS = 64000
MODEL_CATALOG = (
    {"id": "claude-opus-5-5", "model": "claude-opus-5-5", "displayName": "Claude Opus 5.5", "isDefault": True},
    {"id": "claude-sonnet-5-5", "model": "claude-sonnet-5-5", "displayName": "Claude Sonnet 5.5", "isDefault": False},
    {"id": "claude-haiku-4-5", "model": "claude-haiku-4-5", "displayName": "Claude Haiku 4.5", "isDefault": False},
)
KEY_HINT = "請在「AI 分析帳號」輸入並儲存 Anthropic API 金鑰。"


class AnthropicApiError(RuntimeError):
    """Credential-free operational error suitable for display in the app."""


def api_key() -> str | None:
    saved = integration_credentials("anthropic")
    key = saved.get("api_key") if saved else None
    return key if isinstance(key, str) and key.strip() else None


def _message(exc: Exception) -> str:
    """Map SDK errors to plain guidance; provider text and request data stay out."""
    if isinstance(exc, anthropic.AuthenticationError):
        return "Anthropic API 金鑰無效或已撤銷，請到設定重新輸入。"
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "這把 Anthropic API 金鑰沒有使用此模型的權限。"
    if isinstance(exc, anthropic.NotFoundError):
        return "找不到所選的 Claude 模型，請到設定改選其他模型。"
    if isinstance(exc, anthropic.RateLimitError):
        return "Anthropic API 用量已達上限，請稍後重試或檢查帳戶額度。"
    if isinstance(exc, anthropic.BadRequestError):
        return "Anthropic API 拒絕了這次請求，請檢查帳戶額度與模型設定後重試。"
    if isinstance(exc, anthropic.APITimeoutError):
        return "Anthropic API 回應逾時，請稍後重試。"
    if isinstance(exc, anthropic.APIConnectionError):
        return "無法連線到 Anthropic API，請檢查網路後重試。"
    if isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500:
        return "Anthropic API 暫時無法使用，請稍後重試。"
    return "Anthropic API 分析未完成，請稍後重試。"


def _tool(schema: dict) -> dict:
    """An OpenAI-style function schema as an Anthropic client tool."""
    return {"name": schema["name"], "description": schema.get("description", ""),
            "input_schema": schema["parameters"], "eager_input_streaming": True}


def _usage(total: dict, usage) -> None:
    if usage is None:
        return
    inputs = [getattr(usage, key, None) for key in
              ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
    total["inputTokens"] += sum(value for value in inputs if isinstance(value, int))
    output = getattr(usage, "output_tokens", None)
    total["outputTokens"] += output if isinstance(output, int) else 0


class AnthropicApiSession:
    def __init__(self, key: str, *, client_factory=anthropic.Anthropic):
        # The loop below owns the deadline; one SDK retry covers a dropped connection.
        self.client = client_factory(api_key=key, max_retries=1)

    def analyze(self, instructions: str, context: str, model: str,
                tools: list[dict], tool_handler, *, timeout: float,
                effort: str = "medium", response_format: str = "json",
                on_text: Callable[[str], None] | None = None, web_search: bool = False) -> dict:
        if response_format not in {"json", "text"}:
            raise ValueError("Unsupported Anthropic response format")
        if response_format == "text" and (tools or tool_handler is not None):
            raise ValueError("Plain text discussions cannot expose model tools")
        system = f"{instructions}\n\n" + (
            "Only use the supplied trading evidence and registered tools. Return the report as JSON."
            if response_format == "json" else
            "Discuss only the supplied frozen trading evidence and conversation in readable prose "
            "using the response language specified by the task instructions. Return a "
            "conversational answer, not a JSON report, and never execute trades.")
        deadline = monotonic() + timeout
        messages: list = [{"role": "user", "content": context}]
        declared = [_tool(item) for item in tools]
        if web_search and response_format == "json":
            declared.append(search_tool(model or DEFAULT_MODEL))
        options = {"tools": declared} if declared else {}
        usage = {"inputTokens": 0, "outputTokens": 0}
        unparsed = 0
        # Up to four optional tool calls, then the report.
        for _turn in range(6):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Anthropic API 分析逾時")
            text = ""
            try:
                with self.client.beta.messages.stream(
                    model=model or DEFAULT_MODEL, max_tokens=MAX_OUTPUT_TOKENS, system=system,
                    messages=messages, output_config={"effort": effort},
                    betas=[FALLBACK_BETA], fallbacks="default", timeout=remaining, **options,
                ) as stream:
                    for event in stream:
                        if monotonic() >= deadline:
                            raise TimeoutError("Anthropic API 分析逾時")
                        if event.type == "text" and on_text is not None:
                            text += event.text
                            on_text(text)
                    final = stream.get_final_message()
                unparsed = 0
            except ValueError:
                # A tool input the SDK could not parse at all: re-issue the turn.
                unparsed += 1
                if unparsed > 2:
                    raise AnthropicApiError("Claude 回傳的工具參數無法解析，請重試。") from None
                continue
            except anthropic.AnthropicError as exc:
                raise AnthropicApiError(_message(exc)) from exc
            _usage(usage, final.usage)
            if final.stop_reason == "refusal":
                raise AnthropicApiError("Claude 這次拒絕回答，請調整後重試。")
            if final.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": final.content})
                continue
            tool_uses = [block for block in final.content if block.type == "tool_use"]
            if not tool_uses:
                output = "".join(block.text for block in final.content if block.type == "text")
                if not output.strip():
                    raise AnthropicApiError("Claude 沒有回傳分析內容。")
                if on_text is not None and output != text:
                    on_text(output)
                return {"text": output, "diagnostics": {"provider": "anthropic", "model": final.model},
                        "usage": usage}
            if final.stop_reason == "max_tokens" or tool_handler is None:
                raise AnthropicApiError("Claude 的工具呼叫不完整，請重試。")
            messages.append({"role": "assistant", "content": final.content})
            results = []
            for block in tool_uses:
                try:
                    if not isinstance(block.input, dict):
                        raise TypeError("Tool input is not an object")
                    output = tool_handler(block.name, block.input)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": json.dumps(output, ensure_ascii=False, separators=(",", ":"))})
                except (ValueError, TypeError, KeyError) as exc:
                    # An optional tool failure is unavailable evidence, not a failed report.
                    results.append({"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                    "content": json.dumps({"status": "unavailable", "reason": type(exc).__name__,
                                        "message": "Use the precomputed evidence and finish the report without this optional tool."})})
            messages.append({"role": "user", "content": results})
        raise AnthropicApiError("Claude 呼叫工具的次數超過上限。")

    def close(self):
        self.client.close()


def status() -> dict:
    configured = api_key() is not None
    return {"authenticated": configured, "available": True,
            "auth_source": "anthropic_api_key" if configured else None,
            "login_pending": False, "status_known": True}


def _live_status(client_factory=anthropic.Anthropic) -> dict:
    key = api_key()
    if key is None:
        return status() | {"error": KEY_HINT}
    client = client_factory(api_key=key, max_retries=0, timeout=15)
    try:
        client.models.list(limit=1)
    except anthropic.AnthropicError as exc:
        return status() | {"authenticated": False, "available": not isinstance(exc, anthropic.APIConnectionError),
                           "error": _message(exc)}
    finally:
        client.close()
    return status()


def require_authorized() -> str:
    key = api_key()
    if key is None:
        raise AnthropicApiError(KEY_HINT)
    return key


@router.get("")
@router.get("/status")
def get_status():
    return status()


@router.post("/check")
def check_status():
    return _live_status()


@router.post("/login")
def login():
    # There is no sign-in flow: the key is saved in Settings like other API keys.
    return {"auth_url": None, "status": _live_status()}


@router.post("/cancel")
def cancel():
    return status()


@router.post("/logout")
def logout():
    # Removing the key happens in Settings; nothing else is held for this provider.
    return status()


@router.post("/models")
def models():
    if api_key() is None:
        raise HTTPException(503, KEY_HINT)
    return cached_models()


@router.get("/models")
def cached_models():
    return {"data": [dict(item) for item in MODEL_CATALOG], "nextCursor": None}
