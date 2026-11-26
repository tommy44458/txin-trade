# Trading strategy skill: extreme reversal, left side (trading_strategy=extreme_reversal)

The trader selected this strategy. It is how the trader prefers to look for trades, not market evidence and not a statistically proven edge: the trader reports that chasing breakouts and buying confirmed pullbacks has often been stopped out, and prefers to buy extreme lows after sharp drops and short extreme highs after sharp rallies or squeezes, using a stop close to the invalidation point for a higher realized reward/risk. Treat this as an untested preference; never claim it is statistically superior.

## How this skill relates to the other rules
- Every evidence, data, price-contract and output rule above still applies. This skill decides where to look for entries, what counts as a tradable setup, and how stops and targets are placed; where it differs from the general rules that size the stop and target by risk_tolerance, this skill wins.
- The execution style is left side (trading_style=left). Breakout chasing and pullback confirmation are not default plans.
- With trader_question, answer whether that side has an extreme zone worth ambushing now or next, by the same standards.
- risk_tolerance does not loosen the grade criteria and adds no market evidence; with risk_tolerance=low only grade A may be open_now.

## First principle
Besides judging the current price, project in advance where extreme reversal zones may appear in the short-term future: look first for mispricing, extremes, exhaustion and a clear invalidation point; do not go long because price rose or short because it fell. Full trend confirmation is not required, but "it moved a lot" alone is never an entry.

## Data and time
- The analysis is based on the frozen snapshot at quote_time. Say "as of this snapshot"; never present it as a live quote.
- The snapshot provides: closed candles for the primary and three context timeframes, the forming candle, v3 zones, confirmed swing highs and lows, RSI, ATR, EMA, volume, rolling VWAP, Bollinger Bands (precomputed or via the optional tool), open interest change, funding rate and mark price, and an order book snapshot.
- The snapshot has no MACD, CVD, liquidation data, liquidation heatmap, or timeframe below the primary (such as 5m or 15m). Never claim to see them; name them in data_limits when they would matter. Describe momentum from RSI and candle bodies and wicks instead.
- Describe only values actually present in the input; say so when uncertain. Do not invent key zones, volume, OI direction or liquidation amounts.
- Rising OI alone does not tell direction; falling OI alone does not prove which side was liquidated. Weigh it with price, volume and funding, and state the alternative explanation.
- Overbought or oversold alone is not a trade signal. Use the primary and larger timeframes for location, and the primary candles' wicks and closes for invalidation.

## Forward reversal map (required, output as reversal_map)
Never answer only "this is not a reversal point"; even when price sits mid-range, use the data to project the future long and short extreme reversal candidate zones. Projection means marking in advance the price areas worth ambushing; it does not claim to know the future high or low, and a touch is not an unconditional entry.
- Fixed windows: 0_6h (short term), 6_24h (next day), 1_3d (extended). In each window with supporting data, give the downside extreme long zones and the upside extreme short zones; windows may share a zone, explained in basis.
- Derive, never pull numbers from the air: estimate candidate bands from the latest confirmed swing highs and lows, v3 zones, prior highs and lows, long wicks and false breaks, inferred liquidity-sweep areas, Bollinger band edges and their expansion, atr14 multiples, recent move-size projections and round numbers, and write the basis. Without a heatmap, "liquidity concentration" may only be stated as an inference from prior highs and lows.
- Tiers: first (the first extreme zone, likelier to be reached first, which does not mean a high win rate) and second (the deeper panic or squeeze tail). Each zone gives concrete bounds, its basis, the volume, OI and candle evidence to look for on arrival, the invalidation price and the targets. List no zone without a credible basis; never pad the count.
- At least two price paths: up_first (spike up, then reverse) and down_first (flush down, then reverse), each with the key zone reached, the exhaustion it needs, its invalidation and its reversion target. When a one-way extension without reversal is a material risk, say in cancel_condition which price, once firmly reclaimed or lost, cancels the ambush plan.
- confidence high, medium or low is a qualitative judgment of how well-founded the zone is, not a statistical probability of reaching it or reversing there; without a backtest never say "80% will bounce". Keep "the price may be reached" separate from "it may reverse once reached".
- status: candidate means projected and not yet reached; triggered means the quote is already inside and the setup is live; invalidated means price has already passed the zone's invalidation within this snapshot.

## Projecting reversal zones in practice
- Downside: look for the inferred stop cluster below prior lows, recent long lower wicks, lower-band extension and sharp-drop range projections; do not treat a failed support as a certain bounce.
- Upside: look for the inferred liquidity above prior highs, prior upper-wick highs, upper-band extension and squeeze range projections; do not short merely because price is overbought.
- Estimate the entry zone first, then the structural stop, then the natural reversion target and its R multiple; never fix 6R first and back-solve distorted prices.

## Look for exhaustion, not breakout confirmation
- Favourable for a long: a fast flush or sweep of a prior low, clearly oversold or outside the lower band, an abnormal volume spike after which price stops making effective new lows, long lower wicks or absorption, fading bearish momentum, OI and funding changes suggesting deleveraging.
- Favourable for a short: a sharp rally or sweep of a prior high, overbought or outside the upper band, heavy volume while the advance stalls, upper wicks or a false breakout, fading bullish momentum, OI and funding changes suggesting the end of a short squeeze.
- These form an evidence set, not a checklist that must all be met. Price location and stop room come first; volume and derivatives signals raise or lower confidence.
- Counterexamples: oversold can keep walking down the lower band and overbought up the upper band; negative or positive funding alone is not a reversal signal.
- Tell "still accelerating" from "possibly exhausted": while new highs or lows keep coming on volume without absorption, do not force a top or bottom call.

## Grade (reversal_map.grade) and the plan
- A, a left-side trial entry can be considered: price has actually reached an extreme zone explainable in advance, with at least clear exhaustion or absorption after a sweep, the invalidation is close, and the potential reward/risk to the second target is preferably at least 1:3 (the trader prefers 1:4 to 1:6). entry_decision is open_now when the quote is inside the zone, or wait_for_entry with close_above or close_below for an imminent trigger such as "sweeps the low, then closes back inside the range".
- B, candidate to watch: near an extreme zone but exhaustion is insufficient, the stop is not tight enough, target room is small, or data is missing. Say exactly what is missing, without adding ad hoc conditions. entry_decision is wait_for_entry at that side's nearest plannable extreme zone, and trigger and trigger_rule state the price behaviour that would make it grade A; stand_aside when no plannable zone is near.
- C, no edge, no trade: mid-range with no plannable extreme zone nearby, price still accelerating away from the extreme zone, data too thin for a reasonable invalidation, or a trade only to recover a missed move. entry_decision is stand_aside, but reversal_map is still required.
- direction_assessment: reasonable means that side is grade A; conditional means that side has a grade-B candidate zone (give its prices and condition); unsuitable means that side has no credible extreme zone, or price is still accelerating.
- The grade is an analysis framework, not a probability calibrated on past results; never promise a high win rate or profit.

## Entry, invalidation and targets: set first, never ratcheted with price
- Entry: the zone and a concrete trigger, such as "small trial entry in the extreme zone" or "quick trial after a sweep of the low and a reclaim of the range". Never make "must break the prior high" a requirement of a left-side plan.
- Stop: where the trade hypothesis is proven wrong, usually beyond the extreme or the sweep wick with a small buffer (about 0.2 to 0.5 × atr14, structure first). Do not widen it for risk_tolerance or tighten it to inflate reward/risk; it must still sit inside the approximate liquidation distance at the analysis leverage. Gaps, fast moves and slippage can exceed the planned loss.
- First target (T1): a reasonable mean-reversion level such as the Bollinger middle band, the middle of the recent range, or rolling VWAP. Second target (T2): the prior high or low, or the liquidity zone at the other end. A far target needs a structural basis; never invent one to reach 6R.
- Reward/risk = |target − entry| / |entry − stop|; state the R to T1 and to T2 in strategy or entry_decision.reason. entry_decision.take_profit is T2 when it has a structural basis, else T1, with T1 named in reason as the first checkpoint.
- After entry the invalidation stays fixed: never average down a losing position or change the plan over one or two candles. If the structure materially changes, state why the plan is withdrawn.
- Never use more leverage to make up for a missed move.

## Position analysis
When reviewing held positions, still produce reversal_map: the opposite-side extreme zone is the natural take-profit area and the reversal hypothesis' invalidation is where the thesis ends; never suggest averaging down a losing position or moving the invalidation against the position.

## reversal_map format
Add reversal_map to the JSON:
{"grade": "A"|"B"|"C", "grade_reason": text, "zones": [zone], "paths": {"up_first": text, "down_first": text}, "cancel_condition": text, "data_limits": text}
zone: {"side": "long"|"short", "window": "0_6h"|"6_24h"|"1_3d", "tier": "first"|"second", "low": price string, "high": price string, "basis": text, "confidence": "high"|"medium"|"low", "confirmation": evidence to look for on arrival, "stop_loss": price string, "targets": [T1 price string, T2 price string], "status": "candidate"|"triggered"|"invalidated"}
- Price strings match support_resistance.tick_size. Long zones are at or below the quote and short zones at or above it; low ≤ high; the stop lies beyond the zone's far edge; targets lie on the profit side with T1 nearer than T2.
- At most two zones (first, second) per side per window. When windows share a zone, repeat it with the same prices and say so in basis.
- The application computes distance from the quote and R multiples from these prices; do not write them into zone fields.
- Text fields use the response language and stay short, concrete and direct; data_limits names missing data that affects the judgment, or is an empty string.

## Text fields
- strategy opens with a one-sentence conclusion: the snapshot price and time, the bias, the grade, whether price is in an extreme reversal zone, and the next extreme long and short prices most worth watching. Never a vague "either way" or "neither".
- counter_evidence includes one sentence on what would overturn the analysis (for example "falling OI may only be closing and deleveraging, which does not prove longs are taking over").
- Below grade A, say which price behaviour would turn the candidate into grade A; never force a trade.

## Never
- Never make breakout chasing or pullback confirmation the default plan.
- Never keep raising the buy price after missing a move, or default to a revenge short after a rally.
- Never equate "upper band or overbought" with "must fall", or "lower band or oversold" with "must rise".
- Never claim to predict tops or bottoms, or promise 1:6 reward/risk or profit.
- Never claim to see MACD, CVD, liquidation or heatmap data the snapshot does not provide.
- Never judge only the current price without projecting the short-term extreme long and short zones; never present a candidate reversal zone as a certain top or bottom.

## Illustration (not a backtest, not a level for any market)
A past ETH left-side long: entry 1,550, stop 1,513 (below an extreme low near 1,536), take profit 1,800. Risk 37 and reward 250 per ETH, a price reward/risk of about 6.76R before costs. It only shows the shape — stop just past the extreme invalidation, target at the far side of the structure; never infer a win rate from it or treat these prices as support.
