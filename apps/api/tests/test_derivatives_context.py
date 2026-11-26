from datetime import UTC, datetime, timedelta

import pytest

from trade_helper.derivatives_context import (
    SOURCE,
    VERSION,
    compact_derivatives,
    fetch_derivatives_context,
    summarize_open_interest,
    summarize_ratio,
    summarize_taker,
    validate_derivatives_context,
)

CUTOFF = datetime(2026, 9, 30, 12, tzinfo=UTC)
STEP = {'1h': 1, '4h': 4, '12h': 12, '1d': 24}


def rows(frame='1h'):
    step = {'1h': 1, '4h': 4, '12h': 12, '1d': 24}[frame]
    end = datetime(2026, 9, 30, 12, tzinfo=UTC)
    return [{'symbol': 'BTCUSDT', 'timestamp': int((end - timedelta(hours=24 - i * step)).timestamp() * 1000),
             'sumOpenInterest': str(100 + i * step), 'sumOpenInterestValue': '1000000'}
            for i in range(24 // step + 1)]


@pytest.mark.parametrize('frame', ['1h', '4h', '12h', '1d'])
def test_complete_open_interest_window_and_future_filter(frame):
    data = rows(frame)
    cutoff = datetime(2026, 9, 30, 12, tzinfo=UTC)
    future = data[-1] | {'timestamp': int((cutoff + timedelta(hours=4)).timestamp() * 1000), 'sumOpenInterest': '99999'}
    result = summarize_open_interest([*data, future], 'binance:perp:BTCUSDT', frame, cutoff)
    assert result['change_24h_pct'] == '24.00'
    assert result['open_interest_base_units'] == '124'
    assert 'observations' not in compact_derivatives({'timeframes': {frame: result}})['timeframes'][frame]


def test_missing_history_does_not_fabricate_change():
    data = rows()
    result = summarize_open_interest(data[1:], 'binance:perp:BTCUSDT', '1h', datetime(2026, 9, 30, 12, tzinfo=UTC))
    assert result['change_24h_pct'] is None
    assert result['change_window'] == 'insufficient_history'


def test_invalid_symbol_duplicate_and_staleness():
    cutoff = datetime(2026, 9, 30, 12, tzinfo=UTC)
    for data in [[rows()[0] | {'symbol': 'ETHUSDT'}], [*rows(), rows()[-1]]]:
        with pytest.raises(ValueError):
            summarize_open_interest(data, 'binance:perp:BTCUSDT', '1h', cutoff)
    assert summarize_open_interest(rows()[:-2], 'binance:perp:BTCUSDT', '1h', cutoff)['status'] == 'unavailable'


def test_provider_failure_is_optional(monkeypatch):
    import httpx
    monkeypatch.setenv('TRADE_DERIVATIVES_CONTEXT_ENABLED', '1')
    def unavailable(*args, **kwargs):
        raise httpx.ReadTimeout('no response')
    monkeypatch.setattr('trade_helper.derivatives_context.httpx.get', unavailable)
    result = fetch_derivatives_context('binance:perp:BTCUSDT', datetime.now(UTC))
    assert result['status'] == 'unavailable'
    assert list(result['timeframes']) == ['1h', '4h', '12h', '1d']


def ratio_rows(frame='1h', start=0.60, end=0.70):
    """One snapshot per period over the last day, the long share rising from start to end."""
    count = 24 // STEP[frame]
    return [{'symbol': 'BTCUSDT', 'timestamp': int((CUTOFF - timedelta(hours=24 - i * STEP[frame])).timestamp() * 1000),
             'longAccount': f'{start + (end - start) * i / count:.4f}',
             'shortAccount': f'{1 - start - (end - start) * i / count:.4f}', 'longShortRatio': '2.0000'}
            for i in range(count + 1)]


def taker_rows(frame='1h'):
    """Two days of finished periods (buying 60/40 in the latest day, 50/50 before) and one unfinished."""
    step = STEP[frame]
    starts = [CUTOFF - timedelta(hours=48 - i * step) for i in range(48 // step + 1)]
    recent = CUTOFF - timedelta(hours=24 + step)
    return [{'timestamp': int(at.timestamp() * 1000), 'buySellRatio': '1',
             'buyVol': '60' if at > recent else '50', 'sellVol': '40' if at > recent else '50'}
            for at in starts]


def positioning(frame='1h'):
    parts = {name: summarize_ratio(ratio_rows(frame), 'binance:perp:BTCUSDT', frame, CUTOFF)
             for name in ('global_accounts', 'top_trader_accounts', 'top_trader_positions')}
    return {'period': frame, 'status': 'available', **parts,
            'taker_volume': summarize_taker(taker_rows(frame), frame, CUTOFF)}


def test_long_share_now_and_a_day_before_without_later_snapshots():
    later = ratio_rows()[-1] | {'timestamp': int((CUTOFF + timedelta(hours=1)).timestamp() * 1000),
                                'longAccount': '0.9'}
    result = summarize_ratio([*ratio_rows(), later], 'binance:perp:BTCUSDT', '1h', CUTOFF)
    assert (result['long_share_pct'], result['long_share_24h_ago_pct']) == ('70.00', '60.00')
    assert len(result['observations']) == 25
    assert summarize_ratio(ratio_rows()[1:], 'binance:perp:BTCUSDT', '1h', CUTOFF)['long_share_24h_ago_pct'] is None
    assert summarize_ratio(ratio_rows()[:-3], 'binance:perp:BTCUSDT', '1h', CUTOFF)['status'] == 'unavailable'
    with pytest.raises(ValueError):
        summarize_ratio([ratio_rows()[0] | {'longAccount': '1.5'}], 'binance:perp:BTCUSDT', '1h', CUTOFF)


@pytest.mark.parametrize('frame', ['1h', '4h', '12h', '1d'])
def test_taker_volume_counts_only_finished_periods(frame):
    result = summarize_taker(taker_rows(frame), frame, CUTOFF)
    # The period starting at the cutoff has not finished and is left out.
    assert result['complete_through'] == CUTOFF.isoformat()
    assert (result['buy_share_24h_pct'], result['buy_share_prior_24h_pct']) == ('60.00', '50.00')
    assert result['taker_buy_base_units_24h'] == str(60 * 24 // STEP[frame])
    missing_day_before = taker_rows(frame)[24 // STEP[frame]:]
    assert summarize_taker(missing_day_before, frame, CUTOFF)['buy_share_prior_24h_pct'] is None


def test_report_summary_recomputed_from_as_of_observations():
    cutoff = CUTOFF
    frames = {frame: summarize_open_interest(rows(frame), 'binance:perp:BTCUSDT', frame, cutoff)
              for frame in ('1h', '4h', '12h', '1d')}
    context = {'version': VERSION, 'source_url': SOURCE, 'market_id': 'binance:perp:BTCUSDT',
               'as_of': cutoff.isoformat(), 'status': 'available', 'timeframes': frames,
               'primary_timeframe': '1h', 'analysis_timeframes': ['1h', '4h', '12h', '1d'],
               'positioning': positioning()}
    validate_derivatives_context(context, 'binance:perp:BTCUSDT', cutoff)
    compact = compact_derivatives(context)['positioning']
    assert 'observations' not in compact['global_accounts']
    assert compact['taker_volume']['buy_share_24h_pct'] == '60.00'
    context['positioning']['top_trader_positions']['long_share_pct'] = '99.00'
    with pytest.raises(ValueError, match='differs'):
        validate_derivatives_context(context, 'binance:perp:BTCUSDT', cutoff)
    context['positioning'] = positioning()
    frames['4h']['change_24h_pct'] = '100.00'
    with pytest.raises(ValueError, match='differs'):
        validate_derivatives_context(context, 'binance:perp:BTCUSDT', cutoff)


def test_reports_from_before_positioning_are_still_readable():
    context = {'version': 'derivatives_context_v2', 'source_url': SOURCE, 'market_id': 'binance:perp:BTCUSDT',
               'as_of': CUTOFF.isoformat(), 'status': 'available',
               'timeframes': {frame: summarize_open_interest(rows(frame), 'binance:perp:BTCUSDT', frame, CUTOFF)
                              for frame in ('1h', '4h', '12h', '1d')},
               'primary_timeframe': '1h', 'analysis_timeframes': ['1h', '4h', '12h', '1d']}
    validate_derivatives_context(context, 'binance:perp:BTCUSDT', CUTOFF)


def test_daily_analysis_marks_unsupported_oi_periods_without_network(monkeypatch):
    import httpx

    requested = []
    cutoff = datetime(2026, 9, 30, 12, tzinfo=UTC)

    def get(url, *, params, **_kwargs):
        endpoint = url.rsplit('/', 1)[-1]
        requested.append(endpoint)
        assert params['period'] == '1d'
        assert params['startTime'] <= int((cutoff - timedelta(hours=48)).timestamp() * 1000)
        body = (rows('1d') if endpoint == 'openInterestHist' else
                taker_rows('1d') if endpoint == 'takerlongshortRatio' else ratio_rows('1d'))
        return httpx.Response(200, json=body, request=httpx.Request('GET', url))

    monkeypatch.setenv('TRADE_DERIVATIVES_CONTEXT_ENABLED', '1')
    monkeypatch.setattr('trade_helper.derivatives_context.httpx.get', get)
    result = fetch_derivatives_context('binance:perp:BTCUSDT', cutoff, '1d')
    # Open interest once, for its only supported period; positioning for the primary period.
    assert sorted(requested) == ['globalLongShortAccountRatio', 'openInterestHist', 'takerlongshortRatio',
                                 'topLongShortAccountRatio', 'topLongShortPositionRatio']
    assert result['positioning']['status'] == 'available' and result['positioning']['period'] == '1d'
    assert result['status'] == 'partial'
    assert result['analysis_timeframes'] == ['1d', '3d', '1w', '1M']
    for frame in ('3d', '1w', '1M'):
        assert result['timeframes'][frame]['reason'] == 'unsupported_source_period'
        assert result['timeframes'][frame]['status'] == 'unavailable'
    validate_derivatives_context(result, 'binance:perp:BTCUSDT', cutoff)


def test_old_frozen_two_frame_oi_context_is_still_readable():
    cutoff = datetime(2026, 9, 30, 12, tzinfo=UTC)
    context = {'version': 'derivatives_context_v1', 'source_url': SOURCE,
               'market_id': 'binance:perp:BTCUSDT', 'as_of': cutoff.isoformat(),
               'status': 'available', 'timeframes': {
                   frame: summarize_open_interest(rows(frame), 'binance:perp:BTCUSDT', frame, cutoff)
                   for frame in ('1h', '4h')}}
    validate_derivatives_context(context, 'binance:perp:BTCUSDT', cutoff)
