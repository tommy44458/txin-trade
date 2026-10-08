import { useState } from "react";
import { uiText } from "./i18n/index.ts";
import { settingsRequest, type LocalSettings } from "./localSettings";
import { AnalysisSpinner } from "./AnalysisProgress";

/** Whether analyses may search the web for recent news; on by default. */
export default function WebSearchSection({ enabled, onChanged }: {
  enabled: boolean;
  onChanged: (settings: LocalSettings) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <section className="panel settings-section" aria-labelledby="settings-web-search-title">
      <div className="panel-head">
        <h2 id="settings-web-search-title">{uiText("AI 網路搜尋")}</h2>
        <span className={`settings-status${enabled ? " connected" : ""}`}>{enabled ? uiText("已開啟") : uiText("未開啟")}</span>
      </div>
      <p className="settings-help">{uiText("分析時，AI 可搜尋網路最多 3 次，查詢突發新聞、交易所公告或重大事件。搜尋結果只作背景參考，不取代行情數字，報告會列出參考的網頁。使用 Claude API 或 OpenAI API 金鑰時，每次搜尋會另外計費。")}</p>
      <label className="settings-sharing-toggle">
        <input type="checkbox" checked={enabled} disabled={busy}
          onChange={(event) => {
            const next = event.target.checked;
            setBusy(true);
            setError("");
            settingsRequest<LocalSettings>("/settings", { method: "PATCH", body: JSON.stringify({ allow_web_search: next }) })
              .then(onChanged)
              .catch((reason: Error) => setError(reason.message))
              .finally(() => setBusy(false));
          }} />
        <span>{uiText("允許 AI 搜尋網路最新資訊")}</span>
        {busy && <AnalysisSpinner />}
      </label>
      {error && <p className="settings-inline-error" role="alert">{error}</p>}
    </section>
  );
}
