from datetime import UTC, datetime, timedelta

import pytest

from trade_helper.db import connect, init_db
from trade_helper.events import event_snapshot
from trade_helper.macro_actuals import (
    BLS_SERIES,
    actuals_as_of,
    bea_release_urls,
    parse_bea_release,
    parse_bls_response,
    save_actuals,
    validate_snapshot_actual,
)
from trade_helper.macro_context import build_macro_context


def bls_response():
    monthly = {
        "CUSR0000SA0": {"2025-07": "300", "2025-08": "301", "2026-06": "310", "2026-07": "311", "2026-08": "312"},
        "CUUR0000SA0": {"2025-07": "300", "2025-08": "301", "2026-07": "310", "2026-08": "311"},
        "CUUR0000SA0L1E": {"2025-07": "300", "2025-08": "301", "2026-07": "305", "2026-08": "306"},
        "CES0000000001": {"2026-06": "150000", "2026-07": "150100", "2026-08": "150250"},
        "LNS14000000": {"2026-07": "4.0", "2026-08": "4.1"},
    }
    return {"status": "REQUEST_SUCCEEDED", "Results": {"series": [
        {"seriesID": series, "data": [
            {"year": period[:4], "period": "M" + period[5:], "value": value}
            for period, value in observations.items()]
         + ([{"year": "2025", "period": "M10", "value": "-"}]
            if series != "CES0000000001" else [])}
        for series, observations in monthly.items()]}}


BEA_INDEX = '''<h1>Releases</h1><a href="/news/2026/personal-income-and-outlays-july-2026">Personal Income and Outlays, July 2026</a>
<a href="/news/2026/gdp-second-estimate-and-corporate-profits-2nd-quarter-2026">GDP (Second Estimate) and Corporate Profits, 2nd Quarter 2026</a>'''
PCE = '''<p>EMBARGOED UNTIL RELEASE AT 8:30 a.m. EDT, Wednesday, August 26, 2026</p>
<h1>Personal Income and Outlays, July 2026</h1>
<p>From the same month one year ago, the PCE price index for July increased 3.7 percent.
Excluding food and energy, the PCE price index increased 3.3 percent from one year ago.</p>'''
GDP = '''<p>EMBARGOED UNTIL RELEASE AT 8:30 a.m. EDT, Wednesday, August 26, 2026</p>
<h1>GDP (Second Estimate), 2nd Quarter 2026</h1>
<p>Real gross domestic product (GDP) increased at an annual rate of 1.5 percent in the second quarter of 2026, according to the second estimate.</p>'''


def test_official_parsers_keep_metric_period_units_and_release_time():
    assert set(BLS_SERIES) == {"CUSR0000SA0", "CUUR0000SA0", "CUUR0000SA0L1E",
                               "CES0000000001", "LNS14000000"}
    actuals = {item["metric"]: item for item in parse_bls_response(bls_response())}
    assert actuals["payroll_change"]["value"] == "150"
    assert actuals["payroll_change"]["unit"] == "thousand_jobs"
    assert actuals["unemployment_rate"]["value"] == "4.1"
    assert actuals["cpi_mom"]["value"] == "0.3"
    assert actuals["cpi_yoy"]["period"] == "2026-08"
    assert actuals["cpi_yoy"]["published_at"] is None
    urls = bea_release_urls(BEA_INDEX)
    pce = parse_bea_release(PCE, urls["pce"], "pce")
    gdp = parse_bea_release(GDP, urls["gdp"], "gdp")
    assert [(row["metric"], row["value"]) for row in pce] == [
        ("pce_yoy", "3.7"), ("core_pce_yoy", "3.3")]
    assert gdp[0]["period"] == "2026-Q2"
    assert gdp[0]["value"] == "1.5"
    assert all(row["published_at"] == "2026-08-26T12:30:00+00:00" for row in pce + gdp)
    with pytest.raises(ValueError, match="PCE annual actuals"):
        parse_bea_release(PCE.replace("3.3 percent", "unknown"), urls["pce"], "pce")


@pytest.mark.usefixtures("sqlite_db")
def test_actuals_are_versioned_as_of_and_stale_bea_values_are_labeled_background():
    init_db()
    first = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    bls = parse_bls_response(bls_response())
    urls = bea_release_urls(BEA_INDEX)
    bea = parse_bea_release(PCE, urls["pce"], "pce") + parse_bea_release(GDP, urls["gdp"], "gdp")
    assert save_actuals("bls", bls, first) == 5
    assert save_actuals("bea", bea, first) == 3
    assert save_actuals("bea", bea, first + timedelta(seconds=1)) == 0
    assert event_snapshot(first - timedelta(seconds=1))["official_actuals"] == []
    snapshot = event_snapshot(first + timedelta(seconds=1))
    assert len(snapshot["official_actuals"]) == 8
    assert snapshot["actual_values_status"] == "partial_official"
    macro = build_macro_context(first + timedelta(seconds=1), snapshot, None)
    assert {item["kind"] for item in macro["directional_evidence"]} == {"cpi", "employment"}
    assert set(macro["missing_actuals"]) == {"pce", "gdp"}
    assert {item["kind"] for item in macro["latest_actuals"]} == {"cpi", "employment", "pce", "gdp"}
    assert all(not item["within_month"] for item in macro["latest_actuals"]
               if item["kind"] in {"pce", "gdp"})
    revised = [row | {"value": "4.2"} if row["metric"] == "unemployment_rate" else row
               for row in bls]
    assert save_actuals("bls", revised, first + timedelta(hours=1)) == 1
    with connect() as db:
        before, _ = actuals_as_of(db, first + timedelta(minutes=30))
        after, _ = actuals_as_of(db, first + timedelta(hours=2))
    assert next(item for item in before if item["metric"] == "unemployment_rate")["value"] == "4.1"
    updated = next(item for item in after if item["metric"] == "unemployment_rate")
    assert updated["value"] == "4.2" and updated["version"] == 2
    validate_snapshot_actual(updated, first + timedelta(hours=2))
    with pytest.raises(ValueError, match="content hash"):
        validate_snapshot_actual(updated | {"value": "7.9"}, first + timedelta(hours=2))
    with pytest.raises(ValueError, match="after cutoff"):
        validate_snapshot_actual(updated, first + timedelta(minutes=30))
    with pytest.raises(ValueError, match="Future macro observation period"):
        validate_snapshot_actual(updated | {"period": "2027-01"}, first + timedelta(hours=2))


@pytest.mark.usefixtures("sqlite_db")
def test_report_rejects_tampered_or_future_macro_actual():
    from trade_helper.agent import fallback_analysis
    from trade_helper.analysis import build_report
    from trade_helper.report_contract import validate_report

    from .test_analysis import sample_candles

    init_db()
    now = datetime.now(UTC)
    save_actuals("bls", parse_bls_response(bls_response()), now - timedelta(minutes=1))
    events = event_snapshot(now)
    request = {"market_id": "binance:perp:BTCUSDT", "timeframe": "1h", "leverage": 5}
    quote = {"price": "120", "tick_size": "0.1", "observed_at": now.isoformat(),
             "snapshot_hash": "a" * 64, "event_risk": events["risk"],
             "events_status": events["status"]}
    candles = sample_candles(recent=True)
    report = build_report(request, candles, quote, [], fallback_analysis(request, candles, quote),
                          events=events)
    assert report["macro_context"]["directional_evidence"]
    validate_report(report)
    report["event_context"]["official_actuals"][0]["value"] = "99.9"
    with pytest.raises(ValueError, match="content hash"):
        validate_report(report)


@pytest.mark.usefixtures("sqlite_db")
def test_sync_keeps_healthy_bls_when_bea_fetch_fails():
    from trade_helper.macro_actual_sync import sync_once

    init_db()
    result = sync_once(lambda: bls_response(), lambda _url: "temporary failure")
    assert result["bls"]["status"] == "ok"
    assert result["bea"]["status"] == "offline"
    snapshot = event_snapshot(datetime.now(UTC))
    assert snapshot["actual_source_status"] == {"bls": "ok", "bea": "offline"}
    assert {item["kind"] for item in snapshot["official_actuals"]} == {"cpi", "employment"}


def test_bea_headline_survives_inline_markup_around_gdp():
    urls = bea_release_urls(BEA_INDEX)
    marked_up = GDP.replace("(GDP)", "(<abbr title='gross domestic product'>GDP</abbr> )")
    assert parse_bea_release(marked_up, urls["gdp"], "gdp")[0]["value"] == "1.5"


@pytest.mark.usefixtures("sqlite_db")
def test_one_failed_bea_release_keeps_the_other_and_marks_the_source_partial():
    from trade_helper.macro_actual_sync import sync_once

    init_db()
    pages = {"https://www.bea.gov/news/current-releases": BEA_INDEX,
             "https://www.bea.gov/news/2026/personal-income-and-outlays-july-2026": PCE,
             "https://www.bea.gov/news/2026/gdp-second-estimate-and-corporate-profits-2nd-quarter-2026":
                 GDP.replace("Real gross domestic product", "Output")}
    result = sync_once(lambda: bls_response(), lambda url: pages[url])
    assert result["bea"] == {"status": "partial", "parsed": 2, "changed": 2, "failed": ["gdp:ValueError"]}
    snapshot = event_snapshot(datetime.now(UTC))
    assert snapshot["actual_source_status"]["bea"] == "partial"
    with connect(readonly=True) as db:
        saved = {row["metric"] for row in db.execute(
            "SELECT metric FROM macro_actual_versions WHERE source='bea'").fetchall()}
    assert saved == {"pce_yoy", "core_pce_yoy"}


def test_an_annual_cpi_change_missing_from_the_latest_release_is_reported_unpublished():
    from trade_helper.macro_actuals import unpublished_actuals

    def actual(metric, period, value="0.4"):
        return {"source": "bls", "metric": metric, "period": period, "value": value,
                "source_url": "https://api.bls.gov/publicAPI/v1/timeseries/data/"}

    latest = [actual("cpi_mom", "2026-10"), actual("cpi_yoy", "2026-09", "3.1"),
              actual("payroll_change", "2026-10", "165")]
    gaps = unpublished_actuals(latest)
    assert [(gap["metric"], gap["period"], gap["comparison_period"]) for gap in gaps] == [
        ("cpi_yoy", "2026-10", "2025-10"), ("core_cpi_yoy", "2026-10", "2025-10")]
    assert gaps[0]["previous"] == {"period": "2026-09", "value": "3.1"} and gaps[1]["previous"] is None
    assert gaps[0]["reason"] == "comparison_period_not_published"
    assert unpublished_actuals([actual("cpi_mom", "2026-11"), actual("cpi_yoy", "2026-11"),
                                actual("core_cpi_yoy", "2026-11")]) == []
    assert unpublished_actuals([actual("payroll_change", "2026-10")]) == []
