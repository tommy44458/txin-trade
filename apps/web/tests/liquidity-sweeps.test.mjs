import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { setUiLocale } from '../src/i18n/index.ts';
import { liquidationRows } from '../src/liquidationBands.ts';
import { levelSweepNotes } from '../src/levelSweeps.ts';
import { discussionLiveMarketView } from '../src/discussionLiveMarket.ts';

afterEach(async () => { await setUiLocale('zh-TW'); });

test('liquidation rows put shorts above the price and longs below in half-percent steps', () => {
  const rows = liquidationRows({
    status: 'available', reference_price: '100',
    histogram: [['100.5', '100.75', '0', '300'], ['100.75', '101', '0', '200'], ['98.5', '98.75', '400', '0'],
      ['80', '80.25', '999', '0'], ['99.75', '100', '50', '0']],
  });
  assert.equal(rows.length, 20);
  assert.deepEqual(rows.map(row => row.index).slice(0, 3), [9, 8, 7]);
  assert.equal(rows.find(row => row.index === 1).value, 500);
  assert.equal(rows.find(row => row.index === -3).value, 400);
  assert.equal(rows.find(row => row.index === -1).value, 50);
  // A band beyond five percent is left out of the chart.
  assert.equal(rows.reduce((sum, row) => sum + row.value, 0), 950);
  assert.ok(rows.every(row => row.to > row.from));
});

test('sweep notes skip older zones and name untaken equal pivots, sweeps and the forming candle', async () => {
  const when = value => `at ${value}`;
  assert.deepEqual(levelSweepNotes({}, when), []);
  assert.deepEqual(levelSweepNotes({ liquidity_beyond: { side: 'below', basis: 'single_pivot', status: 'untaken', taken_at: null } }, when), []);
  assert.deepEqual(levelSweepNotes({
    sweep_count: 2, forming_candle_swept: true,
    liquidity_beyond: { side: 'above', basis: 'equal_pivots', status: 'untaken', taken_at: null },
  }, when), ['這根未收盤 K 線掃過外緣後已收回', '上方等高的停損尚未被掃', '累計被掃 2 次']);
  await setUiLocale('en-US');
  assert.deepEqual(levelSweepNotes({
    sweep_count: 1, liquidity_beyond: { side: 'below', basis: 'single_pivot', status: 'swept', taken_at: 'T' },
  }, when), ['Swept past the edge and reclaimed at T', 'Sweeps so far: 1']);
});

test('a v2 follow-up shows whether the market was recalculated; v1 replies show nothing new', () => {
  const base = {
    version: 'discussion_live_market_v1', status: 'available', market_id: 'binance:perp:BTCUSDT', timeframe: '1h',
    source: 'binance_usdt_perpetual', requested_at: '2026-11-27T00:00:00Z', observed_at: '2026-11-27T00:00:00Z',
    quote: { price: '100', observed_at: '2026-11-27T00:00:00Z' }, forming_candle: null, recent_closed_candles: [],
    comparison: null, errors: {}, not_refreshed: [],
  };
  assert.equal(discussionLiveMarketView(base).snapshot, null);
  const v2 = { ...base, version: 'discussion_live_market_v2' };
  assert.equal(discussionLiveMarketView({ ...v2, snapshot: { status: 'available' } }).snapshot, 'available');
  assert.equal(discussionLiveMarketView({ ...v2, snapshot: { status: 'partial' } }).snapshot, 'partial');
  assert.equal(discussionLiveMarketView({ ...v2, snapshot: { status: 'unavailable', reason: 'TIMEOUT' } }).snapshot, 'unavailable');
  assert.equal(discussionLiveMarketView(v2).snapshot, 'unavailable');
  assert.equal(discussionLiveMarketView({ ...v2, snapshot: { status: 'available' } }, 'binance:perp:ETHUSDT').snapshot, null);
});
