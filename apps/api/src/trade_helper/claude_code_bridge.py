"""Bounded access to the user's locally installed Claude Code CLI.

The app never handles Claude credentials. Claude Code keeps its own sign-in
(`claude auth login`); this module only checks that status and starts short,
headless sessions. Every built-in tool, user setting, plugin, hook and MCP
server is disabled; only the application's registered Python tools are exposed
through an in-process MCP server carried over the CLI's stdio control channel.
"""

import json
import os
import queue
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from time import monotonic

from fastapi import APIRouter, HTTPException

from .auth_metadata import read_metadata, write_metadata
from .cli_paths import find_executable
from .local_settings import desktop_mode, patch_preferences, preferences
from .platform_process import NO_WINDOW, WINDOWS, launch_command, stop_process

router = APIRouter(prefix="/api/v1/auth/claude_code", tags=["Claude Code authorization"])

_METADATA = "claude_code"
_MCP_SERVER = "indicators"
_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
# Aliases always resolve to the newest model of each family in Claude Code.
MODEL_CATALOG = (
    {"id": "opus", "model": "opus", "displayName": "Claude Opus", "isDefault": False},
    {"id": "sonnet", "model": "sonnet", "displayName": "Claude Sonnet", "isDefault": False},
    {"id": "haiku", "model": "haiku", "displayName": "Claude Haiku", "isDefault": False},
)
LOGIN_HINT = "請先在終端機執行 claude auth login 登入 Claude Code，再回到設定按「連線 Claude Code」。"


class ClaudeCodeError(RuntimeError):
    """Credential-free operational error suitable for display in the app."""


class ClaudeCodeTimeoutError(ClaudeCodeError, TimeoutError):
    """A safe stage-specific timeout, without hidden reasoning or inputs."""


def _find_claude() -> str | None:
    return find_executable("claude", os.getenv("APP_CLAUDE_CODE_PATH"),
                           (str(Path.home() / ".claude/local/claude"),))


def cli_installed() -> bool:
    return _find_claude() is not None


def claude_executable() -> str:
    executable = _find_claude()
    if executable is None:
        raise ClaudeCodeError("找不到 Claude Code。請先安裝 Claude Code CLI。")
    return executable


def _environment() -> dict:
    environment = dict(os.environ)
    for key in list(environment):
        # Application secrets never reach the model process. Claude Code's own
        # configuration (for example ANTHROPIC_API_KEY) is left to the user.
        if (key.startswith(("OPENAI_", "BINGX_", "TYPESAFE_", "APP_DESKTOP_TOKEN"))
                or key in {"DATABASE_URL", "MIGRATION_DATABASE_URL", "JBLANKED_API_KEY",
                           "CODEX_API_KEY", "CLAUDECODE"}):
            environment.pop(key)
    return environment


def effort_level(value: str) -> str:
    return {"minimal": "low", "ultra": "max"}.get(value, value)


def _own_group() -> bool:
    # Outside the desktop app the CLI gets its own POSIX process group, so it can
    # be stopped with its children; Windows stops the process tree instead.
    return not desktop_mode() and not WINDOWS


class ClaudeCodeSession:
    """One headless Claude Code run: system prompt, one user turn, final text."""

    def __init__(self, *, process_factory=subprocess.Popen):
        self.process_factory = process_factory
        self.messages: queue.Queue = queue.Queue()
        self.started_at = monotonic()
        self.last_stage = "連接模型"
        self.event_counts: dict[str, int] = {}
        self.tools: dict[str, dict] = {}
        self.tool_handler = None
        self.process = None
        # A CLI child that Windows has not released yet must not fail the run.
        self.workspace = tempfile.TemporaryDirectory(prefix="ath-claude-", ignore_cleanup_errors=True)

    def _command(self, model: str, effort: str, prompt_file: Path, max_turns: int) -> list[str]:
        command = [*launch_command(claude_executable()), "-p", "--output-format", "stream-json", "--verbose",
                   "--input-format", "stream-json", "--include-partial-messages",
                   "--system-prompt-file", str(prompt_file), "--tools", "",
                   "--strict-mcp-config", "--permission-mode", "dontAsk",
                   "--setting-sources=", "--disable-slash-commands",
                   "--no-session-persistence", "--max-turns", str(max_turns)]
        if self.tools:
            command += ["--mcp-config", json.dumps(
                {"mcpServers": {_MCP_SERVER: {"type": "sdk", "name": _MCP_SERVER}}}),
                "--allowedTools", ",".join(f"mcp__{_MCP_SERVER}__{name}" for name in self.tools)]
        if model:
            command.append(f"--model={model}")
        if effort:
            command += ["--effort", effort]
        return command

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        self.messages.put(value)
                except ValueError:
                    continue
        finally:
            self.messages.put(None)

    def send(self, message: dict):
        try:
            self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise ClaudeCodeError("Claude Code 連線已中斷，請重新分析。") from exc

    def _next(self, deadline: float) -> dict:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise self._timeout()
        try:
            message = self.messages.get(timeout=remaining)
        except queue.Empty as exc:
            raise self._timeout() from exc
        if message is None:
            raise ClaudeCodeError("Claude Code 已停止，請確認登入狀態後重試。")
        kind = message.get("type", "unknown")
        self.event_counts[kind] = self.event_counts.get(kind, 0) + 1
        return message

    def _timeout(self) -> ClaudeCodeTimeoutError:
        elapsed = round(monotonic() - self.started_at)
        return ClaudeCodeTimeoutError(f"Claude Code 分析超過等待時間（{elapsed} 秒，最後階段：{self.last_stage}）。請重新分析，或在設定選擇其他模型。")

    def diagnostics(self) -> dict:
        return {"elapsed_ms": round((monotonic() - self.started_at) * 1000),
                "last_stage": self.last_stage,
                "notification_counts": dict(self.event_counts)}

    def _mcp(self, message: dict) -> dict:
        method, identifier = message.get("method"), message.get("id")
        if identifier is None:
            return {"jsonrpc": "2.0", "result": {}}  # notifications still need an ack
        if method == "initialize":
            result = {"protocolVersion": message.get("params", {}).get("protocolVersion", "2025-06-18"),
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": _MCP_SERVER, "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": item["name"], "description": item.get("description", ""),
                                 "inputSchema": item["parameters"]} for item in self.tools.values()]}
        elif method == "tools/call":
            params = message.get("params", {})
            self.last_stage = "計算補充指標"
            try:
                if params.get("name") not in self.tools or self.tool_handler is None:
                    raise KeyError("Unregistered tool")
                output = self.tool_handler(params["name"], params.get("arguments") or {})
                result = {"content": [{"type": "text", "text": json.dumps(
                    output, ensure_ascii=False, separators=(",", ":"))}]}
            except (ValueError, TypeError, KeyError) as exc:
                result = {"isError": True, "content": [{"type": "text", "text": json.dumps(
                    {"status": "unavailable", "reason": type(exc).__name__,
                     "message": "Use supplied evidence and finish without this optional tool."})}]}
        elif method == "ping":
            result = {}
        else:
            return {"jsonrpc": "2.0", "id": identifier,
                    "error": {"code": -32601, "message": "Method not found"}}
        return {"jsonrpc": "2.0", "id": identifier, "result": result}

    def _control_request(self, message: dict) -> None:
        request = message.get("request", {})
        identifier = message.get("request_id")
        if (request.get("subtype") == "mcp_message"
                and request.get("server_name") == _MCP_SERVER and isinstance(request.get("message"), dict)):
            self.send({"type": "control_response", "response": {
                "subtype": "success", "request_id": identifier,
                "response": {"mcp_response": self._mcp(request["message"])}}})
        elif request.get("subtype") == "can_use_tool":
            self.send({"type": "control_response", "response": {
                "subtype": "success", "request_id": identifier, "response": {
                    "behavior": "deny",
                    "message": "This application only supports its registered Python tools."}}})
        else:
            self.send({"type": "control_response", "response": {
                "subtype": "error", "request_id": identifier,
                "error": "Unsupported request"}})

    def analyze(self, instructions: str, context: str, model: str,
                tools: list[dict], tool_handler, *, timeout: float,
                effort: str = "medium", response_format: str = "json",
                on_text: Callable[[str], None] | None = None) -> dict:
        if response_format not in {"json", "text"}:
            raise ValueError("Unsupported Claude Code response format")
        if response_format == "text" and (tools or tool_handler is not None):
            raise ValueError("Plain text discussions cannot expose model tools")
        developer_instructions = (
            "Only use the supplied trading evidence and registered Python tools. "
            "Return the report as JSON. Do not use shell, files, plugins or web search."
            if response_format == "json" else
            "Discuss only the supplied frozen trading evidence and conversation in readable "
            "prose using the response language specified by the task instructions. "
            "Return a conversational answer. Do not return a JSON "
            "report or use tools, shell, files, plugins, web search, or execute trades."
        )
        deadline = monotonic() + timeout
        self.tools = {item["name"]: item for item in tools}
        self.tool_handler = tool_handler
        prompt_file = Path(self.workspace.name) / "system-prompt.txt"
        prompt_file.write_text(f"{instructions}\n\n{developer_instructions}", encoding="utf-8")
        # Six turns: up to four optional tool calls, then the report.
        command = self._command(model, effort, prompt_file, 6 if self.tools else 1)
        self.process = self.process_factory(
            command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1, env=_environment(),
            cwd=self.workspace.name, start_new_session=_own_group(), creationflags=NO_WINDOW)
        threading.Thread(target=self._read, daemon=True).start()
        self.send({"type": "control_request", "request_id": "ath-initialize",
                   "request": {"subtype": "initialize", "hooks": None}})
        initialized = False
        text, last_emitted = "", ""
        while True:
            message = self._next(deadline)
            kind = message.get("type")
            if kind == "control_request":
                self._control_request(message)
            elif kind == "control_response":
                response = message.get("response", {})
                if response.get("request_id") == "ath-initialize" and not initialized:
                    if response.get("subtype") != "success":
                        raise ClaudeCodeError("Claude Code 無法啟動分析，請確認版本與登入狀態。")
                    initialized = True
                    self.send({"type": "user", "session_id": "", "parent_tool_use_id": None,
                               "message": {"role": "user", "content": context}})
            elif kind == "stream_event":
                event = message.get("event", {})
                if event.get("type") == "message_start":
                    self.last_stage = "判斷行情"
                    text = ""  # text before a tool call is not the final answer
                elif (event.get("type") == "content_block_delta"
                      and event.get("delta", {}).get("type") == "text_delta"):
                    self.last_stage = "撰寫報告"
                    text += event["delta"].get("text", "")
                    if on_text is not None and text != last_emitted:
                        on_text(text)
                        last_emitted = text
            elif kind == "result":
                output = message.get("result")
                if message.get("is_error") or message.get("subtype") != "success":
                    raise ClaudeCodeError("Claude Code 分析未完成，請檢查登入狀態與帳號額度後重試。")
                if not isinstance(output, str) or not output.strip():
                    raise ClaudeCodeError("Claude Code 沒有回傳分析報告。")
                if on_text is not None and output != last_emitted:
                    on_text(output)
                usage = message.get("usage") or {}
                input_tokens = [usage.get(key) for key in (
                    "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")]
                return {"text": output, "diagnostics": self.diagnostics(), "usage": {
                    "inputTokens": sum(value for value in input_tokens if isinstance(value, int))
                    if isinstance(usage.get("input_tokens"), int) else None,
                    "outputTokens": usage.get("output_tokens")}}

    def close(self):
        process = self.process
        if process:
            stop_process(process, own_group=_own_group())
        self.workspace.cleanup()


def _cli_status(runner=subprocess.run) -> dict:
    """Ask the CLI itself; tokens stay in Claude Code's own storage."""
    try:
        completed = runner([*launch_command(claude_executable()), "auth", "status", "--json"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=15, env=_environment(), stdin=subprocess.DEVNULL,
                           creationflags=NO_WINDOW)
        value = json.loads(completed.stdout or "{}")
    except subprocess.TimeoutExpired as exc:
        raise ClaudeCodeError("Claude Code 暫時無法回應，請稍後重試。") from exc
    except (OSError, ValueError) as exc:
        raise ClaudeCodeError("無法讀取 Claude Code 登入狀態，請確認 CLI 版本。") from exc
    return value if isinstance(value, dict) else {}


def _live_status() -> dict:
    installed = cli_installed()
    if preferences().get("claude_code_disconnected"):
        return {"authenticated": False, "available": True, "auth_source": None,
                "login_pending": False, "status_known": True, "cli_installed": installed}
    try:
        account = _cli_status()
        authenticated = account.get("loggedIn") is True
        result = {"authenticated": authenticated, "available": True,
                  "email": account.get("email") if authenticated else None,
                  "plan": account.get("subscriptionType") if authenticated else None,
                  "auth_source": "existing_claude_code" if authenticated else None,
                  "login_pending": False}
        if not authenticated:
            result["error"] = LOGIN_HINT
    except ClaudeCodeError as exc:
        result = {"authenticated": False, "available": False, "auth_source": None,
                  "login_pending": False, "error": str(exc)}
    write_metadata(_METADATA, {"status": {key: result.get(key) for key in
                                          ("authenticated", "available", "email", "plan", "auth_source")}})
    return result | {"status_known": True, "cli_installed": installed}


def status() -> dict:
    # Only a file check: passive reads never start the CLI.
    installed = cli_installed()
    if preferences().get("claude_code_disconnected"):
        return {"authenticated": False, "available": True, "auth_source": None,
                "login_pending": False, "status_known": True, "cli_installed": installed}
    saved = read_metadata(_METADATA).get("status")
    return {"authenticated": False, "available": True, "auth_source": None,
            **(saved if isinstance(saved, dict) else {}),
            "login_pending": False, "status_known": isinstance(saved, dict),
            "cli_installed": installed}


def require_authorized() -> None:
    result = _live_status()
    if not result["authenticated"]:
        raise ClaudeCodeError(result.get("error") or LOGIN_HINT)


@router.get("")
@router.get("/status")
def get_status():
    return status()


@router.post("/check")
def check_status():
    return _live_status()


@router.post("/login")
def login():
    # Binding means using Claude Code's existing sign-in; the app never runs
    # an OAuth flow or stores Claude tokens itself.
    patch_preferences(remove=("claude_code_disconnected",))
    return {"auth_url": None, "status": _live_status()}


@router.post("/cancel")
def cancel():
    return status()


@router.post("/logout")
def logout():
    # Only unbinds this app. Claude Code's own sign-in is left untouched.
    patch_preferences({"claude_code_disconnected": True})
    write_metadata(_METADATA, {})
    return status()


@router.post("/models")
def models():
    try:
        require_authorized()
    except ClaudeCodeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return cached_models()


@router.get("/models")
def cached_models():
    return {"data": [dict(item) for item in MODEL_CATALOG], "nextCursor": None}
