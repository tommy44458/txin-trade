"""Programmatic, bounded Codex app-server access for a personal local app.

Only the application's dynamic Python tools are exposed during analysis. This
module does not copy shared Codex token files or permit model shell/file
operations; app-owned login persistence is handled by the SQLite adapter.
"""

import atexit
import json
import os
import queue
import subprocess
import tempfile
import threading
from collections.abc import Callable
from time import monotonic

from fastapi import APIRouter, HTTPException

from .auth_metadata import read_metadata, write_metadata
from .cli_paths import find_executable
from .codex_auth_storage import LocalCodexAuth
from .credential_store import CredentialStoreError, delete_credentials
from .local_settings import desktop_mode, patch_preferences, preferences
from .platform_process import NO_WINDOW, WINDOWS, launch_command, stop_process

router = APIRouter(prefix="/api/v1/auth/codex", tags=["Codex authorization"])


class CodexError(RuntimeError):
    """Credential-free operational error suitable for display in the app."""


class CodexTimeoutError(CodexError, TimeoutError):
    """A safe stage-specific timeout, without hidden reasoning or inputs."""


def _find_codex() -> str | None:
    return find_executable("codex", os.getenv("APP_CODEX_PATH"), (
        "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
        "/Applications/Codex.app/Contents/Resources/codex"))


def cli_installed() -> bool:
    return _find_codex() is not None


def codex_executable() -> str:
    executable = _find_codex()
    if executable is None:
        raise CodexError("找不到 Codex。請先安裝 Codex CLI 或 ChatGPT 桌面應用程式。")
    return executable


def _restricted_config() -> dict:
    # Apply at process and thread scope, including on machines with permissive
    # user configuration. environments=[] additionally disables environment tools.
    disabled = ("shell_tool", "unified_exec", "apply_patch_freeform", "view_image",
                "code_mode", "js_repl", "multi_agent", "multi_agent_v2", "plugins",
                "apps", "hooks", "codex_hooks", "plugin_hooks", "browser_use",
                "computer_use", "skill_search", "search_tool", "memory_tool",
                "goals", "tool_suggest", "tool_search", "request_permissions",
                "workspace_dependencies", "skip_host_skill_discovery")
    result = {f"features.{name}": False for name in disabled}
    result["features.skip_host_skill_discovery"] = True
    result["web_search"] = "disabled"
    result["tools.update_plan.enabled"] = False
    result["tools.experimental_request_user_input.enabled"] = False
    result["sandbox_mode"] = "read-only"
    result["approval_policy"] = "never"
    result["include_apps_instructions"] = False
    return result


def _plan_provider_config() -> dict:
    """Codex as a Responses provider authorized by a ChatGPT plan token (OpenAI's documented setup)."""
    prefix = "model_providers.openai_chatgpt_plan"
    return {"model_provider": "openai_chatgpt_plan", f"{prefix}.name": "ChatGPT plan",
            f"{prefix}.base_url": "https://api.openai.com/v1", f"{prefix}.env_key": "ACCESS_TOKEN",
            f"{prefix}.wire_api": "responses", f"{prefix}.requires_openai_auth": False,
            f"{prefix}.supports_websockets": False}


class CodexRpc:
    def __init__(self, *, isolated: bool = False, plan_token: str | None = None,
                 process_factory=subprocess.Popen):
        self.messages: queue.Queue = queue.Queue()
        self.sequence = 0
        self.started_at = monotonic()
        self.last_stage = "連接模型"
        self.notification_counts: dict[str, int] = {}
        self.lock = threading.RLock()
        self.deferred: list[dict] = []
        self.isolated = isolated
        # Outside the desktop app the CLI gets its own POSIX process group; Windows
        # stops the process tree instead.
        self.owns_process_group = not desktop_mode() and not WINDOWS
        self.tool_handler = None
        self.workspace = tempfile.TemporaryDirectory(prefix="ath-codex-", ignore_cleanup_errors=True)
        environment = dict(os.environ)
        for key in list(environment):
            if (key.startswith(("OPENAI_", "BINGX_", "TYPESAFE_", "APP_DESKTOP_TOKEN"))
                    or key in {"DATABASE_URL", "MIGRATION_DATABASE_URL", "JBLANKED_API_KEY", "CODEX_API_KEY"}):
                environment.pop(key)
        self.local_auth = None
        if isolated:
            try:
                self.local_auth = LocalCodexAuth()
            except (CredentialStoreError, ValueError, OSError) as exc:
                self.workspace.cleanup()
                raise CodexError("無法讀取本應用程式的 Codex 授權，請在設定重新登入。") from exc
            environment["CODEX_HOME"] = str(self.local_auth.home)
        if plan_token is not None:
            # A fresh Codex home: neither the user's Codex login nor their config is used.
            home = os.path.join(self.workspace.name, "codex-home")
            os.makedirs(home, exist_ok=True)
            environment["CODEX_HOME"] = home
            environment["ACCESS_TOKEN"] = plan_token
        command = [*launch_command(codex_executable()), "app-server", "--listen", "stdio://"]
        overrides = _restricted_config()
        if plan_token is not None:
            overrides |= _plan_provider_config()
        # Prevent both isolated and shared CLI sessions from opening an OS
        # credential store; a keyring-only shared login must reconnect here.
        overrides["cli_auth_credentials_store"] = "file"
        for key, value in overrides.items():
            command += ["-c", f"{key}={json.dumps(value)}"]
        try:
            self.process = process_factory(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                           errors="replace", bufsize=1,
                                           env=environment, cwd=self.workspace.name,
                                           start_new_session=self.owns_process_group,
                                           creationflags=NO_WINDOW)
            self.reader = threading.Thread(target=self._read, daemon=True)
            self.reader.start()
            self.request("initialize", {
                "clientInfo": {"name": "txintrade", "title": "txinTrade",
                               "version": "0.2.0"},
                "capabilities": {"experimentalApi": True},
            }, timeout=15)
            self.send({"method": "initialized", "params": {}})
        except Exception:
            self.close()
            raise

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
            raise CodexError("Codex 連線已中斷，請重新連接。") from exc

    def _next(self, deadline: float) -> dict:
        while True:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise self._timeout()
            try:
                message = self.messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise self._timeout() from exc
            if message is None:
                raise CodexError("Codex 已停止，請重新連接。")
            method = message.get("method")
            if method:
                self.notification_counts[method] = self.notification_counts.get(method, 0) + 1
                item_type = message.get("params", {}).get("item", {}).get("type")
                if method == "turn/started" or item_type == "reasoning":
                    self.last_stage = "判斷行情"
                elif method == "item/tool/call" or item_type == "dynamicToolCall":
                    self.last_stage = "計算補充指標"
                elif method == "item/agentMessage/delta" or item_type == "agentMessage":
                    self.last_stage = "撰寫報告"
            if "id" in message and "method" in message:
                self._server_request(message)
                continue
            return message

    def _timeout(self) -> CodexTimeoutError:
        elapsed = round(monotonic() - self.started_at)
        return CodexTimeoutError(f"Codex 分析超過等待時間（{elapsed} 秒，最後階段：{self.last_stage}）。請重新分析，或在設定選擇其他模型。")

    def diagnostics(self) -> dict:
        return {"elapsed_ms": round((monotonic() - self.started_at) * 1000),
                "last_stage": self.last_stage,
                "notification_counts": dict(self.notification_counts)}

    def _server_request(self, message: dict):
        identifier, method = message["id"], message["method"]
        if method == "item/tool/call" and self.tool_handler:
            params = message.get("params", {})
            try:
                output = self.tool_handler(params.get("tool", ""), params.get("arguments", {}))
                result = {"success": True, "contentItems": [{"type": "inputText",
                          "text": json.dumps(output, ensure_ascii=False, separators=(",", ":"))}]}
            except (ValueError, TypeError, KeyError) as exc:
                result = {"success": False, "contentItems": [{"type": "inputText",
                          "text": json.dumps({"status": "unavailable", "reason": type(exc).__name__,
                          "message": "Use supplied evidence and finish without this optional tool."})}]}
            self.send({"id": identifier, "result": result})
        elif method.endswith("requestApproval"):
            self.send({"id": identifier, "result": {"decision": "decline"}})
        else:
            self.send({"id": identifier, "error": {"code": -32601,
                       "message": "This application only supports its registered Python tools."}})

    def request(self, method: str, params: dict | None = None, *, timeout: float = 30) -> dict:
        with self.lock:
            self.sequence += 1
            identifier = self.sequence
            self.send({"id": identifier, "method": method, "params": params or {}})
            deadline = monotonic() + timeout
            while True:
                message = self._next(deadline)
                if message.get("id") == identifier and "method" not in message:
                    if "error" in message:
                        # Provider messages may contain private inputs or auth details.
                        raise CodexError(f"Codex 無法完成 {method}（代碼 {message['error'].get('code', 'unknown')}）。")
                    if method != "account/logout":
                        self._persist_auth()
                    return message.get("result", {})
                self.deferred.append(message)
                if len(self.deferred) > 2000:
                    self.deferred.pop(0)

    def analyze(self, instructions: str, context: str, model: str,
                tools: list[dict], tool_handler, *, timeout: float,
                effort: str = "medium", response_format: str = "json",
                on_text: Callable[[str], None] | None = None) -> dict:
        if response_format not in {"json", "text"}:
            raise ValueError("Unsupported Codex response format")
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
        with self.lock:
            self.tool_handler = tool_handler
            deadline = monotonic() + timeout
            # Explicitly disable every configured MCP endpoint, including user-
            # configured servers; replacing {} alone could merge with config.
            configuration = self.request("config/read", {"includeLayers": False},
                                         timeout=min(15, max(0.01, deadline - monotonic())))
            config = _restricted_config()
            for name in configuration.get("config", {}).get("mcp_servers", {}):
                config[f"mcp_servers.{name}.enabled"] = False
            dynamic_tools = [{"type": "function", "name": item["name"],
                              "description": item.get("description", ""),
                              "inputSchema": item["parameters"]} for item in tools]
            start = self.request("thread/start", {
                "model": model or None, "baseInstructions": instructions,
                "developerInstructions": developer_instructions,
                "cwd": self.workspace.name, "ephemeral": True,
                "approvalPolicy": "never", "sandbox": "read-only", "environments": [],
                "config": config, "dynamicTools": dynamic_tools,
            }, timeout=min(30, max(1, deadline - monotonic())))
            thread_id = start["thread"]["id"]
            self.deferred.clear()
            result = self.request("turn/start", {"threadId": thread_id,
                "input": [{"type": "text", "text": context}], "environments": [],
                "effort": effort}, timeout=min(30, max(1, deadline - monotonic())))
            turn_id = result["turn"]["id"]
            text_by_item, final_messages = {}, []
            phases = {}
            last_emitted = ""

            def emit(item_id):
                nonlocal last_emitted
                text = text_by_item.get(item_id, "")
                if (on_text is not None and phases.get(item_id) in {None, "final_answer"}
                        and text != last_emitted):
                    on_text(text)
                    last_emitted = text

            usage = None
            while True:
                if monotonic() >= deadline:
                    raise CodexTimeoutError("Codex 分析回應逾時。")
                event = self.deferred.pop(0) if self.deferred else self._next(deadline)
                params = event.get("params", {})
                if params.get("threadId") not in {None, thread_id}:
                    continue
                if params.get("turnId") not in {None, turn_id}:
                    continue
                method = event.get("method")
                if method == "item/started" and params.get("item", {}).get("type") == "agentMessage":
                    item = params["item"]
                    phases[item.get("id", "final")] = item.get("phase")
                elif method == "item/agentMessage/delta":
                    item_id = params.get("itemId", "final")
                    text_by_item[item_id] = text_by_item.get(item_id, "") + params.get("delta", "")
                    emit(item_id)
                elif method == "item/completed" and params.get("item", {}).get("type") == "agentMessage":
                    item = params["item"]
                    item_id = item.get("id", "final")
                    phases[item_id] = item.get("phase", phases.get(item_id))
                    if phases[item_id] in {None, "final_answer"}:
                        final_messages.append(item.get("text", ""))
                    text_by_item[item_id] = item.get("text", "")
                    emit(item_id)
                elif method == "thread/tokenUsage/updated":
                    usage = params.get("tokenUsage", {}).get("total")
                elif method == "turn/completed" and params.get("turn", {}).get("id") == turn_id:
                    turn = params["turn"]
                    if turn.get("status") != "completed":
                        raise CodexError("Codex 分析未完成，請檢查登入狀態與帳號額度後重試。")
                    output = next((value for value in reversed(final_messages) if value), None)
                    if not output:
                        output = next((value for item_id, value in reversed(text_by_item.items())
                                       if phases.get(item_id) in {None, "final_answer"}), "")
                    if not output:
                        raise CodexError("Codex 沒有回傳分析報告。")
                    self._persist_auth()
                    return {"text": output, "usage": usage, "diagnostics": self.diagnostics()}

    def _persist_auth(self):
        if self.local_auth is not None:
            try:
                self.local_auth.persist()
            except (CredentialStoreError, ValueError, OSError) as exc:
                raise CodexError("無法保存 Codex 授權至本地資料庫，請確認資料目錄後重新登入。") from exc

    def close(self):
        process = getattr(self, "process", None)
        if process:
            stop_process(process, own_group=self.owns_process_group)
        try:
            if self.local_auth is not None:
                self.local_auth.close()
        except (CredentialStoreError, ValueError, OSError):
            # Cleanup is unconditional; explicit operations expose the safe
            # storage error, atexit never logs credentials or provider payloads.
            pass
        finally:
            self.workspace.cleanup()


_auth_lock = threading.RLock()
_auth_rpc: CodexRpc | None = None
_login_id: str | None = None


def _auth_server() -> CodexRpc:
    global _auth_rpc
    isolated = preferences().get("codex_auth_scope") == "application"
    if _auth_rpc is None or _auth_rpc.process.poll() is not None or _auth_rpc.isolated != isolated:
        if _auth_rpc:
            _auth_rpc.close()
        _auth_rpc = CodexRpc(isolated=isolated)
    return _auth_rpc


def _live_status() -> dict:
    global _login_id
    if preferences().get("codex_disconnected"):
        return {"authenticated": False, "available": True, "auth_source": None,
                "login_pending": False}
    try:
        with _auth_lock:
            rpc = _auth_server()
            account = rpc.request("account/read", {"refreshToken": False}, timeout=15).get("account")
            authenticated = bool(account and account.get("type") in {"chatgpt", "chatgptAuthTokens"})
            if authenticated:
                _login_id = None
            result = {"authenticated": authenticated, "available": True,
                    "email": account.get("email") if authenticated else None,
                    "plan": account.get("planType") if authenticated else None,
                    "auth_source": ("application" if rpc.isolated else "existing_codex") if authenticated else None,
                    "login_pending": bool(_login_id and not authenticated)}
            _cache_status(result)
            return result | {"status_known": True}
    except (CodexError, TimeoutError, OSError) as exc:
        result = {"authenticated": False, "available": False, "auth_source": None,
                "login_pending": False, "error": str(exc) if isinstance(exc, CodexError)
                else "Codex 暫時無法連線，請重新連接。"}
        _cache_status(result)
        return result | {"status_known": True}


def _cache_status(result: dict) -> None:
    # account/read remains an explicit user operation. This file has no tokens.
    previous = read_metadata("codex")
    write_metadata("codex", {
        "status": {key: result.get(key) for key in
                   ("authenticated", "available", "email", "plan", "auth_source")},
        "models": previous.get("models", []) if result.get("authenticated") else [],
    })


def status() -> dict:
    if preferences().get("codex_disconnected"):
        return {"authenticated": False, "available": True, "auth_source": None,
                "login_pending": False, "status_known": True}
    if _login_id:
        # This polling belongs to a login explicitly started by the user.
        return _live_status()
    saved = read_metadata("codex").get("status")
    return {"authenticated": False, "available": True, "auth_source": None,
            **(saved if isinstance(saved, dict) else {}),
            "login_pending": False, "status_known": isinstance(saved, dict)}


def require_authorized() -> bool:
    result = _live_status()
    if not result["authenticated"]:
        raise CodexError(result.get("error") or "請先在設定連接 Codex 的 ChatGPT 帳號。")
    return result["auth_source"] == "application"


def shutdown():
    global _auth_rpc
    with _auth_lock:
        if _auth_rpc:
            _auth_rpc.close()
            _auth_rpc = None


atexit.register(shutdown)


def _with_cli(result: dict) -> dict:
    # Only a file check, so passive status reads never start the CLI.
    return result | {"cli_installed": cli_installed()}


@router.get("")
@router.get("/status")
def get_status():
    return _with_cli(status())


@router.post("/check")
def check_status():
    return _with_cli(_live_status())


@router.post("/login")
def login():
    global _login_id
    if not cli_installed():
        return {"auth_url": None, "status": _with_cli(status())}
    with _auth_lock:
        patch_preferences(remove=("codex_disconnected",))
        existing = _live_status()
        if existing["authenticated"]:
            return {"auth_url": None, "status": _with_cli(existing)}
        # New authorization belongs solely to this app. Official CLI file
        # storage is staged temporarily and persisted in encrypted SQLite;
        # existing shared authorization is never copied or replaced.
        patch_preferences({"codex_auth_scope": "application"})
        try:
            result = _auth_server().request("account/login/start", {
                "type": "chatgpt", "useHostedLoginSuccessPage": True,
                "appBrand": "codex"})
            _login_id = result.get("loginId")
            return {"auth_url": result.get("authUrl"), "status": _with_cli(status())}
        except (CodexError, TimeoutError) as exc:
            raise HTTPException(503, str(exc)) from exc


@router.post("/cancel")
def cancel():
    global _login_id
    with _auth_lock:
        if _login_id:
            try:
                _auth_server().request("account/login/cancel", {"loginId": _login_id})
            except (CodexError, TimeoutError) as exc:
                raise HTTPException(503, str(exc)) from exc
            _login_id = None
        return status()


@router.post("/logout")
def logout():
    global _login_id
    with _auth_lock:
        if preferences().get("codex_auth_scope") == "application":
            try:
                _auth_server().request("account/logout")
                # Logout is the user's explicit removal, unlike a stale CLI
                # refresh. Clear the latest local record even if another worker
                # refreshed it after the long-lived auth server was started.
                delete_credentials("codex")
            except (CodexError, TimeoutError, CredentialStoreError) as exc:
                raise HTTPException(503, str(exc)) from exc
        patch_preferences({"codex_disconnected": True})
        _login_id = None
        shutdown()
        return status()


@router.post("/models")
def models():
    try:
        with _auth_lock:
            require_authorized()
            result = _auth_server().request("model/list", {"includeHidden": False})
            catalog = {"data": [{key: item.get(key) for key in
                              ("id", "model", "displayName", "isDefault")}
                             for item in result.get("data", [])],
                    "nextCursor": result.get("nextCursor")}
            write_metadata("codex", {**read_metadata("codex"), "models": catalog["data"]})
            return catalog
    except (CodexError, TimeoutError) as exc:
        raise HTTPException(503, str(exc)) from exc


@router.get("/models")
def cached_models():
    return {"data": read_metadata("codex").get("models", []), "nextCursor": None}
