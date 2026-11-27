"""Versioned BLS API and BEA release actuals, visible only after local ingestion."""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from .db import connect
from .storage_codec import decode_row, utc_text

BLS_API = "https://api.bls.gov/publicAPI/v1/timeseries/data/"
BEA_RELEASES = "https://www.bea.gov/news/current-releases"
BLS_SERIES = (
    "CUSR0000SA0", "CUUR0000SA0", "CUUR0000SA0L1E",
    "CES0000000001", "LNS14000000",
)
METRIC_KIND = {
    "cpi_mom": "cpi", "cpi_yoy": "cpi", "core_cpi_yoy": "cpi",
    "payroll_change": "employment", "unemployment_rate": "employment",
    "pce_yoy": "pce", "core_pce_yoy": "pce", "real_gdp_annualized": "gdp",
}
METRIC_LABEL = {
    "cpi_mom": "CPI 月增率", "cpi_yoy": "CPI 年增率",
    "core_cpi_yoy": "核心 CPI 年增率", "payroll_change": "非農就業月增量",
    "unemployment_rate": "失業率", "pce_yoy": "PCE 物價年增率",
    "core_pce_yoy": "核心 PCE 物價年增率", "real_gdp_annualized": "實質 GDP 年化季增率",
}
EASTERN = ZoneInfo("America/New_York")
# A year-over-year change is published with the month-over-month change of the same release;
# when only the monthly change exists, the agency did not publish the annual one.
SAME_RELEASE_ANCHOR = {"cpi_yoy": "cpi_mom", "core_cpi_yoy": "cpi_mom"}


def _decimal(value: object) -> Decimal:
    try:
        result = Decimal(str(value).replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError("Invalid official statistic") from exc
    if not result.is_finite():
        raise ValueError("Invalid official statistic")
    return result


def _percent(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def _month_before(period: str, months: int = 1) -> str:
    year, month = map(int, period.split("-"))
    index = year * 12 + month - 1 - months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _actual(source: str, metric: str, period: str, value: str,
            source_url: str, *, published_at: str | None = None,
            previous_value: str | None = None, method: str,
            evidence: str) -> dict:
    return {
        "source": source, "kind": METRIC_KIND[metric], "metric": metric,
        "label": METRIC_LABEL[metric], "period": period, "value": value,
        "unit": "thousand_jobs" if metric == "payroll_change" else "percent",
        "previous_value": previous_value, "published_at": published_at,
        "source_url": source_url, "method": method, "evidence": evidence[:300],
    }


def parse_bls_response(body: dict) -> list[dict]:
    """Calculate published-series changes; label them as derived, not release headlines."""
    if body.get("status") != "REQUEST_SUCCEEDED":
        raise ValueError("BLS API did not succeed")
    series_list = body.get("Results", {}).get("series", [])
    if not isinstance(series_list, list) or {s.get("seriesID") for s in series_list} != set(BLS_SERIES):
        raise ValueError("BLS API series are incomplete")
    values: dict[str, dict[str, Decimal]] = {}
    for series in series_list:
        monthly = {}
        for item in series.get("data", []):
            period = item.get("period", "")
            if (re.fullmatch(r"M(0[1-9]|1[0-2])", period) and
                    re.fullmatch(r"20\d{2}", item.get("year", "")) and
                    item.get("value") not in {None, "-"}):
                monthly[f"{item['year']}-{period[1:]}"] = _decimal(item["value"])
        if not monthly:
            raise ValueError("BLS monthly observations are missing")
        values[series["seriesID"]] = monthly
    result = []
    for series_id, metric, lag in (
        ("CUSR0000SA0", "cpi_mom", 1),
        ("CUUR0000SA0", "cpi_yoy", 12),
        ("CUUR0000SA0L1E", "core_cpi_yoy", 12),
        ("CES0000000001", "payroll_change", 1),
    ):
        observations = values[series_id]
        period = max(observations)
        previous_period = _month_before(period, lag)
        if previous_period not in observations:
            continue
        current, previous = observations[period], observations[previous_period]
        if previous <= 0:
            raise ValueError("BLS comparison index is invalid")
        if metric == "payroll_change":
            value = str(current - previous)
            earlier = _month_before(previous_period)
            previous_value = (str(previous - observations[earlier])
                              if earlier in observations else None)
        else:
            value = _percent((current / previous - 1) * 100)
            prior = _month_before(period)
            prior_comparison = _month_before(prior, lag)
            previous_value = (_percent((observations[prior] / observations[prior_comparison] - 1) * 100)
                              if prior in observations and prior_comparison in observations else None)
        result.append(_actual("bls", metric, period, value, BLS_API,
                              previous_value=previous_value,
                              method="derived_from_bls_v1_series",
                              evidence=f"{series_id}: {period}={current}; {previous_period}={previous}"))
    unemployment = values["LNS14000000"]
    period = max(unemployment)
    previous = unemployment.get(_month_before(period))
    result.append(_actual("bls", "unemployment_rate", period, str(unemployment[period]), BLS_API,
                          previous_value=str(previous) if previous is not None else None,
                          method="published_bls_v1_series",
                          evidence=f"LNS14000000: {period}={unemployment[period]}"))
    return result


class _OfficialHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text: list[str] = []
        self.link: str | None = None
        self.link_text: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.link = dict(attrs).get("href")
            self.link_text = []

    def handle_data(self, data):
        self.text.append(data)
        if self.link is not None:
            self.link_text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.link is not None:
            self.links.append((" ".join(" ".join(self.link_text).split()), self.link))
            self.link = None


def bea_release_urls(body: str) -> dict[str, str]:
    parser = _OfficialHTML()
    parser.feed(body)
    if "Releases" not in " ".join(parser.text):
        raise ValueError("BEA release index missing")
    result = {}
    for title, href in parser.links:
        kind = ("pce" if title.startswith("Personal Income and Outlays,") else
                "gdp" if title.startswith("GDP (") else None)
        url = urljoin(BEA_RELEASES, href)
        parsed = urlsplit(url)
        if kind and kind not in result and parsed.scheme == "https" and parsed.hostname == "www.bea.gov" and re.fullmatch(r"/news/20\d{2}/[a-z0-9-]+", parsed.path):
            result[kind] = url
    if set(result) != {"pce", "gdp"}:
        raise ValueError("BEA latest PCE or GDP release missing")
    return result


def _bea_published(text: str) -> str:
    match = re.search(r"EMBARGOED UNTIL RELEASE AT\s+8:30\s+a\.m\.\s+(EDT|EST),\s+\w+,\s+([A-Za-z]+ \d{1,2}, 20\d{2})", text)
    if not match:
        raise ValueError("BEA official publication time missing")
    at = datetime.strptime(match.group(2) + " 08:30", "%B %d, %Y %H:%M").replace(tzinfo=EASTERN)
    if at.tzname() != match.group(1):
        raise ValueError("BEA publication timezone mismatch")
    return at.astimezone(UTC).isoformat()


def parse_bea_release(body: str, url: str, kind: str) -> list[dict]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "www.bea.gov" or kind not in {"pce", "gdp"}:
        raise ValueError("Unapproved BEA release URL")
    parser = _OfficialHTML()
    parser.feed(body)
    text = " ".join(" ".join(parser.text).split())
    # Inline markup splits text nodes, so "(<abbr>GDP</abbr>)" reads "(GDP )".
    text = re.sub(r"\(\s+", "(", re.sub(r"\s+\)", ")", text))
    published = _bea_published(text)
    if kind == "gdp":
        pattern = (r"Real gross domestic product \(GDP\) (increased|decreased) at an annual rate of "
                   r"(\d+(?:\.\d+)?) percent in the (first|second|third|fourth) quarter of (20\d{2})")
        match = re.search(pattern, text)
        if not match:
            raise ValueError("BEA real GDP headline missing")
        quarter = {"first": 1, "second": 2, "third": 3, "fourth": 4}[match.group(3)]
        value = _decimal(match.group(2)) * (1 if match.group(1) == "increased" else -1)
        return [_actual("bea", "real_gdp_annualized", f"{match.group(4)}-Q{quarter}",
                        str(value), url, published_at=published,
                        method="official_bea_release_text", evidence=match.group(0))]
    title = re.search(r"Personal Income and Outlays, ([A-Za-z]+) (20\d{2})", text)
    if not title:
        raise ValueError("BEA PCE period missing")
    month = datetime.strptime(title.group(1), "%B").replace(tzinfo=UTC).month
    period = f"{title.group(2)}-{month:02d}"
    headline = re.search(r"From the same month one year ago, the PCE price index for "
                         + re.escape(title.group(1)) + r" (increased|decreased) (\d+(?:\.\d+)?) percent", text)
    core = re.search(r"Excluding food and energy, the PCE price index (increased|decreased) "
                     r"(\d+(?:\.\d+)?) percent from one year ago", text)
    if not headline or not core:
        raise ValueError("BEA PCE annual actuals missing")
    result = []
    for metric, match in (("pce_yoy", headline), ("core_pce_yoy", core)):
        value = _decimal(match.group(2)) * (1 if match.group(1) == "increased" else -1)
        result.append(_actual("bea", metric, period, str(value), url, published_at=published,
                              method="official_bea_release_text", evidence=match.group(0)))
    return result


def _validate_actual(record: dict, cutoff: datetime | None = None) -> None:
    source = record.get("source")
    metric = record.get("metric")
    period = record.get("period", "")
    parsed = urlsplit(record.get("source_url", ""))
    if (source not in {"bls", "bea"} or metric not in METRIC_KIND or
            record.get("kind") != METRIC_KIND[metric] or
            not re.fullmatch(r"20\d{2}-(?:0[1-9]|1[0-2]|Q[1-4])", period) or
            record.get("unit") != ("thousand_jobs" if metric == "payroll_change" else "percent") or
            parsed.scheme != "https" or parsed.hostname !=
            ("api.bls.gov" if source == "bls" else "www.bea.gov") or
            record.get("method") not in ({"derived_from_bls_v1_series", "published_bls_v1_series"}
                                         if source == "bls" else {"official_bea_release_text"}) or
            (source == "bls" and record.get("source_url") != BLS_API) or
            (source == "bea" and not re.fullmatch(r"/news/20\d{2}/[a-z0-9-]+", parsed.path)) or
            not isinstance(record.get("evidence"), str) or not record["evidence"]):
        raise ValueError("Invalid official macro actual")
    value = _decimal(record["value"])
    if abs(value) > (1000 if metric == "payroll_change" else 100):
        raise ValueError("Implausible official macro actual")
    if record.get("previous_value") is not None:
        _decimal(record["previous_value"])
    published = record.get("published_at")
    if published is not None:
        at = datetime.fromisoformat(published)
        if at.tzinfo is None or source != "bea" or (cutoff and at > cutoff):
            raise ValueError("Future or invalid macro publication")
    if source == "bea" and published is None:
        raise ValueError("BEA actual lacks official publication time")
    if cutoff is not None:
        local_day = cutoff.astimezone(EASTERN).date()
        period_year = int(period[:4])
        period_index = (int(period[6]) * 3 if "-Q" in period else int(period[5:]))
        current_index = (3 * ((local_day.month - 1) // 3 + 1)
                         if "-Q" in period else local_day.month)
        if (period_year, period_index) > (local_day.year, current_index):
            raise ValueError("Future macro observation period")
    if cutoff is not None and "ingested_at" in record:
        ingested = datetime.fromisoformat(record["ingested_at"])
        if ingested.tzinfo is None or ingested > cutoff:
            raise ValueError("Macro actual learned after cutoff")


def save_actuals(source: str, records: list[dict], observed_at: datetime, *,
                 partial_error: str | None = None) -> int:
    """Save new versions; partial_error records that some of the source's releases failed."""
    if source not in {"bls", "bea"} or observed_at.tzinfo is None:
        raise ValueError("Invalid macro source or observation time")
    count = 0
    with connect() as db:
        for record in records:
            if record.get("source") != source:
                raise ValueError("Macro source mismatch")
            _validate_actual(record, observed_at)
            canonical = json.dumps(record, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(canonical.encode()).hexdigest()
            prior = db.execute(
                "SELECT version,content_hash FROM macro_actual_versions "
                "WHERE source=? AND metric=? AND period=? ORDER BY version DESC LIMIT 1",
                (source, record["metric"], record["period"]),
            ).fetchone()
            if prior and prior["content_hash"] == digest:
                continue
            version = prior["version"] + 1 if prior else 1
            db.execute(
                "INSERT INTO macro_actual_versions(source,metric,period,version,ingested_at,content_hash,payload) "
                "VALUES(?,?,?,?,?,?,?)",
                (source, record["metric"], record["period"], version,
                 utc_text(observed_at), digest, canonical),
            )
            count += 1
        db.execute("INSERT INTO macro_actual_source_checks(source,status,checked_at,error_code) "
                   "VALUES(?,?,?,?)", (source, "partial" if partial_error else "ok",
                                       utc_text(observed_at), partial_error[:80] if partial_error else None))
        db.commit()
    return count


def mark_actual_source_offline(source: str, observed_at: datetime, code: str) -> None:
    with connect() as db:
        db.execute("INSERT INTO macro_actual_source_checks(source,status,checked_at,error_code) "
                   "VALUES(?,'offline',?,?)", (source, utc_text(observed_at), code[:80]))
        db.commit()


def actuals_as_of(db, cutoff: datetime) -> tuple[list[dict], dict[str, str]]:
    rows = db.execute(
        "WITH ranked AS (SELECT source,metric,period,version,ingested_at,content_hash,payload,"
        "ROW_NUMBER() OVER (PARTITION BY source,metric,period ORDER BY version DESC) AS rank "
        "FROM macro_actual_versions WHERE ingested_at<=?) SELECT source,metric,period,version,"
        "ingested_at,content_hash,payload FROM ranked WHERE rank=1 ORDER BY source,metric,period",
        (utc_text(cutoff),),
    ).fetchall()
    rows = [decode_row(row, json_fields=("payload",), time_fields=("ingested_at",)) for row in rows]
    recent = []
    for row in rows:
        payload = row["payload"]
        available = row["ingested_at"]
        published = datetime.fromisoformat(payload["published_at"]) if payload.get("published_at") else None
        max_age = timedelta(days=120 if payload["kind"] == "gdp" else 45)
        if (published or available) < cutoff - max_age:
            continue
        record = {"id": f"{row['source']}:{row['metric']}:{row['period']}",
                  "version": row["version"], "ingested_at": available.isoformat(),
                  "content_hash": row["content_hash"], **payload}
        _validate_actual(record, cutoff)
        recent.append(record)
    # One latest period per metric; as-of versions remain in storage for old reports.
    latest = {}
    for record in recent:
        key = record["metric"]
        if key not in latest or (record["period"], record["ingested_at"]) > (latest[key]["period"], latest[key]["ingested_at"]):
            latest[key] = record
    checks = db.execute(
        "WITH ranked AS (SELECT source,status,checked_at,ROW_NUMBER() OVER "
        "(PARTITION BY source ORDER BY checked_at DESC,id DESC) AS rank "
        "FROM macro_actual_source_checks WHERE checked_at<=?) SELECT source,status,checked_at "
        "FROM ranked WHERE rank=1 ORDER BY source", (utc_text(cutoff),),
    ).fetchall()
    checks = [decode_row(row, time_fields=("checked_at",)) for row in checks]
    status = {source: "offline" for source in ("bls", "bea")}
    status.update({row["source"]: (row["status"] if cutoff - row["checked_at"] <= timedelta(hours=18)
                                   else "offline") for row in checks})
    return sorted(latest.values(), key=lambda item: (item["kind"], item["metric"])), status


def unpublished_actuals(latest: list[dict]) -> list[dict]:
    """Annual changes the agency left out of the release that carries the latest monthly change.

    BLS did not collect October 2025 prices, so it published no 12-month CPI change for
    October 2026; the series simply lacks the comparison month.
    """
    by_metric = {record["metric"]: record for record in latest}
    gaps = []
    for metric, anchor in SAME_RELEASE_ANCHOR.items():
        reference, own = by_metric.get(anchor), by_metric.get(metric)
        if reference is None or (own is not None and own["period"] >= reference["period"]):
            continue
        gaps.append({"source": reference["source"], "kind": METRIC_KIND[metric], "metric": metric,
                     "label": METRIC_LABEL[metric], "period": reference["period"],
                     "comparison_period": _month_before(reference["period"], 12),
                     "reason": "comparison_period_not_published",
                     "previous": ({"period": own["period"], "value": own["value"]} if own else None),
                     "source_url": reference["source_url"]})
    return gaps


def validate_snapshot_actual(record: dict, cutoff: datetime) -> None:
    """Verify an as-of report copy has the stored payload's identity and hash."""
    _validate_actual(record, cutoff)
    if (record.get("id") != f"{record['source']}:{record['metric']}:{record['period']}" or
            not isinstance(record.get("version"), int) or record["version"] < 1):
        raise ValueError("Invalid macro actual identity")
    payload = {key: value for key, value in record.items()
               if key not in {"id", "version", "ingested_at", "content_hash"}}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if record.get("content_hash") != hashlib.sha256(canonical.encode()).hexdigest():
        raise ValueError("Macro actual content hash differs")
