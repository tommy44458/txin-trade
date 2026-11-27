import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { setUiLocale } from '../src/i18n/index.ts';
import { usEquitySessionText } from '../src/usEquitySession.ts';

afterEach(async () => { await setUiLocale('zh-TW'); });

test('the session line names the state, an early close, the next open and regular-hours share', async () => {
  assert.equal(usEquitySessionText({ status: 'calendar_unavailable' }), '美股交易時段未知');
  assert.equal(usEquitySessionText({ status: 'available', state: 'regular', next_regular_open: '2026-11-30T14:30:00Z' }), '美股盤中');
  const holiday = usEquitySessionText({
    status: 'available', state: 'holiday', next_regular_open: '2026-11-27T14:30:00Z',
    recent_candles: { timeframe: '1h', count: 24, regular_session_share_pct: 0 },
  });
  assert.match(holiday, /^美股假日休市 · 下次開盤 .+ · 最近 24 根 K 線有 0% 落在正規交易時段$/);
  assert.match(usEquitySessionText({ status: 'available', state: 'regular', early_close_today: true }), /今天提早在美東 13:00 收盤/);
  await setUiLocale('en-US');
  assert.match(usEquitySessionText({ status: 'available', state: 'weekend', next_regular_open: '2026-11-30T14:30:00Z' }),
    /^US market closed for the weekend · next open /);
});
