import { useEffect, useState } from "react";
import { uiText } from "./i18n/index.ts";
import { settingsRequest, type LocalSettings } from "./localSettings";
import { AnalysisSpinner } from "./AnalysisProgress";

type Sharing = { enabled: boolean; shared: number };

/** Opt-in sharing of the track record, so the AI can be improved from real results. */
export default function OutcomeSharingSection({ enabled, onChanged }: {
  enabled: boolean;
  onChanged: (settings: LocalSettings) => void;
}) {
  const [sharing, setSharing] = useState<Sharing | null>(null);
  const [busy, setBusy] = useState<"toggle" | "delete" | null>(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    settingsRequest<Sharing>("/outcomes/sharing").then(setSharing).catch(() => setSharing(null));
  }, [enabled]);

  async function run(action: "toggle" | "delete", task: () => Promise<void>) {
    setBusy(action);
    setError("");
    setMessage("");
    try { await task(); }
    catch (reason) { setError((reason as Error).message); }
    finally { setBusy(null); }
  }

  return (
    <section className="panel settings-section" aria-labelledby="settings-sharing-title">
      <div className="panel-head">
        <h2 id="settings-sharing-title">{uiText("分享對帳結果以改善 AI")}<small className="settings-optional">{uiText("選填")}</small></h2>
        <span className={`settings-status${enabled ? " connected" : ""}`}>{enabled ? uiText("已開啟") : uiText("未開啟")}</span>
      </div>
      <p className="settings-help">{uiText("開啟後，App 會把已判定的對帳結果上傳到 txinTrade 雲端，用來比較不同 AI 模型與提示詞版本的實際表現，持續改善分析流程。需要登入 txinTrade 雲端帳號；未登入時結果會先保留在電腦上。")}</p>
      <div className="settings-permission-note">
        <strong>{uiText("會上傳")}</strong>
        <span>{uiText("交易對、週期、報告類型與方向、結果與賺賠幾 R、之後的漲跌幅、風險承擔與交易風格、AI 模型與提示詞版本。")}</span>
      </div>
      <div className="settings-permission-note">
        <strong>{uiText("不會上傳")}</strong>
        <span>{uiText("報告內容、進場與持倉價格、數量、帳戶資金、交易所與金鑰。")}</span>
      </div>
      <label className="settings-sharing-toggle">
        <input type="checkbox" checked={enabled} disabled={!!busy}
          onChange={(event) => void run("toggle", async () => {
            onChanged(await settingsRequest<LocalSettings>("/settings", {
              method: "PATCH", body: JSON.stringify({ share_outcomes: event.target.checked }),
            }));
          })} />
        <span>{uiText("分享我的對帳結果")}</span>
        {busy === "toggle" && <AnalysisSpinner />}
      </label>
      {sharing && sharing.shared > 0 && (
        <div className="settings-actions">
          <span className="settings-help">{uiText("已上傳 {{p0}} 筆。", { p0: sharing.shared })}</span>
          <button type="button" className="settings-secondary" disabled={!!busy}
            onClick={() => void run("delete", async () => {
              const result = await settingsRequest<Sharing>("/outcomes/sharing/delete", { method: "POST" });
              setSharing(result);
              onChanged(await settingsRequest<LocalSettings>("/settings"));
              setMessage(uiText("已刪除雲端上的對帳結果，並停止分享。"));
            })}>{busy === "delete" && <AnalysisSpinner />}{uiText("刪除已上傳的資料")}</button>
        </div>
      )}
      {message && <p className="settings-notice" role="status">{message}</p>}
      {error && <p className="settings-inline-error" role="alert">{error}</p>}
    </section>
  );
}
