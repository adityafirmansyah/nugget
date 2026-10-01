CHALLENGE BRIEF — "Nugget" XAUUSD trading profile (build plan)

Adit wants a new Hermes profile "Nugget" specialised for XAUUSD trading, with
5 cronjobs:
  1. daily market prediction (pre-open)
  2. hourly market-data fetch during market open; each run predicts bullish/
     bearish AND proves the prediction before the next hourly run starts
  3. market-sentiment research to feed prediction
  4. algo improvement cronjob that learns from prediction history
  5. daily report to a Discord channel on global issues affecting the market
(a separate Discord bot will be created for #5's channel)

WHAT I ALREADY VERIFIED LIVE (not assumptions):
- Reachable, keyless: api.gold-api.com (spot XAU), coingecko (PAXG/XAUT),
  data-api.binance.vision (PAXGUSDT OHLCV+kline history), nfs.faireconomy.media
  (ForexFactory weekly econ calendar). Real snapshot pulled: spot 4189.20,
  PAXG 4193.90, basis +0.11%, RSI14 58.3, ATR14 11.06, spread $0.01.
- BLOCKED at DNS by Adit's ISP (Biznet RPZ): api.binance.com,
  forex-data-feed.swissquote.com. Must use data-api.binance.vision instead.
- I wrote and RAN a walk-forward backtest on 1000 real hourly PAXGUSDT bars
  (41 days) plus 1000 daily bars. Results:
  * always-up baseline: 47.25%
  * EMA12>EMA26: 50.27% | close>SMA20: 48.61% | momentum(3): 46.61%
  * RSI14>50: 48.19% | vol breakout: 45.25% | EMA+RSI confluence: 50.69%
  * best rule of 8 tested: RSI mean-revert, 51.35% on 22.9% coverage
  * past-Nh momentum -> next 24h: 49.3% / 48.1% / 46.8% (n~940)
  * daily close>SMA20 -> next daily bar: 49.12% (n=969)
  * weekend bars: 28.8% of all hourly bars; 7.2% are <10% of avg volume
  Conclusion I drew: hourly direction on this target is a coin flip. An
  hourly "predict-then-prove" loop grading raw next-bar direction would grade
  noise, and cronjob #4 would then optimise randomness.

MY PROPOSED DESIGN RESPONSE TO THAT:
(a) Store EVERY prediction immutably before outcome is known (append-only
    ledger, UTC+WIB, exact expiry bar) so cronjob #4's training data cannot be
    retro-edited.
(b) Grade only threshold-aware: a move smaller than ~0.5 x ATR14 is "noise",
    excluded from the hit-rate; report hit-rate on non-noise bars separately,
    plus an explicit no-call/abstain bucket.
(c) Track hit-rate vs the ALWAYS-UP base rate, not vs 50%, and store that
    base rate per bucket alongside each prediction.
(d) Restrict data collection to real market hours; detect and skip the
    weekend/holiday window instead of trading a thin spread.
(e) Treat cronjob #4's output as a shadow-mode challenger model that must beat
    the incumbent out-of-sample over N bars before it replaces it — no silent
    self-modification of a live predictor.
(f) Frame the whole thing as an instrumented research log, not a profit
    engine: no trade execution, no broker credentials.

WHAT I WANT FROM YOU:
Push back hard on this design. Specifically:
 1. Is (b)+(c) the right grading contract, or does threshold-filtering just
    hide a weak model while inflating hit-rate on a shrinking sample? Is
    bootstrapping/CI needed before calling ANY edge real at n=969?
 2. Is using PAXGUSDT (crypto gold token, 24/7) as a proxy for XAUUSD spot
    structurally flawed for intraday US-session signals? The basis was +0.11%
    and spread $0.01 — does that hold at the hours that matter?
 3. Cronjobs 3 and 5 inject LLM-written sentiment/news into the predictor.
    Is that a contamination/anchoring risk that makes the prediction
    unfalsifiable? Should sentiment be a separately-graded input?
 4. Cronjob #4 self-improvement is where these systems usually go wrong
    (overfitting, look-ahead, survivorship, drift). What is the minimum
    guardrail set you would insist on?
 5. What is the most likely way this whole plan FAILS in 30 days even if every
    job runs perfectly?
 6. Is the 5-job split itself right, or should some be merged/split?

Give me severity-rated objections (BLOCKER / MAJOR / MINOR), object only where
you have a real basis, and state the single change you would make first.
