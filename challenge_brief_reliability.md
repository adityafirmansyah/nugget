CHALLENGE BRIEF — Nugget reliability hardening (round 2)

CONTEXT: You reviewed the Nugget XAUUSD design and raised 3 BLOCKERs + 2 MAJORs.
Adit accepted your corrections and wants a RECOMMENDED, HARDENED design that is
STABLE AND RELIABLE. This round is about operational survival, not modelling.

WHAT IS ALREADY BUILT AND RUNNING (verified, not planned):
  * Profile `nugget` exists, gateway multiplexed under the host gateway,
    Discord bot "Nugget#7454" (client 1554838380205051925) live-connected.
    Currently in ZERO servers (invite pending) -> every send 403s until invited.
  * SOUL.md encodes: no advice, no trade execution, CI-on-every-number,
    append-only ledger, anti-narrative-drift.
  * ledger.py - SQLite (WAL) with DB-level triggers that REJECT UPDATE and
    DELETE on predictions/outcomes. Proven: both refused. Tables:
    predictions, outcomes, fetch_errors.
  * stats.py - bootstrap CI, exact binomial p-value, z vs BASELINE (not 50%),
    best_of_n multiple-comparisons correction, required_n_for_edge.
    Self-tests pass; reproduces your z=0.41/0.44 and best-of-8 p=0.964.
  * predictor.py - 6-parameter scoring (trend/position/momentum/rsi/breadth/
    volume_confirm), abstention when |score|<0.18, noise threshold 0.5xATR14
    stored at forecast time. Runs on live data.
  * Data sources verified reachable from this host (Biznet DNS-blocks
    api.binance.com and Swissquote via RPZ): api.gold-api.com (spot),
    data-api.binance.vision (PAXGUSDT klines), coingecko, nfs.faireconomy.media
    (ForexFactory weekly calendar). All keyless, no SLA.
  * Measured reality: hourly direction is a coin flip (best of 8 = 51.35%,
    z=+0.41; baseline 47.25% always-up; 28.8% of hourly bars are weekend).

THE FIVE CRONS Adit wants:
  1 daily pre-open market prediction
  2 hourly during market open: fetch + predict + PROVE the call before the next
    hourly run starts
  3 market-sentiment research feeding the direction call
  4 algorithm improvement from accumulated prediction history
  5 daily report to a Discord channel on global issues affecting the market

YOUR CORRECTIONS ALREADY ACCEPTED (do not re-litigate):
  - CI/z-test in the grading contract, as the promotion gate
  - sentiment as its own separately-graded column, never fused pre-grading
  - job 4: walk-forward, held-out slice, CI-exclusion promotion, auto-rollback
  - noise threshold in every stored prediction; abstention is a valid outcome
  - append-only ledger, hard-fail on fetch error (never carry forward)

WHAT I NEED NOW — operational reliability of that design. Attack these:

1. MARKET SESSION MECHANICS. Gold (XAUUSD) is not 24/7 like crypto - it runs
   ~Sun 17:00 ET to Fri 17:00 ET with a daily ~1h break (verify this). Job 2 is
   "hourly during market open". What is the concrete, correct open/close and
   break schedule in UTC and WIB, and what breaks if the schedule is naive
   (holidays, DST shifts, the Friday close / Sunday open edges)? Where must the
   ledger record a gap rather than a prediction?

2. CRON EXECUTION REALITY. 5 jobs, some hourly, on a shared multiplexed gateway
   with 17 profiles. Overlap (job 2 takes >1h), missed ticks while the host
   gateway restarts (it restarted 2026-09-30 20:02 and again today), duplicate
   delivery, and the "prove before next hourly run" guarantee that depends on a
   previous run having happened. What is the failure taxonomy and the minimum
   guardrails (idempotency, run-token, catch-up vs skip)?

3. FETCH RESILIENCE. Keyless endpoints with no SLA, hit ~24x/day plus daily/
   sentiment jobs. Rate limits, schema drift, silent partial responses, TLS/DNS
   flap (Biznet RPZ already blocks two hosts). Hard-fail is agreed - but what
   does "hard-fail" concretely require so the ledger stays honest (distinguish
   'source down' from 'market closed' from 'parser broke')?

4. LEDGER INTEGRITY. SQLite WAL, append-only, written by cron processes while
   the gateway and other jobs may also touch it. Concurrent writes, lock
   contention, corruption, no backup. What is the minimum set to make this
   durable and provably un-modified?

5. LLM-BACKED JOBS (1,3,4,5) write prose and make judgements. You already named
   narrative drift as the likely 30-day failure. Now make it OPERATIONAL:
   what structurally prevents job 5 from drifting, job 3 from confabulating,
   and job 4 from overfitting - given nobody is watching hourly?

6. WHAT MOST LIKELY BREAKS THE RELIABILITY LAYER ITSELF within 30 days, even
   if every job runs? Rank the top few, and name the single monitoring/signal
   that would catch each before it silently corrupts the ledger.

Give severity-rated objections (BLOCKER / MAJOR / MINOR), object only with a
real basis, state what would change your mind, and end with the single change
you would make FIRST to make this stable and reliable. Be concrete: file/format/
schedule-level, not principle-level.
