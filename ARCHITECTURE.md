# Architecture

## Tech Stack
| Layer | Tech | Notes |
|-------|------|-------|
| Language | Python 3.13 | Windows-only (MT5 constraint) |
| MT5 | MetaTrader5 package | Synchronous, binds to calling thread |
| Remote DB | asyncpg | Direct PostgreSQL to Supabase |
| Local DB | aiosqlite | `orders.db` for all mutable state |
| Backend | FastAPI + Uvicorn | Async, serves API + static files |
| Frontend | React + TypeScript + Vite | SPA bundled into `frontend/dist/` |
| Real-time | Server-Sent Events (SSE) | Logs + status streamed to browser |
| Config | Pydantic v2 | Typed config.json validation |
| Tray | pystray + Pillow | System tray icon with menu |
| Build | PyInstaller | Single `.exe` with bundled dist/ |
| Env | python-dotenv | `.env` for contributor DSN |

## Concurrency Model
```
Main Thread (pystray)
  |  System tray icon event loop (Windows message pump)
  |  Spawns engine thread on startup
  |
  +-- Engine Thread (threading.Thread)
        |  mt5.initialize() HERE (binds MT5 to this thread)
        |  asyncio.run(engine.run_forever())
        |
        +-- Task: uvicorn.Server.serve() — FastAPI at :8501
        +-- Task: supabase_sync_loop() — polls Supabase every 30s
        +-- Task: tp_monitor_loop() — 1s when positions/orders exist
        +-- Task: license_heartbeat() — HTTP POST every 15 min
```

MT5 calls are sync but <50ms. Called directly in the async loop (no executor). Safe because asyncio is single-threaded — tasks yield between MT5 calls.

## Core Loop (sync_cycle.py)
```
1. Query Supabase: active/hit signals + pending limits (1 query)
2. Query Supabase: live_prices for offset instruments (1 query, only if needed)
3. Diff against SQLite order_mappings locally
4. For new limits: calculate lot, compute offset+spread, place MT5 order, write SQLite
5. For stale limits: cancel MT5 order, update SQLite
6. For price drift on offset instruments: modify MT5 order price
```

**Re-placement guard (no duplicate entries).** A "new" limit is `supabase_limit_ids -
sqlite_active_limit_ids` (active = pending/filled), but a limit that has *ever filled*
on our broker (`order_mappings.status IN ('filled','closed')`) is also subtracted, so it
is never placed again — even after its position TPs/closes and the SQLite row goes to
`closed`. This stops the dangerous loop where a limit fills on our end while the TM/DB
still shows it `pending` (sub-pip mismatch, or the TM bot was down / signal went stale):
the position TPs, the `closed` row drops out of `get_all_active()`, and the still-pending
DB limit would otherwise reappear as "new" and re-enter the same level forever. The
`closed` row is the durable, restart-safe marker. Never-filled `cancelled` /
`spread_cancelled` rows are deliberately *excluded* from the guard so legitimate
re-placement (spread hour, news, offset drift, SL change) still works.

## TP Engine (tp/)
```
TPStrategy (Protocol)
  +-- DefaultTPStrategy
        should_trigger() — newest pos >= threshold AND others_pnl >= 0
        execute()        — close earlier positions, partial-close newest, trail remainder
        update_trailing() — ratchet SL (up for longs, down for shorts)

TrailingStopManager
  Called every 1s for is_trailing=1 positions
  SL = bid - trail_distance (longs) or ask + trail_distance (shorts)
  SL only moves in favorable direction, never retreats

TPEngine (engine.py)
  Groups positions by signal_id (via SQLite)
  Delegates to strategy per signal group
```

### TP Trigger Conditions (both must be true simultaneously)
1. Most-recently-filled position (highest ticket#) moved >= `profit_threshold`
2. Combined P&L of ALL OTHER positions for same signal >= 0

### Follow Server TP (`tp_config.follow_server_tp`, off by default)
Replaces both conditions above with the signal service's own call: the group takes profit
as soon as its signal is `status='profit'` + `closed_reason='automatic'` in Supabase. Users
whose fills or broker prices differ from the TM feed then exit *with* the alert instead of
waiting on a threshold their broker may never print. Nothing after the trigger changes —
partial close, trailing, pending-limit teardown, `signal_tp_fired`, and the outcome rows
(tagged `tp_strategy='follow_server'`) behave identically.

The signal set costs no extra egress: `SyncCycle._check_forced_exits` already fetches
statuses for every locally-filled signal behind the rev-gated cache, so it publishes the
auto-TP'd ids on `SyncCycle.server_tp_signals` and `_tp_loop` hands that set to
`TPEngine.run_cycle`. Manual `profit` is excluded — that path force-closes the whole
position (see below) rather than running a TP.

### On Trigger
- Earlier positions: close 100% at market
- Newest position: close `partial_close_percent`% at market
- Remainder: enter trailing phase (`is_trailing=1` in SQLite)
- If partial_close_percent=100: close everything, no trail
- If partial_close_percent=0: trail full position, close nothing

### Partial Close Ticket Handling
ICMarkets hedging mode: partial close creates a **new ticket** for the remainder. Original ticket is closed. Fill detector must:
1. Detect new ticket via MT5 deal history
2. Create new SQLite row inheriting signal_id, signal_type, is_trailing from parent

### Asset Class Detection Order (symbol_mapper.py)
1. Metals: `XAUUSD`, `XAGUSD`, `GOLD`, `SILVER`
2. Oil: contains `OIL`, `WTI`, `BRENT`
3. **Stocks**: ends in `.NAS` or `.NYSE` (MUST check before indices)
4. Indices: contains `SPX`, `NAS`, `DAX`, `JP225`, `UK100`, `US500`, `USTEC`, etc.
5. Crypto: ends in `USD`/`USDT` and `len > 6`
6. Forex (default); `JPY` in name -> `forex_jpy`

### TP Defaults
```
Standard:                              Scalp (roughly halved):
  forex:     7 pip / 3 pip trail         forex:     5 pip / 2 pip trail
  forex_jpy: 7 pip / 3 pip trail         forex_jpy: 5 pip / 3 pip trail
  metals:    $4 / $2 trail               metals:    $2 / $1 trail
  indices:   $20 / $5 trail              indices:   $10 / $3 trail
  stocks:    $1 / $0.50 trail            stocks:    $0.50 / $0.25 trail
  crypto:    $300 / $50 trail            crypto:    $150 / $25 trail
  oil:       $0.50 / $0.20 trail         oil:       $0.25 / $0.10 trail
```

### TP Outcomes (Supabase `tp_outcomes` — write-only analytics)
Append-only log for forward-test / backtest analysis. The unit of analysis is the **signal**
(a set of limits sharing one stop loss). Each trade produces **two rows**, distinguished by `stage`:
- `stage='trigger'` — written by `TPEngine` the moment TP fires. `realized_pnl`/`r_multiple` cover
  only the closed portion at that instant (a trailing remainder is still open).
- `stage='final'` — written by `TPFinalizer.sweep` once the signal's last position goes flat.
  Carries the **whole-trade** result. Idempotent via the local SQLite `signal_finalized` guard
  (exactly one final row per signal). For analysis, the `final` row is the source of truth for
  outcome; the `trigger` row shows what was captured at the trigger instant.

`r_multiple` is money-based: `realized_pnl / risk_money`, where
`risk_money = |avg_entry − stop_loss| × (tick_value / tick_size) × total_volume`
(`price_distance_to_money`, `bot/trading/lot_calculator.py`). Shared SL makes this exact for
multi-limit signals. MFE/MAE are price-distance excursions from entry (always ≥ 0), sampled each
TP cycle and aggregated as `MAX` across the signal's tickets.

Column reference (key columns; remainder are self-explanatory):

| Column | Meaning |
| --- | --- |
| `stage` | `'trigger'` or `'final'` — see above |
| `signal_type`, `asset_class`, `symbol`, `direction` | trade classification (`direction`: long/short) |
| `total_limits` / `limits_filled` / `limits_pending` / `limits_cancelled` | level counts; on the trigger row, `limits_filled` ≈ how many levels filled before the bounce |
| `level_sequence` | `sequence_number` of the deepest filled limit — how deep price went before reacting |
| `total_levels` | total limits on the signal |
| `avg_entry_price`, `stop_loss` | volume-weighted entry; shared signal SL |
| `risk_per_limit` | price distance `|avg_entry − stop_loss|` (risk in price units) |
| `risk_percent_cfg` | configured risk % at the time (context for sizing) |
| `r_multiple` | realized reward-to-risk in money terms (the headline metric on the `final` row) |
| `realized_pnl` | account-currency P&L (closed portion on `trigger`; full trade on `final`) |
| `others_pnl` | P&L of non-newest positions at trigger (`trigger` only) |
| `mfe_price` / `mae_price` | max favorable / adverse excursion from entry, price units |
| `mfe_r` / `mae_r` | the same expressed in R (`/ risk_per_limit`). `mfe_r` ≈ ceiling of capturable R; capture-efficiency ≈ realized R ÷ `mfe_r` |
| `tp_trigger_price`, `move_at_trigger` | price and favorable move when TP fired (`trigger` only) |
| `threshold_value`, `threshold_unit` | the TP threshold that fired (`pips`/`dollars`) |
| `partial_close_pct`, `trailing_started` | close/trail behavior at trigger |
| `seconds_to_trigger` | fill → trigger latency (`trigger` only) — impulse speed |
| `hold_seconds` | first fill → final close duration (`final` only) |
| `exit_reason` | `final` only: `trailing_stop` / `tp_full` / `stop_loss` (heuristic from row state — not a precise close-cause audit) |
| `total_volume`, `partial_close_pct`, `bot_version`, `tp_strategy`, `notes` | execution context |
| `mt5_account`, `channel_id`, `written_at` | account, source channel, insert time |
| `symbol_normalized` | DB symbol with broker suffixes stripped (`XAUUSDm`/`USDCAD.pro` → `XAUUSD`/`USDCAD`) — group analysis on this, not `symbol` |
| `account_equity` / `account_balance` | MT5 account state at write time (weight users, spot glitched accounts) |
| `entry_slippage_points` | `final` only: avg (actual fill − intended limit price) in broker points, adverse-positive by direction; NULL when fill prices weren't captured |
| `exit_slippage_points` | avg close slippage in broker points, adverse-positive, bot-initiated closes only. **NULL means broker-side SL/trailing exit (unmeasurable), not zero** |

`notes` on **final** rows is a config snapshot: `{profit_threshold, threshold_unit,
trailing_distance, partial_close_percent, lot: {mode, value, source}, disable_auto_tp,
skip_limits_at}` — so trailing-vs-fixed comparisons aren't confounded by per-user settings.
Trigger rows keep `notes = {"non_trailing_count": N}`.

**Trigger-row dedupe**: the SQLite guard table `trigger_recorded` (PK `(signal_id,
mt5_account, level_sequence)`) is claimed before every trigger-row write. A failed close
re-qualifies the TP every ~1s cycle and used to spam identical trigger rows; now each fill
depth writes exactly once. Cleared by `clear_history` alongside the other guards. Close
retry behaviour is unchanged — only the duplicate INSERT is suppressed.

Adding columns requires an owner-run `ALTER TABLE tp_outcomes ADD COLUMN IF NOT EXISTS ...` in the
Supabase SQL editor — the bot only INSERTs. The local SQLite side (`order_mappings.sequence_number`
/ `mfe_price` / `mae_price` / `fill_price` / `exit_slippage_points`, `signal_finalized` and
`trigger_recorded` tables) auto-migrates on startup.

## Spread Adjustment (order_placer.py)
On every pending order placement, adjust price and SL for current spread:
```
spread = ask - bid  (from MT5 live tick)

Long (buy) limits:   adjusted_price = limit_price + spread
                     adjusted_sl    = stop_loss - spread

Short (sell) limits: adjusted_price = limit_price - spread
                     adjusted_sl    = stop_loss + spread
```
Raw DB prices stored in SQLite for reference. Adjusted prices sent to MT5.

## Feed Offset (offset_calculator.py)
For indices/crypto where DB prices come from OANDA/Binance feeds:
```
offset = mt5_mid - feed_mid
mt5_order_price = db_limit_price + offset  (then apply spread adjustment)
```
Offset instruments: SPX500USD->US500, NAS100USD->USTEC, BTCUSDT->BTCUSD, ETHUSDT->ETHUSD.
`mt5_mid` and `feed_mid` are paired AT THE FEED'S EXACT `updated_at`: the broker mid is read
from MT5 tick history (`copy_ticks_range`) matched to `updated_at` to the millisecond
(`time_msc`), so the gap between the feed timestamp and "now" never leaks into the offset
(M1-bar mid is the fallback when tick history is empty). Recompute is throttled per symbol
(`offset_recompute_interval_seconds`, default 300s) and the cached offset served between — one
tick + one history call per symbol per interval; a transient history gap reuses the last good
offset. Dead-feed bound (`feed_max_staleness_seconds`, default 120s): while a signal is active
a stale `updated_at` means the feed updater stalled → skip placement. Readjust existing orders
if offset drift > threshold (default 5 pips).

## Symbol Mapping
| DB Instrument | MT5 Symbol | Offset? |
|---------------|-----------|---------|
| XAUUSD / GOLD | XAUUSD | No |
| EURUSD, etc. | same | No |
| SPX500USD | US500 | Yes (OANDA) |
| NAS100USD | USTEC | Yes (OANDA) |
| BTCUSDT | BTCUSD | Yes (Binance) |
| ETHUSDT | ETHUSD | Yes (Binance) |
| AMD.NAS | AMD.NAS-24 | No |
| MSFT.NYSE | MSFT.NYSE-24 | No |

Stock suffix `-24` means 24-hour trading (permanent, not yearly). Configurable in config.json.

## Lot Sizing — Two Modes
**Risk % (default):** `lot = (balance * risk%) / (num_limits * avg_sl_distance_pips * pip_value_per_lot)`
**Fixed lot:** flat lot per limit, ignoring balance/SL/signal size.
Clamped to MT5 symbol volume_min/volume_max, rounded to volume_step.

## Market Hours / Spread Hour (scheduler.py)
Pause placement + cancel pending during:
- Mon-Thu: 3:55 PM - 6:00 PM EST
- Weekend: Fri 3:55 PM - Sun 6:00 PM EST

The window opens at 3:55 PM (an hour before the 5:00 PM spread spike) so late-market
signals stop activating; the earlier 3:55–4:55 slice is a "late-market" phase (pending
cancelled, SLs untouched). Filled-position SL stripping stays pinned to spread hour
proper (4:55–6:00 PM, `sl_strip_start`). Cancelled orders marked `spread_cancelled` in SQLite. Re-placed automatically when markets reopen (sync cycle sees DB limit still pending + no active SQLite mapping).

## News Mode (per-symbol)
`bot_mode_status.news_mode` is a comma-separated list of news tokens (NULL = no news). Tokens are currency codes (`USD`, `EUR`, `JPY`, …) plus named assets (`GOLD`), or the single token `ALL`. A token applies to an instrument when, aliased, it is a substring of the DB symbol — so `USD` gates EURUSD, USDJPY, XAUUSD, SPX500USD; `GOLD` aliases to `XAU` and gates XAUUSD only. Oil is USD-denominated but its symbol (e.g. USOILSPOT) has no `USD` substring, so `USD` news gates it via asset class. `ALL` gates everything. Crypto and 24h stocks are exempt (same as the spread gate). Parsing/matching live in `symbol_mapper.py` (`parse_news_symbols`, `instrument_under_news`).

While a symbol is under news the bot (a) cancels its pending orders (same path as the spread gate) and (b) force-closes any filled positions on that symbol (`SyncCycle._check_news_exits`), mirroring the manual-cancel / breakeven force-exit.

## Volatility Guard (opt-in)
`bot_mode_status.vol_guard` is a second token column written by the signal service's volatility monitor — identical format to `news_mode` (comma-separated tokens, or `ALL`, NULL when calm), but per-instrument: it writes the whole DB symbol (`EURUSD`, `NAS100USD`, `BTCUSDT`, `AMD.NAS` — matching only that instrument) and `ALL` for gold, which flags the market as a whole. TM covers forex, gold, indices, crypto, oil and stocks; non-forex thresholds are ~2x the execution bot's proximity distance over a 3-minute window, so an instrument only flags on a move twice the band its limits sit in.

It is **off by default and only consumed when the user enables `volatility_guard` in Misc settings**. When on, the sync cycle reads `vol_guard` alongside `news_mode` in the per-cycle sync-state poll (`SupabaseDB.fetch_sync_state`); volatility tokens cancel pending orders and force-close filled positions through the same path and parsing as news, tagged `force_vol` instead of `force_news`. When off, the column is read but its tokens are dropped, so the guard has no effect. Reaction time tracks the existing sync cadence (1s while any order/position is live).

The two token sets stay **separate** (`SyncCycle._gate_symbols` → `_gated_by_news_or_vol`). The crypto / 24h-stock `_gate_exempt` carve-out exists because those markets don't share news' liquidity events — that reasoning doesn't transfer to volatility, which is measured straight off price. So vol tokens are matched *before* the exemption and news tokens *after*: crypto stays live through news but is gated by a real move. Unioning the sets (the original design) silently dropped every crypto and 24h-stock volatility token.

Matching is always on **DB symbols**, never broker symbols: `is_blocked` resolves via `_CycleContext.instr_of` and `_check_news_exits` reverse-maps through `db_symbol_from_mt5`, so a `EURUSD` token gates a suffixed `EURUSDm` position on an Exness-style broker.

## Polling Strategy
Scalp strategy — every second matters. The sync loop runs at 1s while any pending order or
position exists (30s idle), but almost all of its Supabase traffic is a single tiny
`bot_mode_status` row per cycle (`SupabaseDB.fetch_sync_state`): the news/vol gate tokens plus
`signals_rev`, a watermark that TM-side statement triggers bump on every `signals`/`limits`
write. The heavy queries — the active signal set (`FETCH_SIGNAL_SETS`) and force-exit statuses
(`FETCH_SIGNAL_STATUSES`) — are cached and refetched only when the watermark moves (or a
locally-filled signal set changes), with 60s/30s max-ages as a safety net. Against a DB
without the column the bot falls back to the old interval polling (5s sets / 2s statuses).
Live feed prices for offset symbols stay on a 5s pull (values churn, so a watermark can't
help), and feed health on 60s. This keeps shared-pooler egress flat while reaction time to a
new signal or status flip is one sync cycle (~1s) after the TM commits it.

## License System
- Supabase Edge Function (Deno/TypeScript) at `supabase/functions/validate-license/`
- Bot POSTs `{license_key, mt5_account}` to Edge Function URL
- Validates key exists, active, not expired, MT5 account matches
- Startup: validate or refuse to start
- Heartbeat: re-validate every 15 min
- On failure: block new placements, keep managing open positions
- URL: production hardcoded in constants.py, contributors override via `.env`

## Startup Reconciliation (reconciler.py)
1. Get MT5 orders + positions filtered by magic number
2. Compare against SQLite order_mappings
3. Pending in SQLite + in MT5 positions -> mark filled
4. Pending in SQLite + gone from MT5 -> mark cancelled
5. Filled in SQLite + gone from MT5 -> mark closed
6. Filled + is_trailing=1 + still in MT5 -> resume trailing
7. MT5 orders with our magic but not in SQLite -> log warning (orphans), do NOT auto-cancel

## Signal Tracking
Multiple signals can exist for same instrument. signal_id encoded in MT5 order comment: `"s{signal_id}"` (e.g. `"s12345"`). Primary mapping is SQLite `order_mappings.signal_id`. Comment is reconciliation fallback.

## Supabase Contract (TM bot ↔ this bot)

The TM (signal) bot owns and writes the shared database; this bot is a read-mostly
consumer. The exe embeds the `execution_bot_ro` role, whose grants are deliberately
least-privilege:

- **SELECT** on `signals`, `limits`, `live_prices`, `feed_health`, `bot_mode_status`
- **INSERT** on `tp_outcomes` (append-only analytics)
- **EXECUTE** on `public.upsert_user_snapshot(...)` — a `SECURITY DEFINER` function
  that resolves `license_id` from `licenses` and upserts the per-account `users`
  snapshot server-side. The role has **no direct access** to `licenses` or `users`,
  so an exe holder can never read other users' license keys or account snapshots.

### Tables read
**signals**: id, instrument, direction(long/short), stop_loss,
status(active/hit/profit/breakeven/stop_loss/cancelled),
type(standard/scalp/swing/toll/pa/1-1/risky), closed_reason, total_limits, channel_id
**limits**: id, signal_id(FK), price_level, sequence_number, status(pending/hit/cancelled)
**live_prices**: symbol(PK), bid, ask, feed(icmarkets/oanda/binance/exness), updated_at
**feed_health**: feed(PK), status(idle/healthy/down) — there is no `degraded` status
**bot_mode_status**: singleton row — comma-separated tokens or `ALL`, NULL when
inactive. news_mode uses currency/asset tokens (e.g. `EUR, GOLD`); vol_guard uses
whole DB instruments (e.g. `EURUSD, NAS100USD, BTCUSDT`) plus `ALL` for gold. signals_rev is the write watermark
(TM-side statement triggers bump it on every signals/limits write) that drives the
rev-gated caches above; when the column is absent the bot logs once and falls back
to legacy interval polling

### Vocabularies this bot depends on
- `signals.closed_reason`: `automatic`, `manual`, `expiry`, `near_miss`, `news:<CAT>`,
  `spread_hour`, `late_market`, `risky_window`, `real_sl`. Only `expiry` marks a
  rolled-over hit signal (position stays open on weekdays); every other cancel reason
  force-closes.

### Invariants guaranteed by the TM bot
- `limits.id` is stable across signal edits — an unchanged price level keeps its row
  (an edited level gets a fresh IDENTITY id, which the (signal_id, price) re-placement
  guard covers).
- `live_prices.updated_at` advances at least every **30 s** while the feed is ticking
  (heartbeat), and ages honestly when a feed dies — this bot's
  `feed_max_staleness_seconds` (120 s) dark-feed gate and offset anchoring rely on it.
- Cancel paths update `limits` before the signal row transitions, so a pending-limit
  query never sees a half-cancelled signal.
- A TM-marked `hit` limit stays in the active fetch while its signal is live, so the
  local pending order is held for sub-pip fills instead of being stale-cancelled.
- The `profit` branch of `FETCH_SIGNAL_SETS` is scoped to the signal ids this bot
  still holds fills for, keeping the query bounded.

## SQLite Schema (orders.db)
```sql
CREATE TABLE IF NOT EXISTS order_mappings (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    limit_id                BIGINT NOT NULL UNIQUE,
    signal_id               BIGINT NOT NULL,
    mt5_ticket              BIGINT NOT NULL UNIQUE,
    order_type              TEXT NOT NULL,        -- buy_limit/sell_limit/buy_stop/sell_stop/remainder
    lot_size                REAL,
    placed_at               TEXT NOT NULL,        -- ISO timestamp
    filled_at               TEXT,
    cancelled_at            TEXT,
    status                  TEXT NOT NULL DEFAULT 'pending',  -- pending/claimed/filled/closed/cancelled/spread_cancelled/error
    feed_price_at_placement REAL,
    mt5_price_at_placement  REAL,
    offset_at_placement     REAL,
    last_offset_check       TEXT,
    db_stop_loss            REAL,
    last_known_mt5_sl       REAL,
    signal_type             TEXT NOT NULL DEFAULT 'standard',  -- standard|scalp|swing|toll|pa|1-1|risky
    is_trailing             INTEGER NOT NULL DEFAULT 0,
    sl_stripped             INTEGER NOT NULL DEFAULT 0,  -- spread-hour SL strip flag
    symbol                  TEXT,
    realized_pnl            REAL,
    channel_id              INTEGER,
    sequence_number         INTEGER,
    mfe_price               REAL NOT NULL DEFAULT 0,     -- excursion analytics
    mae_price               REAL NOT NULL DEFAULT 0,
    fill_price              REAL,
    exit_slippage_points    REAL
);
```

## config.json Full Structure
```json
{
  "license_key": "",

  "lot_sizing": {
    "mode": "risk_percent",
    "risk_percent": 1.0,
    "fixed_lot": 0.01,
    "max_lot_per_order": 5.0
  },

  "polling": {
    "supabase_interval_seconds": 30,
    "tp_active_interval_seconds": 1,
    "license_heartbeat_seconds": 900
  },

  "symbol_map": {
    "SPX500USD": "US500",
    "NAS100USD": "USTEC",
    "BTCUSDT": "BTCUSD",
    "ETHUSDT": "ETHUSD"
  },
  "stock_suffix": "-24",
  "symbol_suffixes": [{ "suffix": "m", "asset_classes": ["forex", "forex_jpy", "metals", "crypto"] }],

  "offset_instruments": ["SPX500USD", "NAS100USD", "BTCUSDT", "ETHUSDT"],
  "offset_drift": { "indices": { "NAS": 8.0 }, "crypto": 25.0, "oil": 0.15, "default": 5.0 },
  "feed_max_staleness_seconds": 120,

  "spread_hour": {
    "daily_start": "15:55",
    "daily_end": "18:00",
    "timezone": "US/Eastern",
    "weekend_start_day": "Friday",
    "weekend_end_day": "Sunday"
  },

  "tp_config": {
    "partial_close_percent": 50,
    "forex":     { "profit_threshold": 7,     "threshold_unit": "pips",    "trailing_distance": 3 },
    "forex_jpy": { "profit_threshold": 7,     "threshold_unit": "pips",    "trailing_distance": 3 },
    "metals":    { "profit_threshold": 4.0,   "threshold_unit": "dollars", "trailing_distance": 2.0 },
    "indices":   { "profit_threshold": 20.0,  "threshold_unit": "dollars", "trailing_distance": 5.0 },
    "stocks":    { "profit_threshold": 1.0,   "threshold_unit": "dollars", "trailing_distance": 0.5 },
    "crypto":    { "profit_threshold": 300.0, "threshold_unit": "dollars", "trailing_distance": 50.0 },
    "oil":       { "profit_threshold": 0.5,   "threshold_unit": "dollars", "trailing_distance": 0.2 },
    "scalp_overrides": {
      "forex":     { "profit_threshold": 5,   "trailing_distance": 2 },
      "forex_jpy": { "profit_threshold": 5,   "trailing_distance": 3 },
      "metals":    { "profit_threshold": 2.0,  "trailing_distance": 1.0 },
      "indices":   { "profit_threshold": 10.0, "trailing_distance": 3.0 },
      "stocks":    { "profit_threshold": 0.5,  "trailing_distance": 0.25 },
      "crypto":    { "profit_threshold": 150.0,"trailing_distance": 25.0 },
      "oil":       { "profit_threshold": 0.25, "trailing_distance": 0.1 }
    },
    "toll_overrides":  {},
    "swing_overrides": {},
    "pa_overrides":    {},
    "one_to_one": {
      "profit_threshold": 10.0,
      "overrides": {}
    },
    "instrument_overrides": {}
  }
}
```

Per-type override fallback rules (see `bot/tp/asset_config.py`):

- `standard` — uses the base asset-class config.
- `scalp`, `toll`, `pa` — use the per-asset override if present; otherwise fall back to the base asset-class config.
- `swing` — uses the per-asset override if present; otherwise falls back to `3 × base.profit_threshold`.
- `1-1` — engine forces `threshold_unit='dollars'`, `partial_close_percent=100`, and `trailing_distance=0`. Only `one_to_one.profit_threshold` (and per-asset `overrides`) is user-tunable. Trailing is also gated off explicitly in `TPEngine._process_group`.

## Order Type Selection Logic
The order type depends on signal direction and limit price relative to current market:
```
direction=long:
  limit_price < current_ask  -> BUY_LIMIT  (price below market, expect bounce up)
  limit_price > current_ask  -> BUY_STOP   (price above market, expect breakout up)

direction=short:
  limit_price > current_bid  -> SELL_LIMIT (price above market, expect reversal down)
  limit_price < current_bid  -> SELL_STOP  (price below market, expect breakdown)
```
After spread adjustment is applied to the price, the comparison uses the adjusted price.

## FastAPI Endpoints (bot/api/routes.py)
```
GET  /api/status         — { engine_running, license_valid, mt5_connected, supabase_connected, pending_count, open_count, trailing_count }
GET  /api/config         — current config.json as JSON
PUT  /api/config         — update config fields (Pydantic validates, writes to file, hot-reloads)
POST /api/engine/start   — start the trading engine
POST /api/engine/stop    — stop the trading engine (keeps managing open positions until closed)
GET  /api/logs           — SSE stream: { event: "log", data: { level, timestamp, message } }
GET  /api/status/stream  — SSE stream: { event: "status", data: { ...status fields } }
```

Static files: FastAPI serves `frontend/dist/` at `/`. In dev, Vite dev server on `:5173` proxies `/api/*` to FastAPI on `:8501`.

## React Components (frontend/src/components/)
- **LicensePanel**: text input for license key, "Validate" button, green/red status dot
- **ControlPanel**: Start/Stop toggle button, lot mode radio (Risk % / Fixed) with contextual number inputs, MT5 + Supabase connection indicator dots
- **LogPanel**: auto-scrolling div, log entries color-coded by severity (INFO=gray, WARNING=yellow, ERROR=red), receives from SSE /api/logs
- **StatusBar**: numeric counts — active signals, pending orders, open positions, trailing positions

## Implementation Clarifications (decided during build, treat as final)

- **OrderStatus.CLOSED**: Added to the enum in `constants.py`. SQLite schema `status` column
  supports it. Reconciler uses it for "filled + gone from MT5" positions.

- **cancel_pending_order** added to `MT5Client` — uses `TRADE_ACTION_REMOVE`. Single attempt,
  no retry (cancellation is idempotent from the broker's perspective).

- **Partial close detection**: `FillDetector.detect_partial_close_tickets()` uses comment matching
  (`position.comment == "s{signal_id}"`) against positions not yet in SQLite. Does NOT parse
  `history_deals_get()`. This is simpler and reliable for ICMarkets hedging mode.

- **Pip size**: `symbol_info.point * 10` for instruments with `digits in (3, 5)` (FX and JPY);
  `symbol_info.point` for all others (metals, indices, crypto). Used in lot_calculator.py and
  will be needed for pip-unit TP threshold comparisons in the TP engine.

- **LotCalculator.calculate()** signature: `(stop_loss: float, limit_prices: list[float], mt5_symbol: str)`
  — takes primitives, not asyncpg.Record. Caller extracts fields before calling.

- **SQLiteDB**: holds a single persistent connection opened by `init_schema()`. Must be called
  once at startup before any other SQLiteDB method.

- **OffsetCalculator.check_drift()** threshold parameter is in absolute price units, not pips.
  Caller resolves it per asset class via `offset_drift_threshold(asset_class, config.offset_drift,
  db_sym)` — offset instruments are all non-forex, so the threshold is a dollar/point distance.

- **MarketScheduler** is defined in `bot/utils/time_utils.py`. `bot/core/scheduler.py` (step 28)
  imports it directly — no logic is duplicated there.

- **ICMarkets hedging: position ticket ≠ order ticket.** When an order fills, `position.identifier`
  == order ticket, but `position.ticket` is a NEW ID. SQLite stores the order ticket at placement.
  After fill detection, `sqlite.update_ticket(order_ticket, position_ticket)` rewrites the row so
  all downstream lookups (`{p.ticket: p for p in positions}`) work correctly.

- **Partial close remainder insert.** When `FillDetector.detect_partial_close_tickets()` returns a
  `NewTicketEvent`, sync_cycle inserts a new SQLite row with `limit_id = -new_ticket` (synthetic
  negative, guaranteed unique and never mistaken for a Supabase limit ID), `order_type = "remainder"`,
  then immediately calls `mark_filled()` and `set_trailing()`.

- **TP trigger uses price movement, not account P&L.** `profit_threshold` is compared to:
  - pips mode: `(bid - price_open) / pip_size` for longs, `(price_open - ask) / pip_size` for shorts
  - dollars mode: `bid - price_open` for longs, `price_open - ask` for shorts
  The "others combined P&L >= 0" guard uses `position.profit` (account currency).

- **db_symbol_from_mt5()** in `bot/trading/symbol_mapper.py` reverse-maps MT5 symbols to DB symbols
  (e.g., "BTCUSD" → "BTCUSDT", "AMD.NAS-24" → "AMD.NAS"). Required by TPEngine so it can call
  `detect_asset_class()` with the correct DB symbol.

- **symbol_suffixes** is a list of `{suffix, asset_classes}` rules. `map_symbol()` appends a rule's
  `suffix` to every resolved MT5 symbol (after symbol_map / stock_suffix resolution) whose detected
  asset class is listed in that rule — e.g. Exness "m" on forex/metals/crypto: EURUSD → EURUSDm,
  while indices/stocks stay bare. An asset class may appear in at most one rule (validated on load).
  `db_symbol_from_mt5()` reverses this by trying each suffix (longest first) and keeping the candidate
  that round-trips through `map_symbol()`. A legacy flat `universal_suffix` is migrated on load to one
  rule covering every asset class. Defaults to `[]` (no suffix).

- **close_position() and modify_position_sl()** added to `MT5Client`. close_position() uses
  `TRADE_ACTION_DEAL` with retry on transient errors. modify_position_sl() uses `TRADE_ACTION_SLTP`,
  single attempt (trailing retries naturally next cycle).

- **Adaptive polling.** Both sync_loop and tp_loop sleep `tp_active_interval_seconds` (1s) when
  `sqlite.get_all_active()` is non-empty; sleep `supabase_interval_seconds` (30s) when idle.

- **SyncCycle.run(placement_active=bool).** When False (engine stopped or license invalid), skips
  order placement and cancellation but still detects fills and partial close tickets.

- **Offset drift cancel-and-re-place.** Drifted pending orders are cancelled (status='cancelled').
  Since cancelled rows are excluded from `get_all_active()`, the limit reappears in `new_limit_ids`
  on the next cycle and is placed with the current offset.

- **SSEBroadcaster.last_msg** caches the most recent status broadcast. GET /api/status reads this
  cache — no MT5 call is made from the FastAPI handler (MT5 is engine-thread only).

- **Engine.app set externally.** main.py: `engine = Engine(...)`, `engine.app = create_app(engine)`,
  then starts the engine thread. `run_forever()` starts uvicorn only if `engine.app is not None`.

- **LicenseValidator dev bypass.** If `url` is empty string, `validate()` returns VALID immediately.
  Contributors without a live Edge Function can run the bot without license errors.

## DSN / Contributor Access

### DSN Loading (bot/config/settings.py)
1. Check for `.env` file -> load `SUPABASE_DSN` and `LICENSE_API_URL`
2. If no `.env` -> fall back to `_PRODUCTION_DSN` and `_PRODUCTION_LICENSE_URL` in `bot/config/constants.py`
3. Both constants are `""` (empty) in the repo. Owner fills them before compiling production binary.

### Owner's Production Build Workflow (document in CONTRIBUTING.md)
1. Edit `bot/config/constants.py`: set `_PRODUCTION_DSN` and `_PRODUCTION_LICENSE_URL` to real values
2. `cd frontend && npm run build` (produces `frontend/dist/`)
3. `pyinstaller bot.spec` (produces `.exe` with bundled dist/)
4. **Revert** `constants.py` to empty placeholders before pushing to git

### Contributor Role Setup (document in CONTRIBUTING.md with exact SQL)
1. Create read-only Postgres role in Supabase SQL editor:
   ```sql
   CREATE ROLE contributor_bot WITH LOGIN PASSWORD 'initial_password';
   GRANT USAGE ON SCHEMA public TO contributor_bot;
   GRANT SELECT ON signals, limits, live_prices, licenses TO contributor_bot;
   GRANT INSERT ON tp_outcomes TO contributor_bot;
   GRANT SELECT, INSERT, UPDATE ON users TO contributor_bot;
   ```
2. Monthly password rotation:
   ```sql
   ALTER ROLE contributor_bot WITH PASSWORD 'new_password_here';
   ```
3. Share new `.env` with contributors containing updated DSN

### .env Format
```
SUPABASE_DSN=postgresql://contributor_bot:PASSWORD@db.xxxxx.supabase.co:5432/postgres
LICENSE_API_URL=https://xxxxx.supabase.co/functions/v1/validate-license
```
