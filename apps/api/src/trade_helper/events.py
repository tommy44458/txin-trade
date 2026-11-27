"""Official US release calendars, versioned by the time this service observed them."""

import hashlib
import json
import re
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .db import connect, new_id
from .fomc_actual import fomc_results_as_of
from .macro_actuals import actuals_as_of, unpublished_actuals
from .storage_codec import decode_row, utc_text

EASTERN = ZoneInfo("America/New_York")
SOURCES = {
    "bls": "https://www.bls.gov/schedule/news_release/bls.ics",
    "bea": "https://www.bea.gov/news/schedule",
    "fed": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
}
EVENT_TYPES = {
    "cpi": "消費者物價指數 CPI",
    "employment": "非農就業／失業率",
    "pce": "個人消費支出 PCE",
    "gdp": "國內生產毛額 GDP",
    "fomc": "FOMC 利率決策會議",
}


def _official_url(source: str, url: str) -> str:
    parsed = urlsplit(url)
    allowed = {"bls": "bls.gov", "bea": "bea.gov", "fed": "federalreserve.gov"}[source]
    host = (parsed.hostname or "").lower()
    return url if parsed.scheme == "https" and (host == allowed or host.endswith("." + allowed)) else SOURCES[source]


def _record(source: str, uid: str, kind: str, title: str, event_date: date,
            scheduled_at: datetime | None, url: str) -> dict:
    return {"source": source, "source_uid": uid, "kind": kind, "title": title,
            "scheduled_date": event_date.isoformat(),
            "scheduled_at": scheduled_at.astimezone(UTC).isoformat() if scheduled_at else None,
            "time_precision": "minute" if scheduled_at else "date",
            "source_url": _official_url(source, url), "impact": "high", "actual_value": None,
            "expected_value": None, "previous_value": None, "revision_of": None}


def _ics_unfold(raw: str) -> list[str]:
    result: list[str] = []
    for line in raw.replace("\r\n", "\n").split("\n"):
        if line.startswith((" ", "\t")) and result:
            result[-1] += line[1:]
        else:
            result.append(line)
    return result


# BLS names its zone "US-Eastern", which is not an IANA name; without this every
# BLS entry failed to parse and the whole calendar showed offline.
_ICS_ZONES = {"US-Eastern": "America/New_York"}


def _ics_datetime(value: str, name: str) -> tuple[date, datetime | None]:
    if len(value) == 8:
        day = date.fromisoformat(f"{value[:4]}-{value[4:6]}-{value[6:8]}")
        return day, None
    pattern = "%Y%m%dT%H%M%S" if len(value.rstrip("Z")) == 15 else "%Y%m%dT%H%M"
    parsed = datetime.strptime(value.rstrip("Z"), pattern)  # noqa: DTZ007 - zone attached below
    if value.endswith("Z"):
        at = parsed.replace(tzinfo=UTC)
    else:
        tz_match = re.search(r"TZID=([^;:]+)", name)
        zone = _ICS_ZONES.get(tz_match.group(1), tz_match.group(1)) if tz_match else None
        at = parsed.replace(tzinfo=ZoneInfo(zone) if zone else EASTERN)
    return at.astimezone(EASTERN).date(), at


def parse_bls_ics(raw: str) -> list[dict]:
    if "BEGIN:VCALENDAR" not in raw:
        raise ValueError("BLS calendar header missing")
    blocks: list[dict] = []
    current: dict | None = None
    for line in _ics_unfold(raw):
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT" and current is not None:
            blocks.append(current)
            current = None
        elif current is not None and ":" in line:
            key, value = line.split(":", 1)
            current[key] = value
    result = []
    for block in blocks:
        title = block.get("SUMMARY", "").replace("\\,", ",").strip()
        lowered = title.lower()
        kind = ("cpi" if "consumer price index" in lowered else
                "employment" if "employment situation" in lowered else None)
        start_key = next((key for key in block if key.startswith("DTSTART")), None)
        if kind and start_key:
            day, at = _ics_datetime(block[start_key], start_key)
            uid = block.get("UID") or f"{kind}:{title}:{day.isoformat()}"
            result.append(_record("bls", uid, kind, title, day, at,
                                  block.get("URL", SOURCES["bls"])))
    return result


class _ScheduleHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tokens: list[str] = []
        self.rows: list[list[str]] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        clean = " ".join(data.split())
        if clean:
            self.tokens.append(clean)
            if self.cell is not None:
                self.cell.append(clean)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None and self.row is not None:
            self.row.append(" ".join(self.cell))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def parse_bea_html(raw: str) -> list[dict]:
    parser = _ScheduleHTML()
    parser.feed(raw)
    if "Release Schedule" not in " ".join(parser.tokens):
        raise ValueError("BEA schedule header missing")
    year_match = re.search(r"Year\s+(20\d{2})", " ".join(parser.tokens))
    if not year_match:
        raise ValueError("BEA schedule year missing")
    year = int(year_match.group(1))
    result = []
    for cells in parser.rows:
        if len(cells) < 2:
            continue
        title = cells[-1]
        lowered = title.lower()
        kind = ("pce" if "personal income and outlays" in lowered else
                "gdp" if "gdp" in lowered or "gross domestic product" in lowered else None)
        if not kind:
            continue
        match = re.search(r"([A-Za-z]+)\s+(\d{1,2})\s+(\d{1,2}:\d{2})\s*([AP]M)", cells[0])
        if not match:
            continue
        at = datetime.strptime(f"{match.group(1)} {match.group(2)} {year} "
                               f"{match.group(3)} {match.group(4)}", "%B %d %Y %I:%M %p").replace(tzinfo=EASTERN)
        period = re.search(r"(\d(?:st|nd|rd|th) quarter|[A-Za-z]+)\s+20\d{2}", lowered)
        estimate = re.search(r"(advance|second|third|updated) estimate", lowered)
        identity = (f"{period.group(0)}:{estimate.group(1) if estimate else 'regular'}"
                    if period else lowered)
        uid = f"{kind}:{re.sub(r'[^a-z0-9]+', '-', identity).strip('-')}"
        result.append(_record("bea", uid, kind, title, at.date(), at, SOURCES["bea"]))
    return result


def parse_fed_html(raw: str, year: int) -> list[dict]:
    parser = _ScheduleHTML()
    parser.feed(raw)
    text = " ".join(parser.tokens)
    marker = f"{year} FOMC Meetings"
    if marker not in text:
        raise ValueError("FOMC year section missing")
    section = text.split(marker, 1)[1].split(f"{year - 1} FOMC Meetings", 1)[0]
    section = re.sub(r"\(Released [^)]+\)", "", section)
    months = "January February March April May June July August September October November December"
    pattern = rf"\b({months.replace(' ', '|')})\s+(\d{{1,2}})(?:\s*[-–]\s*(\d{{1,2}}))?\*?"
    result = []
    seen = set()
    for match in re.finditer(pattern, section):
        month = match.group(1)
        day = int(match.group(3) or match.group(2))
        event_date = datetime.strptime(f"{month} {day} {year}", "%B %d %Y").replace(tzinfo=EASTERN).date()
        uid = f"fomc:{year}:{month.lower()}"
        if uid in seen:
            continue
        seen.add(uid)
        result.append(_record("fed", uid, "fomc", f"FOMC {month} 政策會議",
                              event_date, None, SOURCES["fed"]))
    return result


def save_source(source: str, events: list[dict], observed_at: datetime) -> int:
    if source not in SOURCES or observed_at.tzinfo is None:
        raise ValueError("Invalid official source or observation time")
    count = 0
    with connect() as db:
        for payload in events:
            if payload["source"] != source:
                raise ValueError("Event source mismatch")
            canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            digest = hashlib.sha256(canonical.encode()).hexdigest()
            existing = db.execute(
                "SELECT id,current_version FROM economic_events WHERE source=? AND source_uid=?",
                (source, payload["source_uid"]),
            ).fetchone()
            if existing:
                event_id, version = existing["id"], existing["current_version"]
                prior = db.execute(
                    "SELECT content_hash FROM economic_event_versions WHERE event_id=? AND version=?",
                    (event_id, version),
                ).fetchone()
                if prior["content_hash"] == digest:
                    continue
                version += 1
                db.execute("UPDATE economic_events SET current_version=? WHERE id=?",
                           (version, event_id))
            else:
                event_id, version = new_id("evt"), 1
                db.execute(
                    "INSERT INTO economic_events(id,source,source_uid,current_version) VALUES(?,?,?,?)",
                    (event_id, source, payload["source_uid"], version),
                )
            db.execute(
                "INSERT INTO economic_event_versions(event_id,version,ingested_at,content_hash,payload) "
                "VALUES(?,?,?,?,?)",
                (event_id, version, utc_text(observed_at), digest, canonical),
            )
            count += 1
        db.execute(
            "INSERT INTO event_sources(source,status,checked_at,last_success_at,error_code) "
            "VALUES(?,'ok',?,?,NULL) ON CONFLICT(source) DO UPDATE SET "
            "status='ok',checked_at=EXCLUDED.checked_at,last_success_at=EXCLUDED.last_success_at,error_code=NULL",
            (source, utc_text(observed_at), utc_text(observed_at)),
        )
        db.commit()
    return count


def mark_source_offline(source: str, observed_at: datetime, code: str) -> None:
    with connect() as db:
        db.execute(
            "INSERT INTO event_sources(source,status,checked_at,error_code) VALUES(?,'offline',?,?) "
            "ON CONFLICT(source) DO UPDATE SET status='offline',checked_at=EXCLUDED.checked_at,"
            "error_code=EXCLUDED.error_code",
            (source, utc_text(observed_at), code[:80]),
        )
        db.commit()


def event_snapshot(cutoff: datetime) -> dict:
    if cutoff.tzinfo is None:
        raise ValueError("Event cutoff must include timezone")
    cutoff = cutoff.astimezone(UTC)
    start = cutoff.astimezone(EASTERN).date() - timedelta(days=30)
    end = cutoff.astimezone(EASTERN).date() + timedelta(days=7)
    with connect(readonly=True) as db:
        health = db.execute("SELECT * FROM event_sources").fetchall()
        rows = db.execute(
            "WITH ranked AS (SELECT e.id,v.version,v.payload,v.ingested_at,"
            "ROW_NUMBER() OVER (PARTITION BY e.id ORDER BY v.version DESC) AS rank "
            "FROM economic_events e JOIN economic_event_versions v ON v.event_id=e.id "
            "WHERE v.ingested_at<=?) SELECT id,version,payload,ingested_at FROM ranked "
            "WHERE rank=1 ORDER BY id", (utc_text(cutoff),),
        ).fetchall()
        fomc_results = fomc_results_as_of(db, cutoff)
        official_actuals, actual_source_status = actuals_as_of(db, cutoff)
    health = [decode_row(row, time_fields=("checked_at", "last_success_at")) for row in health]
    rows = [decode_row(row, json_fields=("payload",), time_fields=("ingested_at",)) for row in rows]
    by_source = {row["source"]: row for row in health}
    statuses = {}
    for source in SOURCES:
        row = by_source.get(source)
        healthy = (row and row["status"] == "ok" and row["last_success_at"]
                   and row["last_success_at"] <= cutoff
                   and cutoff - row["last_success_at"] <= timedelta(hours=24))
        statuses[source] = "ok" if healthy else "offline"
    events = []
    for row in rows:
        payload = row["payload"]
        day = date.fromisoformat(payload["scheduled_date"])
        if start <= day <= end:
            event = {"id": row["id"], "version": row["version"],
                     "ingested_at": row["ingested_at"].isoformat(), **payload}
            if payload["source"] == "fed" and payload["kind"] == "fomc":
                result = fomc_results.get(payload["scheduled_date"])
                if result:
                    event["official_result"] = result
                    event["actual_value"] = (f"{result['lower_pct']}–"
                                             f"{result['upper_pct']}%")
                    event["actual_unit"] = "target_range_percent"
            events.append(event)
    events.sort(key=lambda item: (item["scheduled_date"], item["scheduled_at"] or "", item["id"]))
    risk = "none"
    for event in events:
        scheduled = event["scheduled_at"]
        if scheduled:
            delta = cutoff - datetime.fromisoformat(scheduled)
            if timedelta(minutes=-30) <= delta <= timedelta(minutes=15):
                risk = "high_impact_window"
                break
        elif (event["scheduled_date"] == cutoff.astimezone(EASTERN).date().isoformat()
              and not event.get("official_result")):
            risk = "unknown_major_event"
    if all(value == "offline" for value in statuses.values()):
        status = "offline"
    elif any(value == "offline" for value in statuses.values()):
        status = "partial"
    else:
        status = "available" if events else "no_events"
    return {"status": status, "source_status": statuses, "events": events,
            "risk": risk, "cutoff": cutoff.isoformat(),
            "coverage": {"from": start.isoformat(), "through": end.isoformat()},
            "official_actuals": official_actuals,
            "actual_source_status": actual_source_status,
            "official_actual_gaps": unpublished_actuals(official_actuals),
            "actual_values_status": ("partial_official" if official_actuals else
                                     "partial_fomc" if any(
                                         event.get("official_result") for event in events)
                                     else "not_integrated"),
            "news_status": "not_integrated"}
