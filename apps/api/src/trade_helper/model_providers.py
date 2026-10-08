"""Model transport adapters; market evidence and strategy prompts stay shared."""

import os
import queue
from collections.abc import Callable
from math import isfinite
from threading import Event, Thread
from time import monotonic
from types import SimpleNamespace

from . import anthropic_api_bridge, chatgpt_plan_auth, claude_code_bridge, openai_api_bridge
from .codex_bridge import CodexRpc, require_authorized
from .codex_bridge import models as codex_models
from .local_settings import integration_credentials, provider_configuration


class ModelProviderError(RuntimeError):
    """Safe model setup/transport explanation, never raw upstream responses."""


STREAM_QUEUE_SIZE = 32
STREAM_QUEUE_PUT_SECONDS = 0.05
TRANSPORT_CLOSE_WAIT_SECONDS = 0.05


def _close_transport(resource, *, deadline: float | None = None) -> None:
    """Do not let socket/client cleanup hold a completed or timed-out worker."""
    if resource is None or not hasattr(resource, "close"):
        return

    def close():
        try:
            resource.close()
        except Exception:  # noqa: BLE001, S110 -- cleanup must not expose provider data
            pass

    cleanup = Thread(target=close, name="ath-transport-close", daemon=True)
    cleanup.start()
    wait = (TRANSPORT_CLOSE_WAIT_SECONDS if deadline is None else
            max(0, min(TRANSPORT_CLOSE_WAIT_SECONDS, deadline - monotonic())))
    if wait:
        cleanup.join(timeout=wait)


class _BoundedResponseReader:
    """One reader owns the blocking iterator; only the caller delivers text."""

    def __init__(self, stream, deadline: float):
        self.stream, self.deadline = stream, deadline
        self.events = queue.Queue(maxsize=STREAM_QUEUE_SIZE)
        self.stopped = Event()
        self.thread = Thread(target=self._read, name="ath-response-reader", daemon=True)
        self.thread.start()

    def _put(self, kind: str, value=None) -> bool:
        while not self.stopped.is_set():
            remaining = self.deadline - monotonic()
            if remaining <= 0:
                return False
            try:
                self.events.put((kind, value), timeout=min(STREAM_QUEUE_PUT_SECONDS, remaining))
                return True
            except queue.Full:
                continue
        return False

    def _read(self) -> None:
        try:
            if self.stopped.is_set() or monotonic() >= self.deadline:
                return
            iterator = iter(self.stream)
            while not self.stopped.is_set() and monotonic() < self.deadline:
                try:
                    event = next(iterator)
                except StopIteration:
                    return
                if not self._put("event", event):
                    return
                # Never ask a completed response's iterator for more data. It
                # may block on EOF or fail after its authoritative completion.
                if getattr(event, "type", None) == "response.completed":
                    return
        except Exception as exc:  # noqa: BLE001 -- raised on the caller; no callbacks from this thread
            self._put("error", exc)
        finally:
            self._put("end")

    def __iter__(self):
        while True:
            remaining = self.deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("模型回應逾時")
            try:
                kind, value = self.events.get(timeout=remaining if isfinite(remaining) else None)
            except queue.Empty:
                raise TimeoutError("模型回應逾時") from None
            if monotonic() >= self.deadline:
                raise TimeoutError("模型回應逾時")
            if kind == "error":
                raise value
            if kind == "end":
                return
            yield value

    def close(self) -> None:
        self.stopped.set()
        _close_transport(self.stream, deadline=self.deadline)


def _visible_response_text(response) -> str:
    output = getattr(response, "output", []) or []
    if not output:
        return getattr(response, "output_text", "") or ""
    return "".join(
        getattr(part, "text", "") for item in output
        if getattr(item, "type", None) == "message"
        and getattr(item, "phase", None) in {None, "final_answer"}
        for part in getattr(item, "content", [])
        if getattr(part, "type", None) == "output_text"
    )


def _read_response_stream(stream, *, deadline: float,
                          on_text: Callable[[str], None] | None = None):
    """Read genuine response deltas; callbacks replace the complete visible prefix."""
    text_parts, phases = {}, {}
    last_emitted = ""

    def emit(text):
        nonlocal last_emitted
        if on_text is not None and text != last_emitted:
            if monotonic() >= deadline:
                raise TimeoutError("模型回應逾時")
            on_text(text)
            if monotonic() >= deadline:
                raise TimeoutError("模型回應逾時")
            last_emitted = text

    reader = _BoundedResponseReader(stream, deadline)
    try:
        for event in reader:
            if event.type in {"error", "response.failed", "response.incomplete"}:
                raise ModelProviderError("模型回應未完成，請檢查授權與帳號額度後重試。")
            if event.type == "response.output_item.added":
                item = event.item
                phases[getattr(item, "id", None)] = (
                    getattr(item, "phase", None) if getattr(item, "type", None) == "message"
                    else "hidden"
                )
            elif event.type in {"response.output_text.delta", "response.output_text.done"}:
                item_id = getattr(event, "item_id", None)
                key = (getattr(event, "output_index", 0), getattr(event, "content_index", 0), item_id)
                if event.type == "response.output_text.delta":
                    text_parts[key] = text_parts.get(key, "") + event.delta
                else:
                    text_parts[key] = event.text
                emit("".join(text_parts[key] for key in sorted(text_parts, key=lambda k: k[:2])
                             if phases.get(key[2]) in {None, "final_answer"}))
            elif event.type == "response.completed":
                completed = event.response
                if getattr(completed, "status", "completed") not in {None, "completed"}:
                    raise ModelProviderError("模型回應未完成，請稍後重試。")
                if on_text is not None and (visible := _visible_response_text(completed)):
                    emit(visible)
                # Completion is authoritative; EOF cannot revoke the answer.
                if monotonic() >= deadline:
                    raise TimeoutError("模型回應逾時")
                return completed
        raise ModelProviderError("模型連線中斷，沒有收到完整回覆。")
    finally:
        reader.close()


def analysis_timeout_seconds() -> int:
    try:
        value = int(os.getenv("APP_ANALYSIS_TIMEOUT_SECONDS", "240"))
        if not 30 <= value <= 900:
            raise ValueError()
        return value
    except ValueError as exc:
        raise ModelProviderError("APP_ANALYSIS_TIMEOUT_SECONDS 必須介於 30 與 900 秒。") from exc


def codex_reasoning_effort() -> str:
    value = os.getenv("APP_CODEX_REASONING_EFFORT", "medium")
    if value not in {"minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
        raise ModelProviderError("APP_CODEX_REASONING_EFFORT 不是支援的推理強度。")
    return value


def claude_code_effort() -> str:
    value = os.getenv("APP_CLAUDE_CODE_EFFORT") or claude_code_bridge.effort_level(codex_reasoning_effort())
    if value not in {"low", "medium", "high", "xhigh", "max"}:
        raise ModelProviderError("APP_CLAUDE_CODE_EFFORT 不是支援的推理強度。")
    return value


def tool_name(name: str) -> str:
    # Namespaced tool names are accepted; existing Python dispatch is flat.
    return name.removeprefix("indicators.")


LOCAL_AGENT_PROVIDERS = frozenset({"codex", "claude_code", "anthropic", "chatgpt_plan"})


class ModelSession:
    def __init__(self, *, openai_factory):
        self.provider, self.model = provider_configuration()
        self.client = None
        self.isolated_codex = False
        if self.provider == "openai":
            credentials = integration_credentials("openai")
            key = credentials.get("api_key") if credentials else None
            # Without a chosen model, use the first one Settings found for this key.
            self.model = self.model or openai_api_bridge.default_model() or ""
            if not key or not self.model:
                # Retain the old dev-mode exception contract for existing callers.
                raise RuntimeError("OPENAI_API_KEY and OPENAI_MODEL are required for Agent analysis")
            self.client = openai_factory(api_key=key, timeout=90, max_retries=0)
        elif self.provider == "claude_code":
            # An empty model uses Claude Code's own default for the signed-in account.
            claude_code_bridge.require_authorized()
        elif self.provider == "anthropic":
            self.anthropic_key = anthropic_api_bridge.require_authorized()
            self.model = self.model or anthropic_api_bridge.DEFAULT_MODEL
        elif self.provider == "chatgpt_plan":
            # Access tokens last an hour; each analysis starts Codex with a current one.
            self.plan_token = chatgpt_plan_auth.require_authorized()
            if not self.model:
                saved = chatgpt_plan_auth.cached_models()["data"] or chatgpt_plan_auth.models()["data"]
                selected = next((item for item in saved if item.get("isDefault")), saved[0] if saved else None)
                if not selected:
                    raise ModelProviderError("ChatGPT 方案沒有可用模型。")
                self.model = selected.get("model") or selected["id"]
        else:
            self.isolated_codex = require_authorized()
            if not self.model:
                available = codex_models()["data"]
                selected = next((item for item in available if item.get("isDefault")),
                                available[0] if available else None)
                if not selected:
                    raise ModelProviderError("Codex 沒有可用模型。")
                self.model = selected.get("model") or selected["id"]

    @property
    def uses_local_agent(self) -> bool:
        return self.provider in LOCAL_AGENT_PROVIDERS

    def analyze_local_agent(self, *, instructions: str, context: str, tools: list[dict],
                            tool_handler, timeout: float, response_format: str = "json",
                            on_text: Callable[[str], None] | None = None, web_search: bool = False):
        deadline = monotonic() + timeout
        if self.provider == "claude_code":
            label, effort = "Claude Code", claude_code_effort()
            runner = claude_code_bridge.ClaudeCodeSession()
        elif self.provider == "anthropic":
            label, effort = "Claude", claude_code_effort()
            runner = anthropic_api_bridge.AnthropicApiSession(self.anthropic_key)
        elif self.provider == "chatgpt_plan":
            label, effort = "ChatGPT", codex_reasoning_effort()
            runner = CodexRpc(plan_token=self.plan_token)
        else:
            label, effort = "Codex", codex_reasoning_effort()
            runner = CodexRpc(isolated=self.isolated_codex)
        try:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError(f"{label} 模型連接逾時")
            options = {"response_format": response_format} if response_format != "json" else {}
            if on_text is not None:
                options["on_text"] = on_text
            if web_search:
                options["web_search"] = True
            result = runner.analyze(instructions, context, self.model, tools, tool_handler,
                                    timeout=remaining, effort=effort, **options)
            usage = result.get("usage") or {}
            return SimpleNamespace(output=[], output_text=result["text"],
                provider_diagnostics=result.get("diagnostics"),
                usage=SimpleNamespace(input_tokens=usage.get("inputTokens"),
                                      output_tokens=usage.get("outputTokens")))
        finally:
            runner.close()

    def stream_text(self, *, instructions: str, context: str, timeout: float,
                    on_text: Callable[[str], None] | None = None):
        if self.uses_local_agent:
            return self.analyze_local_agent(instructions=instructions, context=context,
                tools=[], tool_handler=None, timeout=timeout, response_format="text", on_text=on_text)
        deadline = monotonic() + timeout
        self._stream_deadline = deadline
        options = {"model": self.model, "instructions": instructions,
                   "input": [{"role": "user", "content": context}],
                   "timeout": timeout, "store": False, "stream": True}
        stream = self.client.responses.create(**options)
        response = _read_response_stream(stream, deadline=deadline, on_text=on_text)
        return SimpleNamespace(output=getattr(response, "output", []),
            output_text=_visible_response_text(response), status=getattr(response, "status", None),
            usage=getattr(response, "usage", None))

    def close(self):
        if self.client and hasattr(self.client, "close"):
            deadline = getattr(self, "_stream_deadline", None)
            _close_transport(self.client, deadline=deadline)
