import asyncio
import json
import os
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

if sys.platform == "win32":
    import winreg
else:
    winreg = None  # type: ignore[assignment]

from bot.config.constants import BOT_VERSION
from bot.config.settings import Settings
from bot.trading.approx_lot import compute_recommendations

router = APIRouter()

_CONFIG_PATH = Path("config.json")

_STATUS_DEFAULTS: dict = {
    "engine_running": False,
    "trading_active": False,
    "license_valid": False,
    "license_status": "error",
    "license_message": "",
    "mt5_connected": False,
    "mt5_error": None,
    "supabase_connected": False,
    "supabase_error": None,
    "pending_count": 0,
    "open_count": 0,
    "trailing_count": 0,
    "bot_version": BOT_VERSION,
    "update_available": False,
    "update_version": None,
    "update_notes": "",
    "update_in_progress": False,
    "update_progress": 0,
    "update_error": None,
    "spread_hour_active": False,
    "sl_strip_active": False,
    "market_closed": False,
    "algo_trading_disabled": False,
    "symbol_count": 0,
}


@router.get("/api/status")
async def get_status(request: Request) -> dict:
    last = request.app.state.status_broadcaster.last_msg
    return last if last is not None else _STATUS_DEFAULTS


@router.get("/api/config")
async def get_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text())
    except FileNotFoundError:
        raise HTTPException(404, "config.json not found") from None


@router.put("/api/config")
async def update_config(config: Settings) -> dict:
    _CONFIG_PATH.write_text(config.model_dump_json(indent=2))
    return {"ok": True}


# Keys that survive a reset: credentials, install identity, broker/symbol wiring,
# and the applied-migrations list (the fresh template already embodies them).
_RESET_PRESERVE = (
    "license_key",
    "mt5_terminal_path",
    "instance_id",
    "disclaimer_accepted",
    "config_migrations",
    "symbol_map",
    "stock_suffix",
    "symbol_suffixes",
    "stock_no_suffix",
    "offset_instruments",
)


@router.post("/api/config/reset")
async def reset_config() -> dict:
    """Restore config.json to the bundled defaults, keeping license, terminal path,
    and broker/symbol wiring. Same template used for first-run config creation."""
    bundle_dir = Path(getattr(sys, "_MEIPASS", "."))
    template = bundle_dir / "config.example.json"
    if not template.exists():
        raise HTTPException(500, "bundled config template not found")
    try:
        current = json.loads(_CONFIG_PATH.read_text())
    except FileNotFoundError:
        raise HTTPException(404, "config.json not found") from None
    fresh = json.loads(template.read_text())
    for key in _RESET_PRESERVE:
        if key in current:
            fresh[key] = current[key]
    _CONFIG_PATH.write_text(json.dumps(fresh, indent=2))
    return {"ok": True}


@router.post("/api/disclaimer/accept")
async def accept_disclaimer() -> dict:
    """Persist the user's one-time risk-disclaimer acceptance. Targeted raw-JSON write
    so the rest of config.json is untouched (and the key is backfilled for existing
    installs that predate the field)."""
    try:
        data = json.loads(_CONFIG_PATH.read_text())
    except FileNotFoundError:
        raise HTTPException(404, "config.json not found") from None
    data["disclaimer_accepted"] = True
    _CONFIG_PATH.write_text(json.dumps(data, indent=2))
    return {"ok": True}


@router.post("/api/engine/start")
async def engine_start(request: Request) -> dict:
    request.app.state.engine.start()
    return {"ok": True}


@router.post("/api/engine/stop")
async def engine_stop(request: Request) -> dict:
    request.app.state.engine.stop()
    return {"ok": True}


@router.post("/api/engine/shutdown")
async def engine_shutdown(request: Request) -> dict:
    request.app.state.engine.shutdown()
    return {"ok": True}


@router.post("/api/update/install")
async def update_install(request: Request) -> dict:
    request.app.state.engine.start_update()
    return {"ok": True}


@router.post("/api/update/check")
async def update_check(request: Request) -> dict:
    await request.app.state.engine.check_updates_now()
    return {"ok": True}


@router.get("/api/dashboard")
async def get_dashboard(request: Request) -> dict:
    cache = request.app.state.engine.dashboard_cache
    data = cache.data
    return {
        "account": data.account,
        "positions": data.positions,
        "pending_orders": data.pending_orders,
        "nearby_signals": data.nearby_signals,
        "summary": data.summary,
        "updated_at": data.updated_at,
    }


class SignalActionBody(BaseModel):
    signal_id: int
    action: str  # 'skip' | 'manual' | 'none' (clear)


@router.post("/api/signals/action")
async def set_signal_action(request: Request, body: SignalActionBody) -> dict:
    """Persist a per-signal user override. 'skip' pulls and never places the signal;
    'manual' orphans it (bot stops managing); 'none' clears the override. The pull /
    re-place takes effect on the next sync cycle — MT5 is never touched here."""
    if body.action not in ("skip", "manual", "none"):
        raise HTTPException(400, "action must be 'skip', 'manual', or 'none'")
    sqlite = request.app.state.engine.sqlite
    if body.action == "none":
        await sqlite.clear_signal_action(body.signal_id)
    else:
        await sqlite.set_signal_action(body.signal_id, body.action)
    return {"ok": True}


@router.get("/api/history")
async def get_history(request: Request, from_date: str = "", to_date: str = "") -> dict:
    if not from_date:
        from_date = "2020-01-01T00:00:00"
    if not to_date:
        to_date = "2099-12-31T23:59:59"

    engine = request.app.state.engine
    rows = await engine.sqlite.get_order_history(from_date, to_date)

    trades = []
    total_pnl = 0.0
    wins = 0
    losses = 0
    breakevens = 0

    for row in rows:
        signal_id = row["signal_id"]
        signal_pnl = row["total_pnl"] or 0.0
        closed_count = row["closed_count"] or 0
        cancelled_count = row["cancelled_count"] or 0
        ch = row["channel_id"]
        # Signal status: "closed" if any limit was filled-then-closed; otherwise
        # "cancelled" (the whole signal was rejected/expired before any fill).
        status = "closed" if closed_count > 0 else "cancelled"
        # A signal the TM flattened at breakeven scores neither win nor loss: the
        # residual P&L is spread/commission, not a directional outcome. An exactly
        # flat close is the same — it must not sit in the win-rate denominator.
        breakeven = status == "closed" and (bool(row["breakeven_count"]) or signal_pnl == 0)
        trades.append(
            {
                "signal_id": signal_id,
                "symbol": row["symbol"] or "",
                "direction": row["direction"],
                "total_lots": row["total_lots"] or 0.0,
                "placed_at": row["placed_at"],
                "filled_at": row["first_filled_at"] or "",
                "closed_at": row["last_closed_at"] or "",
                "status": status,
                "breakeven": breakeven,
                "signal_type": row["signal_type"],
                "total_pnl": signal_pnl,
                "fills_count": closed_count,
                "cancelled_count": cancelled_count,
                "channel_id": str(ch) if ch is not None else None,
            }
        )
        if status == "closed":
            total_pnl += signal_pnl
            if breakeven:
                breakevens += 1
            elif signal_pnl > 0:
                wins += 1
            elif signal_pnl < 0:
                losses += 1

    decided = wins + losses
    win_rate = (wins / decided * 100) if decided > 0 else 0.0

    return {
        "trades": trades,
        "stats": {
            "total_trades": decided + breakevens,
            "wins": wins,
            "losses": losses,
            "breakevens": breakevens,
            "win_rate": round(win_rate, 1),
            "total_pnl": round(total_pnl, 2),
        },
    }


@router.post("/api/history/clear")
async def clear_history(request: Request) -> dict:
    """Reset the account to new — wipe all closed/cancelled trade history so every
    history- and dashboard-stat goes to zero. Live positions and pending orders are
    untouched (only terminal rows are removed)."""
    deleted = await request.app.state.engine.sqlite.clear_history()
    return {"ok": True, "deleted": deleted}


@router.get("/api/mt5/terminals")
async def list_mt5_terminals() -> dict:
    paths = await asyncio.to_thread(_collect_mt5_terminals)
    return {"paths": paths}


@router.get("/api/mt5/symbols")
async def list_mt5_symbols(request: Request) -> dict:
    # Served from the engine's cached catalogue — never calls MT5 from the handler.
    return {"symbols": request.app.state.engine.broker_symbols}


@router.get("/api/lot-sizing/approximate")
async def approximate_lot_sizes(request: Request, mode: str = "fixed") -> dict:
    """Lot suggestions that put a median signal near 5% account risk, one per supported
    broker symbol. `mode` is "fixed" (per-limit lots) or "total_lot" (total-per-signal
    lots). Computed from cached specs + balance — no MT5 call."""
    engine = request.app.state.engine
    acct = engine.dashboard_cache.data.account
    if not acct or not acct.get("balance"):
        raise HTTPException(409, "Account balance unavailable — connect MT5 and let a sync run")
    recs = compute_recommendations(
        engine.config,
        float(acct["balance"]),
        engine.lot_specs,
        engine.config.lot_sizing.max_lot_per_order,
        mode,
    )
    return {
        "balance": acct["balance"],
        "currency": acct.get("currency"),
        "exceptions": [
            {
                "symbol": r.symbol,
                "channel": "",
                "signal_type": r.signal_type,
                "mode": r.mode,
                "value": r.value,
            }
            for r in recs
        ],
    }


@router.get("/api/mt5/not-found-symbols")
async def list_not_found_symbols(request: Request) -> dict:
    # DB instruments with a live signal but no matching MT5 symbol on this broker.
    return {"symbols": request.app.state.engine.not_found_symbols}


def _collect_mt5_terminals() -> list[str]:
    found: set[Path] = set()
    _scan_origin_files(found)
    _scan_uninstall_registry(found)
    _scan_install_roots(found)
    return sorted(str(p) for p in found)


def _add_install_dir(install_dir: Path, found: set[Path]) -> None:
    candidate = install_dir / "terminal64.exe"
    try:
        if candidate.is_file():
            found.add(candidate.resolve())
    except OSError:
        pass


# ---- Source 1: %APPDATA%\MetaQuotes\Terminal\<hash>\origin.txt ----
# MT5 writes the absolute install path here for every terminal it has
# launched. Authoritative regardless of where the broker installed to.


def _scan_origin_files(found: set[Path]) -> None:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return
    meta_root = Path(appdata) / "MetaQuotes" / "Terminal"
    try:
        entries = list(meta_root.iterdir())
    except OSError:
        return
    for sub in entries:
        try:
            if not sub.is_dir():
                continue
        except OSError:
            continue
        if sub.name.lower() in {"common", "community"}:
            continue
        origin = sub / "origin.txt"
        try:
            raw = origin.read_bytes()
        except OSError:
            continue
        text = _decode_origin(raw).strip().strip('"')
        if text:
            _add_install_dir(Path(text), found)


def _decode_origin(raw: bytes) -> str:
    for enc in ("utf-16", "utf-8-sig", "utf-8", "mbcs", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


# ---- Source 2: Windows Uninstall registry ----
# Every MetaTrader installer (broker-branded or plain) writes a
# DisplayName + InstallLocation here. Covers terminals that exist but
# have never been launched, so origin.txt doesn't exist yet.

_REG_NAME_HINTS = ("metatrader", "mt5", "metaquotes")


def _scan_uninstall_registry(found: set[Path]) -> None:
    if winreg is None:
        return
    sub = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
    bases = (
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ | winreg.KEY_WOW64_64KEY),
        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ | winreg.KEY_WOW64_32KEY),
        (winreg.HKEY_CURRENT_USER, winreg.KEY_READ),
    )
    for hive, access in bases:
        try:
            base = winreg.OpenKey(hive, sub, 0, access)
        except OSError:
            continue
        try:
            _walk_uninstall_key(base, found)
        finally:
            base.Close()


def _walk_uninstall_key(base, found: set[Path]) -> None:
    i = 0
    while True:
        try:
            name = winreg.EnumKey(base, i)
        except OSError:
            return
        i += 1
        try:
            with winreg.OpenKey(base, name) as key:
                display = _read_reg_str(key, "DisplayName").lower()
                if not any(h in display for h in _REG_NAME_HINTS):
                    continue
                loc = _read_reg_str(key, "InstallLocation")
                if loc:
                    _add_install_dir(Path(loc), found)
                    continue
                uninstall = _read_reg_str(key, "UninstallString")
                parent = _parent_from_uninstall(uninstall)
                if parent is not None:
                    _add_install_dir(parent, found)
        except OSError:
            continue


def _read_reg_str(key, name: str) -> str:
    try:
        val, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return ""
    return str(val).strip().strip('"') if val else ""


def _parent_from_uninstall(uninstall: str) -> Path | None:
    s = uninstall.strip().strip('"')
    if not s:
        return None
    if s.lower().endswith(".exe"):
        try:
            return Path(s).parent
        except (OSError, ValueError):
            return None
    return None


# ---- Source 3: filesystem scan ----
# Fallback for portable installs that aren't in the registry and have
# never been launched. Only descends into directories whose name itself
# contains an MT5 token — keeps the scan tiny (no walking Program Files
# at large) and dodges permission/junction issues on unrelated trees.

_MT5_NAME_TOKENS = ("metatrader", "mt5", "metaquotes")
_FS_SCAN_MAX_DEPTH = 4


def _is_mt5_named(name: str) -> bool:
    n = name.lower()
    return any(t in n for t in _MT5_NAME_TOKENS)


def _scan_install_roots(found: set[Path]) -> None:
    roots: list[Path] = []
    seen: set[Path] = set()
    for env_var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "LOCALAPPDATA"):
        val = os.environ.get(env_var)
        if not val:
            continue
        root = Path(val)
        if root in seen:
            continue
        seen.add(root)
        if root.exists():
            roots.append(root)
    for root in roots:
        for sub in _safe_subdirs(root):
            if _is_mt5_named(sub.name):
                _scan_mt5_tree(sub, found, _FS_SCAN_MAX_DEPTH)


def _scan_mt5_tree(folder: Path, found: set[Path], depth: int) -> None:
    _add_install_dir(folder, found)
    if depth <= 0:
        return
    for sub in _safe_subdirs(folder):
        _scan_mt5_tree(sub, found, depth - 1)


def _safe_subdirs(path: Path) -> list[Path]:
    out: list[Path] = []
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        out.append(Path(entry.path))
                except OSError:
                    continue
    except OSError:
        return out
    return out


@router.get("/api/logs")
async def stream_logs(request: Request):
    gen = request.app.state.log_broadcaster.make_generator("log")
    return EventSourceResponse(gen)


@router.get("/api/status/stream")
async def stream_status(request: Request):
    gen = request.app.state.status_broadcaster.make_generator("status")
    return EventSourceResponse(gen)
