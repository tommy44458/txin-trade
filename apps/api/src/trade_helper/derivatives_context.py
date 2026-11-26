"""As-of Binance futures statistics: open interest per period, and for the primary
period the long/short account ratios and taker buy/sell volume. They describe
participation and positioning on one exchange, never a trade direction by themselves."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import httpx

from .market import BASE_URL, symbol_for
from .timeframes import analysis_timeframes

VERSION = 'derivatives_context_v3'
OI_HOURS = {'1h': 1, '4h': 4, '12h': 12, '1d': 24}
DAY_MS = 86_400_000
# Binance keeps these statistics for 30 days and answers for the primary period only.
RATIO_SOURCES = {
    'global_accounts': 'globalLongShortAccountRatio',
    'top_trader_accounts': 'topLongShortAccountRatio',
    'top_trader_positions': 'topLongShortPositionRatio',
}
TAKER_SOURCE = 'takerlongshortRatio'
INTERPRETATION_LIMIT = (
    'OI is outstanding contracts, not net long/short intent. Long/short ratios are Binance shares of '
    'accounts (all accounts, or the top 20% by margin) or of top-trader position size, not the whole '
    'market. Taker volume counts market orders that bought or sold on Binance. Liquidations are in '
    'liquidation_context.')
SOURCE = 'https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data'


def summarize_open_interest(rows: list[dict], market_id: str, timeframe: str,
                            cutoff: datetime) -> dict:
    if timeframe not in OI_HOURS:
        raise ValueError('Unsupported open interest timeframe')
    symbol = symbol_for(market_id)
    hours = OI_HOURS[timeframe]
    if cutoff.tzinfo is None or not isinstance(rows, list):
        raise ValueError('Invalid open interest input')
    cutoff_ms = int(cutoff.timestamp() * 1000)
    values = []
    for row in rows:
        oi, notional = Decimal(str(row['sumOpenInterest'])), Decimal(str(row['sumOpenInterestValue']))
        stamp = int(row['timestamp'])
        if row['symbol'] != symbol or not oi.is_finite() or not notional.is_finite() or notional < 0 or oi <= 0:
            raise ValueError('Invalid open interest observation')
        if stamp <= cutoff_ms:
            values.append((stamp, oi, notional))
    values.sort()
    if not values:
        return {'status': 'unavailable', 'reason': 'no_as_of_observation'}
    stamps = [v[0] for v in values]
    if len(set(stamps)) != len(stamps):
        raise ValueError('Duplicate open interest observation')
    last = values[-1]
    if cutoff_ms - last[0] > (hours * 3600 + 900) * 1000:
        return {'status': 'unavailable', 'reason': 'stale_observations'}
    start_ms = last[0] - 86_400_000
    window = [v for v in values if v[0] >= start_ms]
    complete = (window[0][0] == start_ms and len(window) == 24 // hours + 1 and
                all(b[0] - a[0] == hours * 3_600_000 for a, b in pairwise(window)))
    return {'status': 'available', 'timeframe': timeframe,
            'observed_at': datetime.fromtimestamp(last[0] / 1000, UTC).isoformat(),
            'open_interest_base_units': str(last[1]), 'open_interest_value_usdt': str(last[2]),
            'change_24h_pct': str(((last[1] / window[0][1] - 1) * 100).quantize(Decimal('.01'))) if complete else None,
            'change_window': 'complete_24h' if complete else 'insufficient_history',
            'observations': [{'timestamp': t, 'sumOpenInterest': str(oi), 'sumOpenInterestValue': str(n)} for t, oi, n in window]}


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat()


def _pct(share: Decimal) -> str:
    return str((share * 100).quantize(Decimal('.01')))


def _fresh(cutoff_ms: int, last_ms: int, timeframe: str) -> bool:
    return cutoff_ms - last_ms <= (OI_HOURS[timeframe] * 3600 + 900) * 1000


def summarize_ratio(rows: list[dict], market_id: str, timeframe: str, cutoff: datetime) -> dict:
    """The latest long account share before the cutoff, and the share one day earlier."""
    symbol = symbol_for(market_id)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    values = []
    for row in rows:
        stamp = int(row['timestamp'])
        share, ratio = Decimal(str(row['longAccount'])), Decimal(str(row['longShortRatio']))
        if (row['symbol'] != symbol or not share.is_finite() or not ratio.is_finite()
                or not 0 <= share <= 1 or ratio < 0):
            raise ValueError('Invalid long/short observation')
        if stamp <= cutoff_ms:
            values.append((stamp, share, ratio))
    values.sort()
    if not values:
        return {'status': 'unavailable', 'reason': 'no_as_of_observation'}
    if len({v[0] for v in values}) != len(values):
        raise ValueError('Duplicate long/short observation')
    last = values[-1]
    if not _fresh(cutoff_ms, last[0], timeframe):
        return {'status': 'unavailable', 'reason': 'stale_observations'}
    before = next((v for v in values if v[0] == last[0] - DAY_MS), None)
    return {'status': 'available', 'observed_at': _iso(last[0]),
            'long_share_pct': _pct(last[1]), 'long_short_ratio': str(last[2]),
            'long_share_24h_ago_pct': _pct(before[1]) if before else None,
            'observations': [{'timestamp': t, 'longAccount': str(a), 'longShortRatio': str(r)}
                             for t, a, r in values if t >= last[0] - DAY_MS]}


def summarize_taker(rows: list[dict], timeframe: str, cutoff: datetime) -> dict:
    """Taker buy and sell volume over the last day of finished periods, and the day before."""
    period = OI_HOURS[timeframe] * 3_600_000
    cutoff_ms = int(cutoff.timestamp() * 1000)
    values = []
    for row in rows:
        stamp = int(row['timestamp'])
        buy, sell = Decimal(str(row['buyVol'])), Decimal(str(row['sellVol']))
        if not buy.is_finite() or not sell.is_finite() or buy < 0 or sell < 0:
            raise ValueError('Invalid taker volume observation')
        # A row covers the period starting at its timestamp; only finished periods count.
        if stamp + period <= cutoff_ms:
            values.append((stamp, buy, sell))
    values.sort()
    if not values:
        return {'status': 'unavailable', 'reason': 'no_as_of_observation'}
    if len({v[0] for v in values}) != len(values):
        raise ValueError('Duplicate taker volume observation')
    last = values[-1]
    if not _fresh(cutoff_ms, last[0] + period, timeframe):
        return {'status': 'unavailable', 'reason': 'stale_observations'}
    recent = [v for v in values if v[0] > last[0] - DAY_MS]
    prior = [v for v in values if last[0] - 2 * DAY_MS < v[0] <= last[0] - DAY_MS]

    def share(part):
        buy, sell = sum(v[1] for v in part), sum(v[2] for v in part)
        complete = len(part) == DAY_MS // period and buy + sell > 0
        return _pct(buy / (buy + sell)) if complete else None
    return {'status': 'available', 'complete_through': _iso(last[0] + period),
            'taker_buy_base_units_24h': str(sum(v[1] for v in recent)),
            'taker_sell_base_units_24h': str(sum(v[2] for v in recent)),
            'buy_share_24h_pct': share(recent), 'buy_share_prior_24h_pct': share(prior),
            'observations': [{'timestamp': t, 'buyVol': str(b), 'sellVol': str(v)}
                             for t, b, v in prior + recent]}


def _positioning_status(parts: dict) -> str:
    count = sum(part['status'] == 'available' for part in parts.values())
    return 'available' if count == len(parts) else 'partial' if count else 'unavailable'


def fetch_derivatives_context(market_id: str, cutoff: datetime, timeframe: str = '1h') -> dict:
    frames = analysis_timeframes(timeframe)
    context = {'version': VERSION, 'market_id': market_id, 'as_of': cutoff.isoformat(),
               'source_url': SOURCE, 'timeframes': {}, 'primary_timeframe': timeframe,
               'analysis_timeframes': list(frames),
               'interpretation_limit': INTERPRETATION_LIMIT}
    if os.getenv('TRADE_DERIVATIVES_CONTEXT_ENABLED', '1') != '1':
        return context | {'status': 'unavailable', 'reason': 'disabled'}

    def fetch(timeframe):
        if timeframe not in OI_HOURS:
            return {'status': 'unavailable', 'timeframe': timeframe,
                    'reason': 'unsupported_source_period'}
        try:
            response = httpx.get(f'{BASE_URL}/futures/data/openInterestHist', params={
                'symbol': symbol_for(market_id), 'period': timeframe,
                # Include the preceding 24h observation even for daily periods
                # when the latest available observation is almost one day old.
                'limit': 40,
                'startTime': int((cutoff - timedelta(hours=max(30, 25 + OI_HOURS[timeframe]))).timestamp() * 1000),
                'endTime': int(cutoff.timestamp() * 1000)}, timeout=6)
            response.raise_for_status()
            return summarize_open_interest(response.json(), market_id, timeframe, cutoff)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, ArithmeticError):
            return {'status': 'unavailable', 'reason': 'source_unavailable_or_invalid'}
    def statistic(endpoint, summarize, hours):
        try:
            response = httpx.get(f'{BASE_URL}/futures/data/{endpoint}', params={
                'symbol': symbol_for(market_id), 'period': timeframe, 'limit': 100,
                'startTime': int((cutoff - timedelta(hours=hours + OI_HOURS[timeframe])).timestamp() * 1000),
                'endTime': int(cutoff.timestamp() * 1000)}, timeout=6)
            response.raise_for_status()
            return summarize(response.json())
        except (httpx.HTTPError, ValueError, KeyError, TypeError, ArithmeticError):
            return {'status': 'unavailable', 'reason': 'source_unavailable_or_invalid'}

    with ThreadPoolExecutor(max_workers=8) as pool:
        open_interest = pool.map(fetch, frames)
        ratios = {name: pool.submit(statistic, endpoint,
                                    lambda rows: summarize_ratio(rows, market_id, timeframe, cutoff), 25)
                  for name, endpoint in RATIO_SOURCES.items()}
        taker = pool.submit(statistic, TAKER_SOURCE, lambda rows: summarize_taker(rows, timeframe, cutoff), 49)
        context['timeframes'] = dict(zip(frames, open_interest, strict=True))
        parts = {name: future.result() for name, future in ratios.items()} | {'taker_volume': taker.result()}
    context['positioning'] = {'period': timeframe, 'status': _positioning_status(parts), **parts}
    count = sum(v['status'] == 'available' for v in context['timeframes'].values())
    return context | {'status': 'available' if count == len(frames) else 'partial' if count else 'unavailable'}


def _without_observations(data):
    return {key: value for key, value in data.items() if key != 'observations'} if isinstance(data, dict) else data


def compact_derivatives(context: dict | None) -> dict | None:
    if context is None:
        return None
    compact = context | {'timeframes': {frame: _without_observations(data)
                                        for frame, data in context.get('timeframes', {}).items()}}
    if 'positioning' in context:
        compact['positioning'] = {key: _without_observations(value)
                                  for key, value in context['positioning'].items()}
    return compact


def _validate_positioning(positioning, market_id: str, cutoff: datetime, timeframe: str) -> None:
    if not isinstance(positioning, dict) or positioning.get('period') != timeframe:
        raise ValueError('Invalid positioning period')
    if set(positioning) != {'period', 'status', *RATIO_SOURCES, 'taker_volume'}:
        raise ValueError('Invalid positioning sources')
    symbol = symbol_for(market_id)
    for name in (*RATIO_SOURCES, 'taker_volume'):
        result = positioning[name]
        if result.get('status') == 'available':
            rows = result.get('observations', [])
            again = (summarize_taker(rows, timeframe, cutoff) if name == 'taker_volume' else
                     summarize_ratio([row | {'symbol': symbol} for row in rows], market_id, timeframe, cutoff))
            if again != result:
                raise ValueError('Positioning summary differs from observations')
        elif result.get('status') != 'unavailable' or result.get('observations'):
            raise ValueError('Invalid positioning availability')
    parts = {name: positioning[name] for name in (*RATIO_SOURCES, 'taker_volume')}
    if positioning['status'] != _positioning_status(parts):
        raise ValueError('Invalid positioning status')


def validate_derivatives_context(context: dict, market_id: str, cutoff: datetime) -> None:
    if (context.get('version') not in {VERSION, 'derivatives_context_v2', 'derivatives_context_v1'}
            or context.get('market_id') != market_id or
            context.get('source_url') != SOURCE or context.get('as_of') != cutoff.isoformat()):
        raise ValueError('Derivatives context differs from market snapshot')
    frames = context.get('timeframes', {})
    expected_frames = {'1h', '4h'}
    if context.get('version') in {VERSION, 'derivatives_context_v2'}:
        primary = context.get('primary_timeframe')
        expected_order = list(analysis_timeframes(primary))
        if context.get('analysis_timeframes') != expected_order:
            raise ValueError('Invalid derivatives analysis timeframes')
        expected_frames = set(expected_order)
    if context.get('reason') == 'disabled' and not frames and context.get('status') == 'unavailable':
        return
    if context.get('version') == VERSION:
        _validate_positioning(context.get('positioning'), market_id, cutoff, context.get('primary_timeframe'))
    if set(frames) != expected_frames:
        raise ValueError('Invalid derivatives timeframes')
    count = 0
    for frame, result in frames.items():
        if result.get('status') == 'available':
            if frame not in OI_HOURS:
                raise ValueError('Unsupported open interest period cannot be available')
            observations = result.get('observations', [])
            source_rows = [row | {'symbol': symbol_for(market_id)} for row in observations]
            if summarize_open_interest(source_rows, market_id, frame, cutoff) != result:
                raise ValueError('Open interest summary differs from observations')
            count += 1
        elif result.get('status') != 'unavailable' or result.get('observations'):
            raise ValueError('Invalid open interest availability')
    expected = 'available' if count == len(expected_frames) else 'partial' if count else 'unavailable'
    if context.get('status') != expected:
        raise ValueError('Invalid derivatives availability')
