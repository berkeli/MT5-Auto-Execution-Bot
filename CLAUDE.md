# CLAUDE.md — MT5 Auto-Execution Bot

Python Windows desktop app that reads trading signals from Supabase PostgreSQL and places/manages pending orders on MetaTrader 5 via ICMarkets. FastAPI backend + React frontend at `localhost:8501`, system tray via pystray.

For users and a quick-start, see [README.md](README.md). For the full system design, see [ARCHITECTURE.md](ARCHITECTURE.md). For contributor setup and the production build workflow, see [CONTRIBUTING.md](CONTRIBUTING.md).

## Build & Run

### Development

```bash
pip install -r requirements.txt
python main.py
```

The bot opens `http://localhost:8501` automatically. For frontend live-reload:

```bash
cd frontend && npm install && npm run dev   # Vite at :5173, proxies /api/* to :8501
```

Tests and formatters:

```bash
pytest                              # backend tests
ruff check bot tests main.py        # lint
ruff format bot tests main.py       # format
cd frontend && npm run format       # prettier
cd frontend && npx tsc --noEmit     # type-check
```

### Production exe + auto-update release (owner only)

**`python release.py [VERSION] [--notes "..."]` does the whole build.** It bakes `SUPABASE_DSN`, `LICENSE_API_URL`, and `UPDATE_MANIFEST_URL` from `.env` into `bot/config/constants.py`, builds the frontend and the one-file exe, then restores `constants.py` to blank secrets (keeping the version) in a `finally` — so secrets never linger and `git checkout` is never needed. Output:

- `dist/MT5Bot.exe` — **distribute this to users** (version-agnostic name; the filename stays correct after the bot self-updates).
- `dist/releases/MT5Bot-<version>.exe` and `dist/releases/latest.json` — **upload both to the Supabase `releases` bucket**.

One-time prerequisites: fill `.env` (`SUPABASE_DSN`, `LICENSE_API_URL`, `UPDATE_MANIFEST_URL` — the public URL of `releases/latest.json`); `cd frontend && npm install`.

Per release:

1. `python release.py 1.3.3 --notes "what changed"` (omit the version to keep the current `BOT_VERSION`; passing it rewrites `BOT_VERSION` in `constants.py`).
2. Commit the version bump: `git add bot/config/constants.py && git commit -m "Bump version to 1.3.3"` (secrets are already blanked).
3. In the public Supabase Storage `releases` bucket, **delete the previous `MT5Bot-*.exe` + `latest.json`**, upload the new `dist/releases/MT5Bot-<version>.exe`, then upload `latest.json` **last** (so users never pull a manifest pointing at a not-yet-uploaded binary). When overwriting, set a low Cache-Control or the CDN may serve a stale manifest.
4. New users install via `dist/MT5Bot-Setup.exe` (see below), so there's nothing per-release to re-ship for first installs. Shipping the raw `dist/MT5Bot.exe` still works as a fallback but goes stale until the hourly self-update kicks in.

### Online installer for new users (`dist/MT5Bot-Setup.exe`)

`python build_installer.py` compiles `installer/MT5Bot.iss` with Inno Setup's ISCC, baking `UPDATE_MANIFEST_URL` from `.env` in as a `/D` define (nothing hardcoded in the committed script). The installer is **version-agnostic**: at install time it fetches `latest.json` (the same manifest the running bot polls), downloads `MT5Bot-<version>.exe`, lets Inno verify its SHA-256 during the download, and installs per-user to `%LOCALAPPDATA%\MT5Bot` with Start-Menu/Desktop shortcuts. So **build it once and re-upload only if the installer logic itself changes — never per release** (a new release just updates `latest.json`, which the installer reads live). Per-user install also keeps the self-updater working, since it needs write access to the exe's own folder (Program Files would need elevation). One-time prerequisite: install Inno Setup 6 (https://jrsoftware.org/isdl.php). Note: unsigned, so SmartScreen still shows "More info → Run anyway"; the installer reduces AV false-positives (small native download, the heavy PyInstaller payload comes from the bucket) but only code-signing clears SmartScreen.

How it reaches users: the running bot polls `UPDATE_MANIFEST_URL` hourly; when `version` exceeds the running `BOT_VERSION` it shows the banner "Update available" button → downloads, verifies SHA-256, self-replaces via a detached updater script, and relaunches (`bot/update/`). A fix to the updater itself only takes effect for users already running a build that contains it. The updater is frozen-only — in dev (`python main.py`) the install path raises a clear "packaged build only" error; test the *check* side against a scratch bucket via the `MT5BOT_UPDATE_URL` env override (`bot/config/settings.py:load_update_manifest_url`).

`dist/` is gitignored, so none of these artifacts get committed. Manual builds (`pyinstaller bot.spec`) still default to `dist/MT5Bot.exe`; only `release.py` produces the versioned `dist/releases/` artifacts.

### Edge Function deploy (owner only)

```bash
npm install -g supabase
supabase login
supabase link --project-ref <your-project-ref>
supabase functions deploy validate-license
```

The Edge Function reads `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` from the project environment automatically. `licenses` table schema:

```sql
CREATE TABLE licenses (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    license_key TEXT NOT NULL UNIQUE,
    mt5_account BIGINT NOT NULL,
    active      BOOLEAN NOT NULL DEFAULT true,
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`users` table (one row per license, UPSERTed by the bot every 5 min for leaderboard / TP optimization). `license_id` matches the `licenses.id` column type — change it if your `licenses.id` is something other than `BIGINT`:

```sql
CREATE TABLE users (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    license_id            BIGINT NOT NULL REFERENCES licenses(id) ON DELETE CASCADE,
    license_key           TEXT NOT NULL UNIQUE,
    mt5_account           BIGINT NOT NULL,
    balance               NUMERIC(14,2),
    equity                NUMERIC(14,2),
    currency              TEXT,
    leverage              INTEGER,
    open_positions_count  INTEGER NOT NULL DEFAULT 0,
    total_realized_pnl    NUMERIC(14,2) NOT NULL DEFAULT 0,
    total_trades          INTEGER NOT NULL DEFAULT 0,
    wins                  INTEGER NOT NULL DEFAULT 0,
    losses                INTEGER NOT NULL DEFAULT 0,
    win_rate              NUMERIC(5,2) NOT NULL DEFAULT 0,
    bot_version           TEXT,
    last_update_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Production role (used by the shipped .exe; DSN role is `execution_bot_ro`):
GRANT SELECT ON licenses TO execution_bot_ro;
GRANT SELECT, INSERT, UPDATE ON users TO execution_bot_ro;

-- Contributor role (used by `.env` setups in dev):
GRANT SELECT ON licenses TO contributor_bot;
GRANT SELECT, INSERT, UPDATE ON users TO contributor_bot;

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
CREATE POLICY users_insert_exec ON users FOR INSERT TO execution_bot_ro WITH CHECK (true);
CREATE POLICY users_update_exec ON users FOR UPDATE TO execution_bot_ro USING (true) WITH CHECK (true);
CREATE POLICY users_insert      ON users FOR INSERT TO contributor_bot   WITH CHECK (true);
CREATE POLICY users_update      ON users FOR UPDATE TO contributor_bot   USING (true) WITH CHECK (true);
```

The bot's UPSERT pulls `license_id` from `licenses` via the `license_key`, so each role needs SELECT on `licenses`. If the configured `license_key` isn't in `licenses`, the INSERT silently inserts zero rows. Policy names must be unique per table — that's why the production and contributor policies have distinct names even though they apply the same predicate.

## Key Constraints (Non-Negotiable)

- Supabase tables (`signals`, `limits`, `live_prices`) are **read-only** from the bot. `licenses` is read-only via SELECT (used by `users` UPSERT to resolve `license_id`); writes still go through the Edge Function. `tp_outcomes` is write-only (append-only analytics log; INSERT granted to `contributor_bot` plus a row-level-security INSERT policy on the same role, applied via DDL run in the Supabase SQL editor). `users` is upsert-only (one row per license, refreshed every 5 min; INSERT + UPDATE granted to `contributor_bot` with matching RLS policies).
- **`tp_outcomes` is two-staged per trade** (`stage` column): a `"trigger"` row when TP fires (partial/closed-portion `realized_pnl` + money-based `r_multiple`, MFE/MAE so far, `level_sequence`, `seconds_to_trigger`) and a `"final"` row written by `TPFinalizer.sweep` once the signal's last position goes flat (full-trade `realized_pnl`, final `r_multiple`, aggregate MFE/MAE, `hold_seconds`, `exit_reason`). The `final` write is idempotent via the local SQLite `signal_finalized` guard table. `r_multiple` is money-based: `realized_pnl / risk_money`, where `risk_money = price_distance_to_money(symbol_info, |avg_entry − stop_loss|, total_volume)` (`bot/trading/lot_calculator.py`); shared-SL multi-limit signals make `|avg_entry − stop_loss| · total_vol` the exact signal risk. Adding columns to `tp_outcomes` requires an `ALTER TABLE … ADD COLUMN IF NOT EXISTS` run by the owner in the Supabase SQL editor (the bot only INSERTs); the current extra columns are `stage, mfe_price, mfe_r, mae_price, mae_r, level_sequence, total_levels, seconds_to_trigger, hold_seconds, exit_reason` (plus the pre-existing `r_multiple`).
- All mutable transactional state lives in local SQLite (`orders.db`).
- No MT5 credentials in code or UI — always `mt5.initialize()` with no arguments.
- All MT5 orders use magic number `20250001`.
- `signal_type` (`standard`, `scalp`, `swing`, `toll`, `pa`, `1-1`, `risky`) is captured at placement time from Supabase `signals.type` and stored in SQLite; never re-read from Supabase.
- **Risky signals** (`tp_config.risky`, Settings → Risky, keyed on `signal_type == "risky"`, currently gold): TP is a normal trailing type with a dollar threshold (default $4), trailing distance, and partial-close % — resolved in `asset_config.get_config` with an optional per-asset-class `overrides` map (instrument_overrides' nested `"risky"` key still applies on top). **Custom SL** (`risky.stop_loss`, price-unit distance, `None` = use DB SL): overrides the DB stop-loss for every limit, measured from the signal's *deepest* limit (lowest for longs, highest for shorts) so all limits share one SL. Computed once per signal in `SyncCycle._risky_sl_map` and used for lot sizing, placement, and pending SL-change detection; filled positions on a custom SL are skipped by `_sync_filled_sls` (the placed SL stands, never synced back to the DB value). **Disable windows** (`risky.disabled_windows`, UTC `"HH:MM-HH:MM"`, defaults 21:55-23:10 / 00:55-02:00 / 11:55-14:00): `MarketScheduler.is_risky_disabled` gates `_is_blocked` (cancels pending, blocks placement — no crypto/24h exemption, unlike news/spread) and `_check_risky_window_exits` force-closes filled risky positions. Cancelled risky limits re-place after the window if still active in the DB. The **Risky Gold channel** (`1522144546299838524`) ships **enabled** — it was opt-in when introduced, so existing installs are switched on once by the `risky_gold_channel_enable_v1` migration, which drops it from `disabled_channels` (a user who then unchecks it keeps it off).
- **Follow Server TP** (`tp_config.follow_server_tp`, checkbox at the top of Settings → Take-profit, **on by default** — new installs get it via `config.example.json`, existing installs are switched on once by the `follow_server_tp_enable_v1` migration; a user who then unchecks it keeps it off): makes the signal service the TP trigger authority. `TPEngine._triggered` ignores `profit_threshold`/`others_pnl` entirely and fires when the signal is in `server_tp_signals` — the auto-TP'd set (`status='profit'` AND `closed_reason != 'manual'`) that `SyncCycle._check_forced_exits` publishes off its existing rev-gated status snapshot, so the feature adds **no Supabase egress**. `_tp_loop` passes the set into `run_cycle` each cycle. Everything downstream of the trigger is unchanged (partial close, trailing, `signal_tp_fired` + pending teardown, weekend profit flatten); trigger rows are tagged `tp_strategy='follow_server'`. Manual `profit` stays on the force-exit path (whole position closed), never routed through TP.
- Idempotent sync — running a cycle twice must have no additional effect.
- TP engine must never crash the main loop — log errors and continue. TP outcome writes are also non-fatal (Supabase failure is logged, sync continues).
- Spread adjustment applied to every order placement (see ARCHITECTURE.md).
- `lot_sizing.risk_percent`, `lot_sizing.fixed_lot`, and `lot_sizing.total_lot` accept either a flat `float` or a per-instrument dict (`{"XAUUSD": 0.3, "default": 1.0}`). Resolution order: exact MT5 symbol → `"default"` key → `1.0`.
- **Lot modes** (`lot_sizing.mode` and each exception's `mode`): `risk_percent` (sizes each limit to risk a % of balance), `fixed` (`fixed_lot` lots **per limit**), and `total_lot` (`total_lot` lots **split evenly across the signal's limits**, so more limits means less per limit and lower risk). The "Calculate approximate sizes" button serves both `fixed` and `total_lot`; the total-lot suggestion is the fixed per-limit value scaled by the assumed median limit count (`_AVG_LIMITS` in `bot/trading/approx_lot.py`).
- **The ladder divisor is `signals.total_limits`, never the visible limit rows** (`LotCalculator.calculate(ladder_size=...)`, passed from `_compute_signal_lots`). Both ladder-splitting modes (`total_lot`, and the `len` term in `risk_percent`) divide by the signal's *declared* limit count, because the signal service inserts a ladder in **several batches** — signal 3600 posted seq 1 and 4 at 00:51:21 and infilled seq 2/3 at 00:51:53, and the bot placed in between. Dividing by the rows visible at that moment sized the first two limits at `0.2/2` and the late pair at `0.2/4`, so the signal carried 0.3 lots against a 0.2 budget. `total_limits` is set at signal creation and is authoritative (never null/zero, never disagrees with the eventual row count), so it is correct from the first placement and needs no cancel/re-place. A declared count *below* the visible rows is ignored (`max(ladder_size, len(limit_prices))`) so a stale value can never over-size. The filled-sibling lot reuse in `_compute_signal_lots` still stands on top: it pins re-placed limits to their siblings' size against `risk_percent` balance/SL drift.
- **A ladder that changes size re-places its survivors** (`SyncCycle._cancel_resized_pending`). A TM message edit rewrites `signals.total_limits` to the edited level count and inserts only the genuinely new levels (`_plan_limit_diff` / `_apply_limit_diff` in the signal service), so a 4-limit ladder edited to 5 would otherwise leave four orders at `total/4` beside one at `total/5`. The placement-time count is persisted in the `order_mappings.ladder_size` column, and a pending whose stored count no longer matches the signal's is cancelled so it re-places at the new split next cycle (the same cancel-and-re-place route `_cancel_sl_changed_pending` uses; cancelled tickets go into `repriced_tickets` so the drift checks skip them). Keyed on the stored count, **not** on a recomputed lot: `risk_percent` depends on balance and SL distances, which drift on their own, and re-placing a live ladder because the balance crossed a volume step would be churn. Signals with a fill or a DB `hit` are spared via the shared `blocked_from_drift` set (their lot is pinned to the filled sibling anyway). Rows written before the column existed carry `NULL` and are skipped, so updating the bot doesn't re-place every open ladder on the first cycle. Note this depends on `signals_rev` moving — the TM's triggers bump it on every signals/limits write, which is what invalidates the cached signal set.
- **Risk defaults** (`risk_2_5_gold_total_lot_v1`): `lot_sizing.mode` is `risk_percent` at **2.5%** per limit, with one shipped exception putting **XAUUSD on `total_lot`** — gold ladders enough limits that per-limit risk sizing overshoots, so its whole signal takes one total lot split across them. That total is **auto-sized**: it ships as `value: 0` (the `AUTO_LOT_VALUE` sentinel), and `Engine._autofill_auto_lots` resolves it on the first sync cycle where the balance is known — same 5% median-signal model as the "Calculate approximate sizes" button (`approx_lot`, unchanged at `_TARGET_RISK_PCT = 5.0`) — then writes the lots back via `persist_auto_lot_values` (a targeted raw-JSON edit). It's a one-shot: the value is no longer the sentinel afterwards. Any total-lot exception left at 0 auto-sizes the same way, not just gold. Until it resolves the exception falls to the broker volume minimum — a conservative under-size for the cycle or two before the balance loads. Existing installs are migrated **only when `lot_sizing` still matches a shipped default exactly** (5% global risk plus at most the single XAUUSD risk-% exception every template has carried); a tuned percentage, a `fixed` mode, or the exception rows the approximate-sizes button writes all count as deliberate and are left alone (`_lot_sizing_untouched`).
- **v1.6.0 defaults** (data-driven; new installs / "Restore defaults" only — never migrated onto existing users, who keep every value they had): `lot_sizing.skip_limits_at` (default **0 = off**, N > 0 skips signals carrying ≥ N limits at placement — existing pendings/fills untouched, gated in `sync_cycle` after the unmanaged filter); `partial_close_percent` defaults to **50 = bank half at the TP trigger, trail the rest** across every normal path (top level, all seven asset classes, signal-type overrides, gold `XAUUSD` instrument overrides); the dedicated `risky` mode keeps its own **0 = trail-full** default. These ship in the bundled `config.example.json`; `migrate_config` leaves `lot_sizing` and `tp_config` *values* alone so an updating user's original settings survive — with two exceptions: an additive `setdefault` backfill of new `tp_config.instrument_overrides` keys (e.g. `DE40`/`UK100`/`XTIUSD` and higher-priced stocks), which only adds instruments a user hasn't already tuned; the `partial_close_50_v1` migration (`_flip_tp_partial_close_zeros`), which forces `partial_close_percent` to 50 wherever it's still at the old default of **0** (skipping the `risky` subtree), preserving any deliberate non-zero value; and `gold_toll_tp_v1`, which tightens gold's toll TP to a **$4 trigger / $2 trail** only where it still holds the $5/$5 every template has shipped. `POST /api/config/reset` restores the bundled template while preserving license/terminal-path/symbol wiring ("Restore defaults" button in Settings).
- `lot_sizing.exceptions` is a **list** of `{symbol, channel, signal_type, mode, value}` entries. Each dimension is optional: `symbol` (MT5 symbol, blank/`"all"` = any), `channel` (Supabase `channel_id` as a string, blank/`"all"` = any), and `signal_type` (`"all"` = any type). An exception overrides the global lot mode. **Resolution is most-specific-wins**, scoring each specified (non-wildcard) dimension: channel = 4, symbol = 2, signal_type = 1; the highest-scoring candidate that matches the trade wins (so a channel-only rule beats a symbol+type rule, while a channel+symbol+type rule beats both). Ties — only possible between true duplicates — resolve to the last-defined entry; no match falls through to the global mode. `channel_id` is passed into `LotCalculator.calculate(...)` from the signal. The legacy `{symbol: {mode, value}}` dict form is still accepted on load (coerced to a list with `signal_type="all"` via a `field_validator`).
- **Crypto symbols (BTCUSDT, ETHUSDT, anything `detect_asset_class()` classifies as `CRYPTO`) are exempt from spread-hour and news-mode gates** — they keep placing and stay live through those windows because the 24/7 crypto market doesn't have the same liquidity events. The exemption is news/spread-specific: the volatility guard still gates them (see below).
- **Volatility guard (opt-in)**: `bot_mode_status.vol_guard` is a second token column (same format as `news_mode` — comma-separated tokens or `ALL`, NULL when calm) written by the signal service's volatility monitor. Its tokens are whole **DB instruments** (`EURUSD`, `NAS100USD`, `BTCUSDT`, `AMD.NAS`), plus `ALL` for gold (market-wide). Only consumed when the user enables `config.volatility_guard` (Misc settings, **off by default**). When on, `SyncCycle` reads it alongside `news_mode` in the per-cycle sync-state poll (`SupabaseDB.fetch_sync_state`) and gates on it via the same parsing/matching (`parse_news_symbols`/`instrument_under_news`) and the same cancel + force-close paths as news — force-exits are tagged `force_vol` rather than `force_news`. When off, the column is read but ignored. No separate polling loop — it tracks the existing sync cadence (1s while any order/position is live).
- **News and volatility tokens are separate sets, not a union** (`SyncCycle._gate_symbols` → `_gated_by_news_or_vol`): the crypto / 24h-stock `_gate_exempt` carve-out is news-specific, so vol tokens are matched **before** it and news tokens after. Volatility is measured directly off price, so a move that already happened applies to a 24/7 instrument like any other. Merging the sets would silently drop every crypto and 24h-stock volatility token.
- **Gate tokens match on DB symbols, never broker symbols.** `is_blocked` resolves the instrument via `_CycleContext.instr_of` (the Supabase `instrument` for known limits, else `db_symbol_from_mt5`), and `_check_news_exits` reverse-maps the position's MT5 symbol the same way — so a `EURUSD` token gates a broker-suffixed `EURUSDm` position. Known gap: `db_symbol_from_mt5` is lossy when two DB symbols share one broker symbol (`USOILSPOT` and `XTIUSD` both map to broker `XTIUSD`, so a filled `XTIUSD` position resolves to `USOILSPOT`); it fails safe — no force-close, never a wrong one. Pending cancellation is unaffected (it uses the Supabase instrument directly).
- **Weekend force-close window**: when a signal's status flips to `cancelled`, the bot only force-closes filled positions if `MarketScheduler.is_weekend_window()` is True (Fri ≥15:55 EST through Sun <18:00 EST) **or** the position is on BTCUSD/crypto. Weekday cancellations on signals with fills are expected (Supabase extends expiry) so positions stay open. `breakeven` status closes unconditionally.
- **Spread-hour SL strip**: `SyncCycle._manage_spread_hour_sls` clears the broker-side stop-loss off every filled position (trailing included) during `MarketScheduler.is_sl_strip_window()` — opens ~5 min before the spread spike (`sl_strip_start` 16:55 forex / `sl_strip_stock_start` 15:55 stocks, before their 16:00 close) and closes at `daily_end` — so a spread-driven spike can't stop it out. When the window ends the SL is restored to the persisted `last_known_mt5_sl`; if price has genuinely moved past that level the position is closed at market instead (a bigger but rare loss). State lives in the SQLite `sl_stripped` flag (restart-safe); while set, both the TP loop and `_sync_filled_sls` skip the position so neither re-arms the stop. Crypto and `-24` 24h stocks are exempt (`_gate_exempt`).
- **Offset-drift throttle**: drift checks are gated per-order by `last_offset_check` and `config.offset_drift_check_interval_seconds` (default 1800s = 30 min). Prevents feed-mid jitter from churning the same order every sync cycle.
- **Index keyword tables** (`proximity.indices`, `offset_drift.indices`) substring-match the DB symbol, and the **longest matching key wins** (`_index_lookup` in `symbol_mapper.py`) — `AUS2000` contains `US2000`, so a first-match loop would silently hand it the Russell's distances. Every OANDA-fed index must also be listed in `offset_instruments`, or its limits go in on the feed's frame against a broker quoting points away (the failure that cost a JP225 ladder, and that F40 carried until `oanda_indices_backfill_v1`). Broker symbols are identical to the DB symbol for `F40`/`HK50`/`CHINA50`/`CHINAH`; only `AUS2000` needs a `symbol_map` entry (ICMarkets calls it `AUS200`).
- **Auto-update** (`bot/update/`): owner-driven and manual-confirm only — the bot detects a newer release from the HTTPS manifest and flags availability, but installs solely on explicit user click. Integrity is the mandatory SHA-256 from the manifest (we don't code-sign); a mismatch aborts and the old exe keeps running. Self-replace works only in the frozen build and needs write access to the exe's own folder (fine for a user-owned folder; Program Files would need elevation, out of scope). `_update_loop` and `_run_update` are catch-and-log — they never crash the main loop. The Supabase `releases` bucket is read over plain HTTPS, not the asyncpg pool.
- **TP `instrument_overrides`** key by **DB symbol** (e.g. `SPX500USD`, `NAS100USD`, `BTCUSDT`), not MT5 symbol. Note this differs from `lot_sizing.*` per-instrument dicts which key by MT5 symbol (e.g. `US500`). Each entry can be either flat (applies to all signal types) or nested per-signal-type:
  ```json
  "instrument_overrides": {
    "SPX500USD": { "profit_threshold": 15.0, "trailing_distance": 4.0 },
    "NAS100USD": {
      "default": { "profit_threshold": 50.0, "trailing_distance": 15.0 },
      "scalp":   { "profit_threshold": 30.0, "trailing_distance": 8.0 },
      "swing":   { "profit_threshold": 150.0, "trailing_distance": 40.0 }
    }
  }
  ```
  Detection rule: if any of `profit_threshold`/`trailing_distance`/`threshold_unit`/`partial_close_percent` sits at the top level, the entry is flat. Otherwise nested — lookup order is `signal_type` key → `"default"` key → no override (asset-class value stands). Each block may set any subset of fields; unspecified fields keep the value resolved earlier in the chain.

## Concurrency (Critical)

- **Main thread**: pystray (system tray icon, Windows message pump).
- **Engine thread**: asyncio event loop + MT5 (bound to this thread).
- MT5 calls are synchronous but <50ms, called directly in the async loop.
- FastAPI runs as an async task in the engine thread's event loop.
- **Never call MT5 from a FastAPI request handler** — use `DashboardCache` (populated each sync cycle) instead.
- Shutdown from UI triggers `engine.shutdown()` → cancels async tasks → calls `tray.stop()` via callback.

## Code Conventions

### Quality (non-negotiable)
- Production-grade code only. No placeholder implementations, no TODO-driven development.
- Minimal, concise implementations. If it can be done in 10 lines, don't write 30.
- No defensive programming unless justified by a real, documented failure case. Trust internal code.
- No "just in case" error handling. Validate at system boundaries (user input, MT5 responses, DB results), not between internal functions.
- Avoid unnecessary comments — only explain WHY, never WHAT. Well-named code is self-documenting.
- Don't add features, helpers, or abstractions beyond what the current task requires.

### Python (3.13)
- Modern type hints: `list[str]` not `List[str]`, `X | None` not `Optional[X]`.
- No docstrings except on Protocol methods.
- No `from __future__ import annotations` — use runtime type hints.
- Naming: `PascalCase` classes, `snake_case` functions, `UPPER_SNAKE` constants, `_private` with single underscore.
- Files: `snake_case.py`.
- Format with `ruff format`; lint with `ruff check`. Config in `pyproject.toml`.

### Async
- Supabase: asyncpg. SQLite: aiosqlite. MT5: synchronous, called directly in async context.
- Never use `loop.run_in_executor()` for MT5 — must run on the engine thread.
- Use `asyncio.sleep()`, never `time.sleep()` in async code.

### Error handling
- Trading operations: catch, log, continue (never crash the loop).
- DB: catch `asyncpg.PostgresError` and `aiosqlite.Error` separately.
- MT5: check `result.retcode == mt5.TRADE_RETCODE_DONE`, retry up to 3x on transient errors.
- Never bare `except:` — at least `except Exception:`.
- `logger.error(..., exc_info=True)` for unexpected exceptions.

### Dependency injection
- All classes take dependencies via constructor. No globals, no singletons.
- `Engine` wires everything together, passes instances down.
- For testing: pass mock `MT5Client`, in-memory SQLite.

### Config access
- Read `config.json` once per sync cycle (hot-reload).
- Pass the config snapshot as a parameter; don't re-read mid-cycle.
- Pydantic validates on load — if invalid, the previous valid config is kept.

### React / TypeScript
- Functional components only; custom hooks in `src/hooks/`.
- API calls centralised in `src/api.ts`.
- No state-management library — `useState` + `useEffect` is sufficient.
- SSE via native `EventSource` in a custom hook.
- Format with `npm run format` (Prettier). Config in `frontend/.prettierrc`.

### File organisation
- One class per file when the class is the primary export.
- Related small types may share a file (`types.py`, `models.py`).
- Tests mirror source: `bot/trading/lot_calculator.py` → `tests/test_lot_calculator.py`.
- SQL constants in `bot/db/queries.py`, never inline.

### Git
- Imperative commit messages (`Add TP engine`, not `Added TP engine`).
- One logical change per commit.
- Never commit: `.env`, `config.json`, `orders.db`, `node_modules/`, `dist/`, `build/`.

## Database Access

```python
# asyncpg: positional params, NOT a list
row = await conn.fetchrow("SELECT * FROM signals WHERE id = $1", signal_id)
# Timestamps are native datetime — DO NOT parse
created_at = row["created_at"]
# ROUND requires CAST
await conn.fetch("SELECT ROUND(CAST(price AS NUMERIC), 2) FROM ...")
```

```python
# aiosqlite: ? placeholders, tuple params, always commit
await db.execute(
    "INSERT INTO order_mappings (limit_id, signal_id) VALUES (?, ?)",
    (limit_id, signal_id),
)
await db.commit()
```

```python
# MT5: always filter by magic, always check retcode
orders = mt5.orders_get()
bot_orders = [o for o in (orders or []) if o.magic == MAGIC_NUMBER]
result = mt5.order_send(request)
if result.retcode != mt5.TRADE_RETCODE_DONE:
    logger.error("Order failed: %s - %s", result.retcode, result.comment)
mt5.initialize()  # attaches to already-running terminal — never pass credentials
```
