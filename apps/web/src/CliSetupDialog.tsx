import { useEffect, useRef, useState } from "react";
import { uiText } from "./i18n/index.ts";
import Icon from "./Icon";
import { AnalysisSpinner } from "./AnalysisProgress";
import "./CliSetupDialog.css";

// The ChatGPT plan analyzes through Codex, so it needs the same install.
export type CliSetupProvider = "codex" | "claude_code" | "chatgpt_plan";
export type CliSetupReason = "install" | "signin";

type Platform = "windows" | "posix";

// Shell commands are product-neutral and are not translated. The first install
// command is each vendor's official standalone installer, which needs no Node.js.
const CLAUDE = {
  windows: {
    install: "irm https://claude.ai/install.ps1 | iex",
    alternatives: ["winget install Anthropic.ClaudeCode", "npm install -g @anthropic-ai/claude-code"],
    // The native installer's own location: works even when it did not reach PATH.
    fullPath: '& "$env:USERPROFILE\\.local\\bin\\claude.exe" auth login',
    // Claude Code's documented fix: add that location to the user's PATH.
    addToPath: "[Environment]::SetEnvironmentVariable('PATH', [Environment]::GetEnvironmentVariable('PATH', 'User') + \";$env:USERPROFILE\\.local\\bin\", 'User')",
  },
  posix: {
    install: "curl -fsSL https://claude.ai/install.sh | bash",
    alternatives: ["brew install --cask claude-code", "npm install -g @anthropic-ai/claude-code"],
    fullPath: "~/.local/bin/claude auth login",
  },
} as const;
const CODEX = {
  windows: {
    install: 'powershell -ExecutionPolicy ByPass -c "irm https://chatgpt.com/codex/install.ps1 | iex"',
    alternatives: ["npm install -g @openai/codex"],
    // The standalone installer's location; prints a version when Codex is installed.
    check: '& "$env:LOCALAPPDATA\\Programs\\OpenAI\\Codex\\bin\\codex.exe" --version',
  },
  posix: {
    install: "curl -fsSL https://chatgpt.com/codex/install.sh | sh",
    alternatives: ["brew install --cask codex", "npm install -g @openai/codex"],
    check: "~/.local/bin/codex --version",
  },
} as const;
const SIGN_IN_COMMAND = "claude auth login";

async function copy(value: string) {
  // The desktop app denies the page clipboard access; its shell copies instead.
  const desktop = window.tradeHelper?.copyText;
  if (desktop) return desktop(value);
  if (!navigator.clipboard) throw new Error("clipboard unavailable");
  return navigator.clipboard.writeText(value);
}

function Command({ value }: { value: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  return (
    <div className="cli-setup-command">
      <code>{value}</code>
      <button type="button" className="settings-secondary" onClick={() => {
        void copy(value).then(() => setState("copied"), () => setState("failed"));
      }}>{state === "copied" ? uiText("已複製") : uiText("複製")}</button>
      {state === "failed" && <span className="cli-setup-copy-failed" role="status">
        {uiText("無法複製，請直接選取指令")}</span>}
    </div>
  );
}

function Alternatives({ commands }: { commands: readonly string[] }) {
  return (
    <details className="cli-setup-alternatives">
      <summary>{uiText("或改用其他安裝方式")}</summary>
      {commands.map(command => <Command key={command} value={command} />)}
    </details>
  );
}

export default function CliSetupDialog({ provider, reason, checking, error, onRecheck, onClose }: {
  provider: CliSetupProvider;
  reason: CliSetupReason;
  checking: boolean;
  error: string;
  onRecheck: () => void;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const recheck = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const element = dialog.current;
    if (element && !element.open) element.showModal();
    recheck.current?.focus();
    return () => element?.close();
  }, []);
  const claude = provider === "claude_code";
  const platform: Platform = window.tradeHelper?.platform === "win32" ? "windows" : "posix";
  const windows = platform === "windows";
  const install = reason === "install";
  const title = reason === "signin" ? uiText("Claude Code 尚未登入")
    : claude ? uiText("找不到 Claude Code CLI") : uiText("找不到 Codex CLI");
  const openShell = windows ? uiText("從開始功能表搜尋並開啟「PowerShell」，貼上這行後按 Enter：")
    : uiText("開啟「終端機」（應用程式 › 工具程式），貼上這行後按 Return：");
  return (
    <dialog ref={dialog} className="cli-setup-dialog" aria-labelledby="cli-setup-title"
      onCancel={event => { event.preventDefault(); onClose(); }}>
      <div className="cli-setup-head">
        <h2 id="cli-setup-title">{title}</h2>
        <button type="button" className="cli-setup-close" onClick={onClose} aria-label={uiText("關閉")}>
          <Icon name="close" />
        </button>
      </div>
      <p className="settings-help">
        {!install
          ? uiText("Claude Code 已安裝，但尚未登入 Claude 帳號。請依下列步驟完成後重新檢查。")
          : claude
            ? uiText("使用 Claude Code 分析，需要在這台電腦安裝 Claude Code 命令列工具，並登入付費的 Claude 帳號（Pro、Max、Team 或 Enterprise；免費方案不含 Claude Code）。")
            : provider === "chatgpt_plan"
              ? uiText("「ChatGPT 方案（官方授權）」透過這台電腦的 Codex 命令列工具執行分析，需要先安裝 Codex，再回到設定按「連線 ChatGPT」授權。只授權 ChatGPT、沒有安裝 Codex，無法進行分析。")
              : uiText("使用 Codex 分析，需要在這台電腦安裝 Codex 命令列工具，之後在 txinTrade 裡用 ChatGPT 帳號登入。")}
      </p>
      {install && (claude || windows) && (
        <p className="cli-setup-note">
          {claude
            ? uiText("只安裝 Claude 桌面版還不夠：txinTrade 需要的是 Claude Code 命令列工具，可以和桌面版同時安裝。")
            : uiText("只安裝 ChatGPT 桌面版還不夠：請另外安裝 Codex 命令列工具。")}
        </p>
      )}
      <ol className="cli-setup-steps">
        {install && claude && (
          <li>
            <p>{openShell}</p>
            <Command value={CLAUDE[platform].install} />
            <Alternatives commands={CLAUDE[platform].alternatives} />
          </li>
        )}
        {install && !claude && (
          <>
            <li>
              <p>{openShell}</p>
              <Command value={CODEX[platform].install} />
              <p className="cli-setup-hint">{windows
                ? uiText("請複製整行：裡面已包含允許執行安裝程式的設定。不需要 Node.js 或 npm。")
                : uiText("這個官方安裝程式不需要 Node.js 或 npm。")}</p>
              <Alternatives commands={CODEX[platform].alternatives} />
            </li>
            <li>
              <p>{windows
                ? uiText("等到 PowerShell 不再輸出文字、又出現可以輸入的提示時，就代表裝好了。不需要關掉 PowerShell，也不用設定 PATH。")
                : uiText("等到終端機不再輸出文字、又出現可以輸入的提示時，就代表裝好了。不需要關掉終端機，也不用設定 PATH。")}</p>
            </li>
            <li>
              <p>{uiText("回到這裡按「重新檢查」，txinTrade 會自己找到 Codex。")}</p>
              <p className="cli-setup-hint">{uiText("如果還是顯示找不到 Codex，在同一個視窗執行這行：出現版本號就代表已裝好，請再按一次「重新檢查」；出現錯誤則代表沒裝成功，請重做第 1 步，並留意紅色的錯誤訊息。")}</p>
              <Command value={CODEX[platform].check} />
            </li>
          </>
        )}
        {claude && (
          <li>
            <p>{install
              ? windows
                ? uiText("安裝完成後，關閉這個 PowerShell，再開一個新的視窗（新視窗才會用到更新後的 PATH），執行登入指令並依瀏覽器指示完成授權：")
                : uiText("安裝完成後，開一個新的終端機視窗，執行登入指令並依瀏覽器指示完成授權：")
              : windows
                ? uiText("在 PowerShell 登入 Claude 帳號，並依瀏覽器指示完成授權：")
                : uiText("在終端機登入 Claude 帳號，並依瀏覽器指示完成授權：")}</p>
            <Command value={SIGN_IN_COMMAND} />
            <p className="cli-setup-hint">{windows
              ? uiText("如果仍出現「無法辨識 claude」，是 Windows 版安裝程式沒有把它加入 PATH。不必自己設定，改執行這行登入即可：")
              : uiText("如果出現「command not found」，改執行這行即可：")}</p>
            <Command value={CLAUDE[platform].fullPath} />
            {install && <p className="cli-setup-hint">
              {uiText("如果這行也顯示找不到，代表還沒裝好：請重做第 1 步，並留意紅色的錯誤訊息。")}</p>}
            {windows && (
              <details className="cli-setup-alternatives">
                <summary>{uiText("想在任何視窗直接輸入 claude？執行這行後再開新視窗：")}</summary>
                <Command value={CLAUDE.windows.addToPath} />
              </details>
            )}
          </li>
        )}
        <li>
          <p>{claude
            ? uiText("完成後回到這裡按「重新檢查」。txinTrade 會自動找到 Claude Code，不需要重新開啟 App。")
            : uiText("找到 Codex 之後，按「連線 Codex」，在開啟的瀏覽器用 ChatGPT 帳號登入，完成後會自動回到 txinTrade。不需要在命令列登入。")}</p>
        </li>
      </ol>
      {error && <p className="settings-inline-error" role="alert">{error}</p>}
      <div className="settings-actions">
        <button type="button" className="action" onClick={onRecheck} disabled={checking} aria-busy={checking}
          ref={recheck}>
          {checking && <AnalysisSpinner />}{" "}{uiText("重新檢查")}
        </button>
        <button type="button" className="settings-secondary" onClick={onClose}>{uiText("稍後再說")}</button>
      </div>
    </dialog>
  );
}
