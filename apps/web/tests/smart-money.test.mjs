import assert from 'node:assert/strict';
import test from 'node:test';
import {
  assetForMarket,
  closeDate,
  combine,
  formatAmount,
  formatUsd,
  netScale,
  reserveOrder,
} from '../src/smartMoney.ts';

test('a market names its coin without the quote or the 1000x contract prefix', () => {
  assert.equal(assetForMarket('binance:perp:BTCUSDT'), 'BTC');
  assert.equal(assetForMarket('binance:perp:1000PEPEUSDT'), 'PEPE');
  assert.equal(assetForMarket('binance:perp:1000000MOGUSDT'), 'MOG');
  assert.equal(assetForMarket('binance:perp:ETHUSDC'), 'ETH');
  // A coin whose name starts with digits keeps them.
  assert.equal(assetForMarket('binance:perp:1INCHUSDT'), '1INCH');
});

test('large dollar amounts are compact, small ones exact, and signs only when asked', () => {
  assert.equal(formatUsd(3_250_000, 'en-US'), '$3.3M');
  assert.equal(formatUsd(-14_400_000, 'en-US', true), '-$14.4M');
  assert.equal(formatUsd(14_400_000, 'en-US', true), '+$14.4M');
  assert.equal(formatUsd(0, 'en-US', true), '$0');
  assert.equal(formatUsd(950, 'en-US'), '$950');
  assert.match(formatUsd(3_250_000, 'zh-TW'), /^US\$325萬$/);
});

test('coin amounts keep enough precision for their size', () => {
  assert.equal(formatAmount(732.42, 'en-US'), '732');
  assert.equal(formatAmount(12.345, 'en-US'), '12.35');
  assert.equal(formatAmount(0.78339, 'en-US'), '0.7834');
  assert.equal(formatAmount(25_642_066_780, 'en-US'), '25.6B');
});

test('coin changes can carry their sign', () => {
  assert.equal(formatAmount(-4752.3, 'en-US', true), '-4,752');
  assert.equal(formatAmount(8684, 'en-US', true), '+8,684');
  assert.equal(formatAmount(0, 'en-US', true), '0');
});

test('BTC, ETH and the stablecoins lead the coin list, the rest follow alphabetically', () => {
  assert.deepEqual(reserveOrder(['SOL', 'USDC', 'AAVE', 'ETH', 'USDT', 'BTC']), ['BTC', 'ETH', 'USDT', 'USDC', 'AAVE', 'SOL']);
});

test('USDT and USDC changes add up, a missing coin counts as nothing, and bars share one scale', () => {
  assert.deepEqual(combine([{ net: 5, net_usd: 5 }, undefined, { net: -2, net_usd: -2 }]), { net: 3, net_usd: 3 });
  assert.equal(netScale([{ net: 1, net_usd: -40 }, { net: 1, net_usd: 25 }]), 40);
  assert.equal(netScale([{ net: 0, net_usd: 0 }]), 1);
});

test('a close is shown on its UTC day wherever the viewer is', () => {
  // 00:00 UTC on 26 November is still 25 November in New York.
  const close = Date.UTC(2026, 10, 26);
  assert.equal(closeDate(close, 'en-US'), '11/26');
});
