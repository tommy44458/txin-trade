"""BTC and ETH direction (SPY and QQQ for a US stock) as market-wide risk context for the analyzed pair.

Crypto pairs often move with the two largest markets, so each analysis carries
their closed-candle trend and structure, plus how closely the analyzed pair
has actually moved with them. The measured linkage, not an assumption, decides
how much weight the model may give this context.
"""

import math
import os
import statistics
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from itertools import pairwise

import httpx

from .higher_timeframes import _structure, calculate_context_metrics
from .market import fetch_candles
from .market_catalog import is_us_equity
from .strategy_engine import other_timeframe
from .timeframes import advance_candle, closed_history_is_stale

VERSION = 'market_reference_v1'
REFERENCE_MARKETS = ('binance:perp:BTCUSDT', 'binance:perp:ETHUSDT')
# A stock is read against the broad US market and its large technology names.
EQUITY_REFERENCE_MARKETS = ('binance:perp:SPYUSDT', 'binance:perp:QQQUSDT')
REFERENCE_CANDLES = 300
CORRELATION_WINDOW = 100
MIN_ALIGNED_RETURNS = 30
INTERPRETATION_LIMIT = ('Correlation and beta describe the stated closed-candle window only; '
                        'they are not causality and do not predict future co-movement.')


def _returns(rows: list[dict]) -> dict[str, float]:
    """Log returns keyed by the later candle's open time, across consecutive candles only."""
    returns = {}
    for previous, current in pairwise(rows):
        before, after = float(previous['close']), float(current['close'])
        if before > 0 and after > 0:
            returns[current['open_time']] = math.log(after / before)
    return returns


def linkage(pair_rows: list[dict], reference_rows: list[dict], timeframe: str,
            window: int = CORRELATION_WINDOW) -> dict:
    """Return correlation and beta of the pair against a reference over aligned closed candles."""
    def consecutive(rows):
        return [row for previous, row in pairwise(rows)
                if datetime.fromisoformat(row['open_time']) ==
                advance_candle(datetime.fromisoformat(previous['open_time']), timeframe)]
    pair, reference = _returns(pair_rows), _returns(reference_rows)
    pair_times = {row['open_time'] for row in consecutive(pair_rows)}
    reference_times = {row['open_time'] for row in consecutive(reference_rows)}
    shared = sorted(pair.keys() & reference.keys() & pair_times & reference_times)[-window:]
    if len(shared) < MIN_ALIGNED_RETURNS:
        return {'correlation': None, 'beta': None, 'aligned_returns': len(shared),
                'linkage': 'unknown', 'reason': 'insufficient_overlap'}
    x = [reference[key] for key in shared]
    y = [pair[key] for key in shared]
    if statistics.pvariance(x) == 0 or statistics.pvariance(y) == 0:
        return {'correlation': None, 'beta': None, 'aligned_returns': len(shared),
                'linkage': 'unknown', 'reason': 'flat_returns'}
    correlation = statistics.correlation(x, y)
    beta = statistics.covariance(x, y) / statistics.variance(x)
    strength = 'strong' if abs(correlation) >= 0.7 else 'moderate' if abs(correlation) >= 0.4 else 'weak'
    return {'correlation': f'{correlation:.2f}', 'beta': f'{beta:.2f}', 'aligned_returns': len(shared),
            'window_start_at': shared[0], 'window_end_at': shared[-1], 'linkage': strength}


def summarize_reference(rows: list[dict], pair_rows: list[dict], timeframe: str,
                        cutoff: datetime) -> dict:
    rows = [row for row in rows if datetime.fromisoformat(row['close_time']) <= cutoff]
    if len(rows) < 60:
        return {'status': 'unavailable', 'timeframe': timeframe, 'reason': 'insufficient_closed_candles',
                'candle_count': len(rows)}
    if closed_history_is_stale(rows, timeframe, cutoff):
        return {'status': 'unavailable', 'timeframe': timeframe, 'reason': 'stale_closed_candles'}
    metrics = calculate_context_metrics(rows, timeframe)
    window = rows[-(CORRELATION_WINDOW + 1):]
    change = (Decimal(window[-1]['close']) / Decimal(window[0]['close']) - 1) * 100
    return {'status': 'available', 'timeframe': timeframe, 'trend': metrics['trend'],
            'structure': _structure(rows)['state'], 'last_close': metrics['last_close'],
            'ma20': metrics['ma20'], 'ma50': metrics['ma50'],
            'last_candle_at': metrics['last_candle_at'],
            'change_pct': str(change.quantize(Decimal('.01'))), 'change_candles': len(window) - 1,
            **linkage(pair_rows, rows, timeframe)}


def fetch_market_reference(market_id: str, timeframe: str, candles: list[dict],
                           context_candles: list[dict], cutoff: datetime) -> dict:
    frames = (timeframe, other_timeframe(timeframe))
    pair_rows = {timeframe: candles, frames[1]: context_candles}
    references = EQUITY_REFERENCE_MARKETS if is_us_equity(market_id) else REFERENCE_MARKETS
    context = {'version': VERSION, 'market_id': market_id, 'as_of': cutoff.isoformat(),
               'timeframes': list(frames), 'references': {},
               'correlation_window_candles': CORRELATION_WINDOW,
               'interpretation_limit': INTERPRETATION_LIMIT}
    if os.getenv('TRADE_MARKET_REFERENCE_ENABLED', '1') != '1':
        return context | {'status': 'unavailable', 'reason': 'disabled'}

    def frame(job):
        reference, interval = job
        try:
            rows = fetch_candles(reference, interval, limit=REFERENCE_CANDLES)
            return summarize_reference(rows, pair_rows[interval], interval, cutoff)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, ArithmeticError):
            return {'status': 'unavailable', 'timeframe': interval, 'reason': 'source_unavailable_or_invalid'}

    jobs = [(reference, interval) for reference in references if reference != market_id
            for interval in frames]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = dict(zip(jobs, pool.map(frame, jobs), strict=True))
    for reference in references:
        context['references'][reference] = (
            {'status': 'self', 'reason': 'analyzed_market'} if reference == market_id else
            {'status': 'available' if all(results[(reference, i)]['status'] == 'available' for i in frames)
             else 'partial' if any(results[(reference, i)]['status'] == 'available' for i in frames)
             else 'unavailable',
             'timeframes': {interval: results[(reference, interval)] for interval in frames}})
    statuses = [data['status'] for data in context['references'].values() if data['status'] != 'self']
    return context | {'status': 'available' if all(s == 'available' for s in statuses) else
                      'partial' if any(s != 'unavailable' for s in statuses) else 'unavailable'}
