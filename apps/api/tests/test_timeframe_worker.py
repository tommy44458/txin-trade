from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from trade_helper import worker
from trade_helper.agent import fallback_analysis
from trade_helper.api import app
from trade_helper.timeframes import analysis_timeframes, higher_timeframes

from .test_analysis import sample_candles


@pytest.mark.parametrize('timeframe', ['1h', '4h', '12h', '1d'])
def test_worker_freezes_selected_frame_and_three_independent_higher_series(monkeypatch, timeframe):
    frames = analysis_timeframes(timeframe)
    rows = {frame: sample_candles(recent=True, timeframe=frame) for frame in frames}
    fetched = []
    model_calls = []

    def fetch(_market, frame, *, limit):
        fetched.append(frame)
        assert limit == 1000
        return rows[frame]

    def higher(_market, primary, *, context_candles):
        assert primary == timeframe
        assert context_candles is rows[frames[1]]
        return {frame: {'candles': rows[frame]} for frame in higher_timeframes(primary)}

    def analyze(request, candles, quote, context_candles, positions, *, prepared_trace, prompt_bundle, on_text=None):
        assert prompt_bundle.response_locale == request['output_locale'] == 'zh-TW'
        model_calls.append(request['timeframe'])
        snapshot = prepared_trace[0]['result']
        assert snapshot['analysis_timeframes'] == list(frames)
        assert list(snapshot['timeframes']) == list(frames)
        assert all(data['status'] == 'available' for data in snapshot['timeframes'].values())
        assert quote['higher_timeframe_candles'][frames[1]]['candles'] is context_candles
        decision = fallback_analysis(request, candles, quote, context_candles=context_candles)
        decision['mode'] = 'openai_assisted'
        decision['tool_trace'] = prepared_trace
        return decision

    monkeypatch.setattr(worker, 'fetch_candles', fetch)
    monkeypatch.setattr(worker, 'fetch_higher_timeframe_candles', higher)
    monkeypatch.setattr(worker, 'fetch_quote', lambda _: {
        'price': rows[timeframe][-1]['close'], 'mark_price': rows[timeframe][-1]['close'],
        'observed_at': datetime.now(UTC).isoformat()})
    monkeypatch.setattr(worker, 'fetch_forming_candle', lambda *_: None)
    monkeypatch.setattr(worker, 'fetch_order_book', lambda _: None)
    monkeypatch.setattr(worker, 'fetch_tick_size', lambda _: Decimal('.1'))
    monkeypatch.setattr(worker, 'analyze_with_tools', analyze)
    with TestClient(app) as client:
        response = client.post('/api/v1/analyses', json={
            'kind': 'market', 'market_id': 'binance:perp:BTCUSDT', 'timeframe': timeframe},
            headers={'Idempotency-Key': 'four-frames-' + timeframe})
        assert response.status_code == 202
        assert worker.run_once()
        result = client.get('/api/v1/analyses/' + response.json()['id']).json()
        assert result['status'] == 'completed', result.get('error')
        assert result['report']['timeframe'] == timeframe
        assert result['report']['technical_snapshot']['context_timeframes'] == list(frames[1:])
        assert result['v4_status'] == ('queued' if timeframe in {'1h', '4h'} else 'not_requested')
    assert fetched == list(frames[:2])
    assert model_calls == [timeframe]
