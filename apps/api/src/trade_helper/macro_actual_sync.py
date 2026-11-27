"""Low-frequency official BLS/BEA actual-value synchronization; no API key needed."""

import sys
import time
from datetime import UTC, datetime

import httpx

from .config import assert_local_mode
from .db import init_db
from .macro_actuals import (
    BEA_RELEASES,
    BLS_API,
    BLS_SERIES,
    bea_release_urls,
    mark_actual_source_offline,
    parse_bea_release,
    parse_bls_response,
    save_actuals,
)


def sync_once(post_bls=None, get_bea=None) -> dict[str, dict]:
    if post_bls is None or get_bea is None:
        with httpx.Client(timeout=20, follow_redirects=True,
                          headers={"User-Agent": "txinTrade/0.1 official macro research"}) as client:
            return sync_once(
                lambda: client.post(BLS_API, json={"seriesid": list(BLS_SERIES)}).raise_for_status().json(),
                lambda url: client.get(url).raise_for_status().text,
            )
    results = {}
    for source in ("bls", "bea"):
        checked = datetime.now(UTC)
        try:
            if source == "bls":
                records = parse_bls_response(post_bls())
            else:
                index = get_bea(BEA_RELEASES)
                if len(index.encode()) > 2_000_000:
                    raise ValueError("BEA release index too large")
                urls = bea_release_urls(index)
                records, failed = [], []
                # One release's format change must not discard the other's new values.
                for kind, url in urls.items():
                    try:
                        body = get_bea(url)
                        if len(body.encode()) > 2_000_000:
                            raise ValueError("BEA release body too large")
                        records.extend(parse_bea_release(body, url, kind))
                    except (httpx.HTTPError, ValueError, KeyError, TypeError, UnicodeError) as exc:
                        failed.append(f"{kind}:{type(exc).__name__}")
            if not records:
                raise ValueError("Official macro actuals missing")
            partial = ",".join(failed) if source == "bea" and failed else None
            changed = save_actuals(source, records, checked, partial_error=partial)
            results[source] = {"status": "partial" if partial else "ok", "parsed": len(records),
                               "changed": changed, **({"failed": failed} if partial else {})}
        except (httpx.HTTPError, ValueError, KeyError, TypeError, UnicodeError) as exc:
            mark_actual_source_offline(source, checked, type(exc).__name__)
            results[source] = {"status": "offline", "error_code": type(exc).__name__}
    return results


def main() -> None:
    assert_local_mode()
    init_db()
    once = "--once" in sys.argv
    while True:
        print(sync_once(), flush=True)
        if once:
            return
        time.sleep(6 * 60 * 60)


if __name__ == "__main__":
    main()
