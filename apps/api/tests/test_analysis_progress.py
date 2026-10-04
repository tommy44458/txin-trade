from trade_helper.analysis_progress import ProgressRecorder, draft_fields, evidence_progress
from trade_helper.technical_snapshot import prepare_analysis_evidence

from .test_technical_snapshot import fixture


def test_sections_are_read_from_a_report_still_being_written():
    text = '```json\n{"market": "BTC 在 \\"關鍵\\" 區間\\n上方", "levels": "支撐 118\\u00'
    assert draft_fields(text) == {
        "market": {"text": 'BTC 在 "關鍵" 區間\n上方', "complete": True},
        # An escape still being written is left out until it is complete.
        "levels": {"text": "支撐 118", "complete": False},
    }
    assert draft_fields('{"market_id": "x", "strategy": ""}') == {}
    assert draft_fields("not json at all") == {}


def test_progress_lists_the_trend_and_the_nearest_active_zones():
    request, candles, context, quote = fixture()
    trace = prepare_analysis_evidence(request, candles, quote, context, [])
    evidence = evidence_progress(request, trace)
    assert evidence["trend"] and evidence["atr14"]
    assert len(evidence["support"]) <= 3 and len(evidence["resistance"]) <= 3
    assert all(set(zone) == {"low", "high"} for zone in evidence["support"] + evidence["resistance"])


def test_draft_writes_are_throttled_but_a_finished_section_is_written_at_once(monkeypatch):
    writes = []
    recorder = ProgressRecorder("analysis-x")
    monkeypatch.setattr(recorder, "_write", lambda: writes.append(dict(recorder.progress["draft"])))
    recorder.model_text('{"market": "上')
    recorder.model_text('{"market": "上漲')  # Within a second: skipped.
    recorder.model_text('{"market": "上漲中", "levels": "支')  # A section finished: written.
    assert [list(item) for item in writes] == [["market"], ["market", "levels"]]
    assert writes[-1]["market"] == {"text": "上漲中", "complete": True}


def test_a_failed_progress_write_never_reaches_the_analysis(monkeypatch):
    def broken():
        raise RuntimeError("database is locked")

    monkeypatch.setattr("trade_helper.analysis_progress.connect", broken)
    ProgressRecorder("analysis-x").record(market={"price": "1"})
