import { useEffect, useState } from "react";
import { uiText } from "./i18n/index.ts";
import { CLOUD_ACCOUNT_CHANGED } from "./cloudAccountEvents";
import { apiFetch, isRemoteMode } from "./transport.ts";
import "./UpdateNotice.css";

const POLL_MS = 30_000;

/**
 * Stays above the account on the computer while no txinTrade cloud account is signed in,
 * so remote use from a phone is easy to find. Selecting it opens the cloud account settings.
 */
export default function CloudSignInNotice({ onOpen }: { onOpen: () => void }) {
  const [show, setShow] = useState(false);
  useEffect(() => {
    // The remote page is always signed in; only the computer offers this.
    if (isRemoteMode()) return;
    let active = true;
    const read = () => {
      apiFetch("/api/v1/cloud-account").then((response) => response.ok ? response.json() : null)
        .then((value) => { if (active) setShow(!!value?.configured && value.signed_in === false); })
        .catch(() => {});
    };
    read();
    const timer = window.setInterval(read, POLL_MS);
    window.addEventListener(CLOUD_ACCOUNT_CHANGED, read);
    return () => { active = false; window.clearInterval(timer); window.removeEventListener(CLOUD_ACCOUNT_CHANGED, read); };
  }, []);
  if (!show) return null;
  return (
    <button type="button" className="update-notice cloud-sign-in-notice" onClick={onOpen}>
      <span className="update-notice-dot" aria-hidden="true" />
      <span className="update-notice-label">{uiText("登入雲端帳戶，就能在手機遠端使用")}</span>
    </button>
  );
}
