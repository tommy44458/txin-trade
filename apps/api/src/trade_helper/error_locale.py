"""Localize known system failures without exposing upstream exception payloads.

Only fixed application messages and explicitly recognized exception names may
reach a saved report. Trading data and AI-generated prose never pass through
this module.
"""

from openai import APIConnectionError, APITimeoutError, AuthenticationError, RateLimitError

from .credential_store import CredentialStoreError

SYSTEM_ERRORS = {
    "QUOTE_STALE": (
        "分析用報價快照超過 180 秒，請重新分析",
        "The analysis quote is more than 180 seconds old. Start a new analysis.",
    ),
    "CANDLES_STALE": (
        "已收盤 K 線資料過期，請重新分析",
        "The closed candles are out of date. Start a new analysis.",
    ),
    "CONTEXT_CANDLES_STALE": (
        "另一週期的已收盤 K 線過期，請重新分析",
        "The closed candles for the other timeframe are out of date. Start a new analysis.",
    ),
    "MARKET_DATA_UNAVAILABLE": (
        "Binance 行情暫時無法取得，請稍後重新分析。",
        "Binance market data is temporarily unavailable. Try the analysis again shortly.",
    ),
    "ANALYSIS_FAILED": (
        "無法完成分析，請檢查服務與行情資料後重新分析。",
        "The analysis could not be completed. Check the service and market data, then try again.",
    ),
}

# These are application-authored literals, not text from a provider's response.
# Preserve established Chinese messages while giving English jobs useful errors.
MODEL_MESSAGES = {
    "OPENAI_API_KEY and OPENAI_MODEL are required for Agent analysis": (
        "尚未設定 OPENAI_API_KEY 或 OPENAI_MODEL，無法產生 Agent 策略分析",
        "OPENAI_API_KEY or OPENAI_MODEL is not configured. Complete model setup before analyzing.",
    ),
    "APP_ANALYSIS_TIMEOUT_SECONDS 必須介於 30 與 900 秒。": (
        "APP_ANALYSIS_TIMEOUT_SECONDS 必須介於 30 與 900 秒。",
        "APP_ANALYSIS_TIMEOUT_SECONDS must be between 30 and 900 seconds.",
    ),
    "APP_CODEX_REASONING_EFFORT 不是支援的推理強度。": (
        "APP_CODEX_REASONING_EFFORT 不是支援的推理強度。",
        "APP_CODEX_REASONING_EFFORT is not a supported reasoning effort.",
    ),
    "APP_CLAUDE_CODE_EFFORT 不是支援的推理強度。": (
        "APP_CLAUDE_CODE_EFFORT 不是支援的推理強度。",
        "APP_CLAUDE_CODE_EFFORT is not a supported reasoning effort.",
    ),
    "Codex 沒有可用模型。": (
        "Codex 沒有可用模型。", "No Codex models are available.",
    ),
    "找不到 Codex。請先安裝 Codex CLI 或 ChatGPT 桌面應用程式。": (
        "找不到 Codex。請先安裝 Codex CLI 或 ChatGPT 桌面應用程式。",
        "Codex was not found. Install Codex CLI or the ChatGPT desktop app first.",
    ),
    "無法讀取本應用程式的 Codex 授權，請在設定重新登入。": (
        "無法讀取本應用程式的 Codex 授權，請在設定重新登入。",
        "This app could not read Codex authorization. Sign in again in Settings.",
    ),
    "Codex 連線已中斷，請重新連接。": (
        "Codex 連線已中斷，請重新連接。", "The Codex connection was interrupted. Reconnect.",
    ),
    "Codex 已停止，請重新連接。": (
        "Codex 已停止，請重新連接。", "Codex stopped. Reconnect.",
    ),
    "Codex 分析未完成，請檢查登入狀態與帳號額度後重試。": (
        "Codex 分析未完成，請檢查登入狀態與帳號額度後重試。",
        "Codex did not finish the analysis. Check sign-in status and account limits, then retry.",
    ),
    "Codex 沒有回傳分析報告。": (
        "Codex 沒有回傳分析報告。", "Codex did not return an analysis report.",
    ),
    "請先在設定連接 Codex 的 ChatGPT 帳號。": (
        "請先在設定連接 Codex 的 ChatGPT 帳號。",
        "Connect your ChatGPT account for Codex in Settings first.",
    ),
    "找不到 Claude Code。請先安裝 Claude Code CLI。": (
        "找不到 Claude Code。請先安裝 Claude Code CLI。",
        "Claude Code was not found. Install the Claude Code CLI first.",
    ),
    "請先在終端機執行 claude auth login 登入 Claude Code，再回到設定按「連線 Claude Code」。": (
        "請先在終端機執行 claude auth login 登入 Claude Code，再回到設定按「連線 Claude Code」。",
        "Run claude auth login in a terminal to sign in to Claude Code, then choose Connect Claude Code in Settings.",
    ),
    "Claude Code 暫時無法回應，請稍後重試。": (
        "Claude Code 暫時無法回應，請稍後重試。",
        "Claude Code is not responding. Try again shortly.",
    ),
    "無法讀取 Claude Code 登入狀態，請確認 CLI 版本。": (
        "無法讀取 Claude Code 登入狀態，請確認 CLI 版本。",
        "Claude Code sign-in status could not be read. Check the CLI version.",
    ),
    "Claude Code 連線已中斷，請重新分析。": (
        "Claude Code 連線已中斷，請重新分析。",
        "The Claude Code connection was interrupted. Start the analysis again.",
    ),
    "Claude Code 已停止，請確認登入狀態後重試。": (
        "Claude Code 已停止，請確認登入狀態後重試。",
        "Claude Code stopped. Check its sign-in status and retry.",
    ),
    "Claude Code 無法啟動分析，請確認版本與登入狀態。": (
        "Claude Code 無法啟動分析，請確認版本與登入狀態。",
        "Claude Code could not start the analysis. Check its version and sign-in status.",
    ),
    "Claude Code 分析未完成，請檢查登入狀態與帳號額度後重試。": (
        "Claude Code 分析未完成，請檢查登入狀態與帳號額度後重試。",
        "Claude Code did not finish the analysis. Check sign-in status and usage limits, then retry.",
    ),
    "Claude Code 沒有回傳分析報告。": (
        "Claude Code 沒有回傳分析報告。", "Claude Code did not return an analysis report.",
    ),
    "請在「AI 分析帳號」輸入並儲存 Anthropic API 金鑰。": (
        "請在「AI 分析帳號」輸入並儲存 Anthropic API 金鑰。",
        "Enter and save an Anthropic API key under AI analysis account.",
    ),
    "Anthropic API 金鑰無效或已撤銷，請到設定重新輸入。": (
        "Anthropic API 金鑰無效或已撤銷，請到設定重新輸入。",
        "The Anthropic API key is invalid or revoked. Enter it again in Settings.",
    ),
    "這把 Anthropic API 金鑰沒有使用此模型的權限。": (
        "這把 Anthropic API 金鑰沒有使用此模型的權限。",
        "This Anthropic API key cannot use the selected model.",
    ),
    "找不到所選的 Claude 模型，請到設定改選其他模型。": (
        "找不到所選的 Claude 模型，請到設定改選其他模型。",
        "The selected Claude model was not found. Choose another model in Settings.",
    ),
    "Anthropic API 用量已達上限，請稍後重試或檢查帳戶額度。": (
        "Anthropic API 用量已達上限，請稍後重試或檢查帳戶額度。",
        "The Anthropic API usage limit was reached. Retry later or check your account limits.",
    ),
    "Anthropic API 拒絕了這次請求，請檢查帳戶額度與模型設定後重試。": (
        "Anthropic API 拒絕了這次請求，請檢查帳戶額度與模型設定後重試。",
        "The Anthropic API rejected the request. Check your account balance and model settings, then retry.",
    ),
    "Anthropic API 回應逾時，請稍後重試。": (
        "Anthropic API 回應逾時，請稍後重試。",
        "The Anthropic API timed out. Try again shortly.",
    ),
    "無法連線到 Anthropic API，請檢查網路後重試。": (
        "無法連線到 Anthropic API，請檢查網路後重試。",
        "Could not reach the Anthropic API. Check your connection and retry.",
    ),
    "Anthropic API 暫時無法使用，請稍後重試。": (
        "Anthropic API 暫時無法使用，請稍後重試。",
        "The Anthropic API is temporarily unavailable. Try again shortly.",
    ),
    "Anthropic API 分析未完成，請稍後重試。": (
        "Anthropic API 分析未完成，請稍後重試。",
        "The Anthropic API analysis did not finish. Try again shortly.",
    ),
    "Claude 這次拒絕回答，請調整後重試。": (
        "Claude 這次拒絕回答，請調整後重試。",
        "Claude declined to answer this time. Adjust the request and retry.",
    ),
    "Claude 沒有回傳分析內容。": (
        "Claude 沒有回傳分析內容。",
        "Claude did not return an analysis.",
    ),
    "Claude 的工具呼叫不完整，請重試。": (
        "Claude 的工具呼叫不完整，請重試。",
        "Claude's tool call was incomplete. Try again.",
    ),
    "Claude 回傳的工具參數無法解析，請重試。": (
        "Claude 回傳的工具參數無法解析，請重試。",
        "Claude returned tool arguments that could not be parsed. Try again.",
    ),
    "Claude 呼叫工具的次數超過上限。": (
        "Claude 呼叫工具的次數超過上限。",
        "Claude called tools more times than allowed.",
    ),
    "請在設定按「連線 ChatGPT」，以你的 ChatGPT Plus 或 Pro 帳號授權。": (
        "請在設定按「連線 ChatGPT」，以你的 ChatGPT Plus 或 Pro 帳號授權。",
        "Choose Connect ChatGPT in Settings and authorize with your ChatGPT Plus or Pro account.",
    ),
    "ChatGPT 授權已失效，請到設定重新連線。": (
        "ChatGPT 授權已失效，請到設定重新連線。",
        "ChatGPT authorization has expired. Reconnect it in Settings.",
    ),
    "無法連線到 OpenAI 授權服務，請檢查網路後重試。": (
        "無法連線到 OpenAI 授權服務，請檢查網路後重試。",
        "Could not reach OpenAI's authorization service. Check your connection and retry.",
    ),
    "OpenAI 授權服務拒絕了這次請求，請重新連線 ChatGPT。": (
        "OpenAI 授權服務拒絕了這次請求，請重新連線 ChatGPT。",
        "OpenAI's authorization service rejected the request. Reconnect ChatGPT.",
    ),
    "無法驗證 ChatGPT 登入身分，請重新連線。": (
        "無法驗證 ChatGPT 登入身分，請重新連線。",
        "The ChatGPT sign-in could not be verified. Reconnect it.",
    ),
    "這個 ChatGPT 帳號沒有授權方案用量；需要 ChatGPT Plus 或 Pro，並在授權頁允許。": (
        "這個 ChatGPT 帳號沒有授權方案用量；需要 ChatGPT Plus 或 Pro，並在授權頁允許。",
        "This ChatGPT account did not authorize plan usage. It needs ChatGPT Plus or Pro, with access allowed on the authorization page.",
    ),
    "OpenAI 沒有回傳完整的授權資料，請重新連線 ChatGPT。": (
        "OpenAI 沒有回傳完整的授權資料，請重新連線 ChatGPT。",
        "OpenAI did not return complete authorization data. Reconnect ChatGPT.",
    ),
    "ChatGPT 方案沒有可用模型。": (
        "ChatGPT 方案沒有可用模型。",
        "No models are available for the ChatGPT plan.",
    ),
    "請在「AI 分析帳號」輸入並儲存 OpenAI API 金鑰。": (
        "請在「AI 分析帳號」輸入並儲存 OpenAI API 金鑰。",
        "Enter and save an OpenAI API key under AI analysis account.",
    ),
    "OpenAI API 金鑰無效或已撤銷，請到設定重新輸入。": (
        "OpenAI API 金鑰無效或已撤銷，請到設定重新輸入。",
        "The OpenAI API key is invalid or revoked. Enter it again in Settings.",
    ),
    "這把 OpenAI API 金鑰沒有讀取模型的權限。": (
        "這把 OpenAI API 金鑰沒有讀取模型的權限。",
        "This OpenAI API key cannot list models.",
    ),
    "OpenAI API 用量已達上限，請稍後重試或檢查帳戶額度。": (
        "OpenAI API 用量已達上限，請稍後重試或檢查帳戶額度。",
        "The OpenAI API usage limit was reached. Retry later or check your account limits.",
    ),
    "無法連線到 OpenAI API，請檢查網路後重試。": (
        "無法連線到 OpenAI API，請檢查網路後重試。",
        "Could not reach the OpenAI API. Check your connection and retry.",
    ),
    "OpenAI API 暫時無法使用，請稍後重試。": (
        "OpenAI API 暫時無法使用，請稍後重試。",
        "The OpenAI API is temporarily unavailable. Try again shortly.",
    ),
}

SAFE_EXCEPTION_NAMES = frozenset({
    "Exception", "ValueError", "RuntimeError", "TimeoutError", "TypeError", "KeyError",
    "OSError", "JSONDecodeError", "APITimeoutError", "APIConnectionError", "AuthenticationError",
    "RateLimitError", "ModelProviderError", "CodexError", "CodexTimeoutError", "ClaudeCodeError",
    "ClaudeCodeTimeoutError", "AnthropicApiError", "ChatGPTPlanError",
    "CredentialStoreError",
})

_ANALYSIS_STAGES = {
    "prompt": ("讀取分析設定", "loading analysis settings"),
    "macro": ("準備宏觀資料", "preparing macro context"),
    "candles": ("取得主週期 K 線", "fetching primary candles"),
    "context_candles": ("取得輔助週期 K 線", "fetching context candles"),
    "higher_candles": ("取得長週期 K 線", "fetching higher timeframe candles"),
    "quote": ("取得現價", "fetching the current price"),
    "tick_size": ("取得最小價格單位", "fetching price precision"),
    "events": ("讀取經濟事件", "loading economic events"),
    "derivatives": ("取得合約市場資料", "fetching derivatives context"),
    "market_reference": ("取得 BTC／ETH 大盤參考", "fetching the BTC/ETH market reference"),
    "fund_flows": ("取得資金流向", "fetching fund flows"),
    "news": ("讀取新聞依據", "loading news context"),
    "snapshot": ("建立行情快照", "building the market snapshot"),
    "preparation": ("計算分析指標", "preparing analysis indicators"),
    "model": ("產生 AI 分析", "generating AI analysis"),
    "report": ("整理分析報告", "building the analysis report"),
    "save_report": ("儲存分析報告", "saving the analysis report"),
}


def analysis_failure_message(stage: str, exc: Exception, output_locale: str = "zh-TW") -> str:
    """Give useful diagnostics without persisting an upstream exception message."""
    english = output_locale == "en-US"
    label = _ANALYSIS_STAGES.get(stage, ("分析流程", "analysis workflow"))[int(english)]
    name = type(exc).__name__
    if name not in SAFE_EXCEPTION_NAMES:
        name = "Exception"
    if english:
        return f"Analysis stopped while {label} ({name}). Try again; if it repeats, share the task ID."
    return f"分析在「{label}」時中斷（{name}）。請重新分析；若重複發生，請提供任務編號。"


def system_error_message(code: str, output_locale: str = "zh-TW") -> str:
    """Return a fixed localized message even for an unknown error code."""
    messages = SYSTEM_ERRORS.get(code, SYSTEM_ERRORS["ANALYSIS_FAILED"])
    return messages[1 if output_locale == "en-US" else 0]


def model_error_message(exc: Exception, output_locale: str = "zh-TW") -> str:
    """Expose only application literals, known validation details, or a safe type."""
    english = output_locale == "en-US"
    message = str(exc)
    if message in MODEL_MESSAGES:
        return MODEL_MESSAGES[message][1 if english else 0]
    if isinstance(exc, ValueError):
        # Reuse the existing application allowlist; do not add strategy checks.
        from .agent import _VALIDATION_ERRORS

        if message in _VALIDATION_ERRORS:
            prefix = "The model response could not be processed: " if english else "模型回應處理失敗："
            return prefix + message
    if english:
        if isinstance(exc, (TimeoutError, APITimeoutError)):
            return "The model analysis timed out. Check usage before starting a new analysis."
        if isinstance(exc, AuthenticationError):
            return "Model authorization failed. Reconnect the model in Settings and retry."
        if isinstance(exc, RateLimitError):
            return "The model is rate limited. Check account limits and retry later."
        if isinstance(exc, APIConnectionError):
            return "The model connection failed. Check the connection and retry."
        if isinstance(exc, CredentialStoreError):
            return "Local model credentials could not be read. Check your connection in Settings."
    name = type(exc).__name__
    if name not in SAFE_EXCEPTION_NAMES:
        name = "Exception"
    return (f"The model analysis could not be completed ({name}). Check model settings and retry."
            if english else f"模型工具流程失敗：{name}")
