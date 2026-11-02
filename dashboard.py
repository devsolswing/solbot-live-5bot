"""Live-only dashboard for the SolBot LIVE 5-bot engine (SOL-only).

This is NOT the SolBot paper app. SolBot (artifact ``swingbot``) shows
PAPER teams only and is never touched by this module. This dashboard
shows the LIVE engine only — devnet first — with a clear LIVE label,
and it must never be mixed into SolBot's paper view.

Read-only, by design
--------------------
There are NO trading buttons, NO buy/sell actions, and NO
enable/disable controls in this dashboard. It only presents current
data. It cannot start, stop, enable, or trade for the engine. The
only HTTP methods served are GET / (HTML) and GET /snapshot.json
(JSON). Every other path returns 404 and every non-GET method
returns 405 — there is no POST /buy, /sell, /trade, or /enable
endpoint, and none may be added without James's explicit say-so.

What it shows (from the live engine's own state only)
-----------------------------------------------------
* Positions — open, per coin: size (SOL invested, tokens), entry
  price, current mark, value, unrealised P&L, gain, peak price.
* P&L — realised + unrealised, per coin and total, in SOL.
* Balance — SOL cash, positions value, portfolio total in SOL and
  (when a fresh SOL price exists) in CAD, plus halt distance:
  portfolio CAD minus the $20 CAD halt threshold (None when no
  fresh SOL price — never an invented CAD total).
* Coins in the pipeline — Screener -> Analyst -> Secondary ->
  Executor/Risk, per coin, with the stage each coin reached.
* Passed / Failed — per pipeline step, with reasons (blocked
  reasons from the loop step, e.g. stale price, route unverified,
  crash watch).
* W/L — wins / losses, per coin and total, from recorded sells.
  A sell is a win when proceeds exceed the recorded invested SOL
  for that position, a loss otherwise. A break-even sell (proceeds
  exactly equal to invested) counts as neither.
* Gate status — live on/off, network, signer validated or not
  (error code only, never a key), feed fresh or stale, route
  verified or not — so it is obvious at a glance why the engine
  is trading or refusing.
* Last step time, and staleness: feed older than 30s or a snapshot
  older than the loop step interval (default 30s) is marked STALE,
  and "no data yet" / "engine off" states are shown plainly when
  the loop has not run or is refusing. Positions, prices, and P&L
  are never invented: a missing mark shows as no mark, not a guess.

Snapshot format
---------------
The loop writes a snapshot (JSON) on every step, containing ONLY
public values: positions, prices, P&L, pipeline decisions, gate
statuses, timestamps. Never key material, seeds, signatures, or
private values. ``build_snapshot`` scrubs any dict key whose name
contains private/key/seed/signature/secret/token (except a token
MINT, which is public) before it reaches the snapshot, and the
tests pin that behaviour. The snapshot writer and the HTTP server
share the same scrubbed dict, so a leaked value cannot reach the
page through either path.

Deployment options (see RULES.md §15 for the full note)
-------------------------------------------------------
A. Thread inside the loop process (recommended): start the loop
   with ``--dashboard``; the server runs in a thread sharing the
   loop's DashboardState, so the page is always current. Port:
   $PORT (Railway) else $DASHBOARD_PORT else 8000.
B. Separate process, same image/service: the loop writes a
   snapshot JSON file on every step when DASHBOARD_SNAPSHOT_PATH
   is set, and ``python3 dashboard.py`` serves that file. The
   page shows "no data yet" until the first snapshot lands.
C. Second Railway service: NOT viable on its own — a second
   service has no shared file or memory with the loop service,
   so it would show "no data yet" forever unless a shared store
   is added (not built here). Do not deploy option C expecting
   live data.

The Dockerfile CMD is UNCHANGED by this build (disabled guard
only). Neither the dashboard nor the loop auto-starts on image
build; both are started explicitly by a deployment start command.
"""

from __future__ import annotations

import html
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import (  # noqa: E402
    HALT_BANKROLL_CAD, MAX_PRICE_AGE_SECONDS, WALLET_PUBKEY,
    pipeline_pass,
)

LOOP_STEP_INTERVAL_SECONDS = 30
DEFAULT_PORT = 8000

# Dict keys that must never reach a snapshot or a page, even if a
# caller passes them in. Matching is case-insensitive, substring.
_FORBIDDEN_KEY_SUBSTRINGS = (
    "private", "seed", "signature", "secret", "password", "keypair",
    "key_material",
)
# "key" alone is too broad (it would eat harmless keys), so it is
# handled separately: a key is forbidden only when it looks like a
# credential key, not when it is a generic mapping key.
_FORBIDDEN_KEY_EXACT = {
    "trading_private_key", "trading_keypair", "private_key",
    "signing_key", "api_key", "auth_token",
}


def _key_is_forbidden(key: str) -> bool:
    lowered = str(key).lower()
    if lowered in _FORBIDDEN_KEY_EXACT:
        return True
    if any(sub in lowered for sub in _FORBIDDEN_KEY_SUBSTRINGS):
        return True
    # A bare "key" / "signingkey"-style credential key, but NOT
    # "monkey"/"turkey"-style words and NOT generic "key" fields
    # that carry public data (e.g. a dict's own key label).
    if lowered in ("key", "signingkey", "secretkey"):
        return True
    return False


def scrub_public(value):
    """Recursively drop forbidden keys from a value.

    Returns a copy with no forbidden key present at any depth.
    Values under a forbidden key are dropped wholesale, never
    stringified into the snapshot.
    """
    if isinstance(value, dict):
        return {
            k: scrub_public(v)
            for k, v in value.items()
            if not _key_is_forbidden(k)
        }
    if isinstance(value, (list, tuple)):
        return [scrub_public(v) for v in value]
    return value


def pipeline_entries_for_candidate(cand) -> list:
    """Stage-by-stage pass/fail for one loop candidate.

    Stages: screener (+2% in 30s, not a >+500% bad tick), analyst
    (gained again within 5s), secondary (still gaining), then
    executor/risk (full pipeline pass, including liquidity floor
    and seller-dominance scrap). A stage fails and later stages
    are marked not-reached, matching the loop's short-circuit.
    """
    from engine import (  # noqa: E402
        LIQUIDITY_FLOOR_USD, SCREENER_MIN_GAIN_30S,
        SELLER_DOMINANCE_RATIO, SPIKE_MAX_GAIN_30S,
    )

    mint = getattr(cand, "mint", str(cand))
    gain = float(getattr(cand, "gain_30s", 0.0) or 0.0)
    screener_ok = SCREENER_MIN_GAIN_30S <= gain <= SPIKE_MAX_GAIN_30S
    screener_reason = (
        "ok" if screener_ok
        else ("gain_below_2pct" if gain < SCREENER_MIN_GAIN_30S
              else "spike_bad_tick")
    )
    entries = [{"mint": mint, "stage": "screener", "passed": screener_ok,
                "reason": screener_reason}]
    if not screener_ok:
        entries.append({"mint": mint, "stage": "analyst",
                        "passed": False, "reason": "not_reached"})
        entries.append({"mint": mint, "stage": "secondary",
                        "passed": False, "reason": "not_reached"})
        entries.append({"mint": mint, "stage": "executor_risk",
                        "passed": False, "reason": "not_reached"})
        return entries
    analyst_ok = bool(getattr(cand, "gained_again_5s", False))
    entries.append({"mint": mint, "stage": "analyst",
                    "passed": analyst_ok,
                    "reason": "ok" if analyst_ok else "no_gain_in_5s"})
    if not analyst_ok:
        entries.append({"mint": mint, "stage": "secondary",
                        "passed": False, "reason": "not_reached"})
        entries.append({"mint": mint, "stage": "executor_risk",
                        "passed": False, "reason": "not_reached"})
        return entries
    secondary_ok = bool(getattr(cand, "still_gaining", False))
    entries.append({"mint": mint, "stage": "secondary",
                    "passed": secondary_ok,
                    "reason": "ok" if secondary_ok else "not_still_gaining"})
    if not secondary_ok:
        entries.append({"mint": mint, "stage": "executor_risk",
                        "passed": False, "reason": "not_reached"})
        return entries
    full = pipeline_pass(cand)
    if full:
        reason = "ok"
    elif float(getattr(cand, "liquidity_usd", 0.0) or 0.0) < LIQUIDITY_FLOOR_USD:
        reason = "liquidity_below_floor"
    elif (float(getattr(cand, "buys", 0.0) or 0.0) > 0 and
          float(getattr(cand, "sells", 0.0) or 0.0) >=
          SELLER_DOMINANCE_RATIO * float(getattr(cand, "buys", 0.0))):
        reason = "seller_dominance_scrap"
    else:
        reason = "pipeline_failed"
    entries.append({"mint": mint, "stage": "executor_risk",
                    "passed": full, "reason": reason})
    return entries


def staleness(snapshot: dict, now: float = None) -> dict:
    """Freshness verdict for a snapshot.

    stale is True when the feed is older than 30s, or when the
    snapshot itself is older than the loop step interval (30s),
    or when there is no step timestamp at all.
    """
    now = now if now is not None else time.time()
    last_step = snapshot.get("last_step_at")
    feed_age = snapshot.get("feed_age_seconds")
    snapshot_age = None if last_step is None else now - float(last_step)
    reasons = []
    if last_step is None:
        reasons.append("no_step_yet")
    elif snapshot_age is not None and snapshot_age > LOOP_STEP_INTERVAL_SECONDS:
        reasons.append("snapshot_older_than_step_interval")
    if feed_age is not None and float(feed_age) > MAX_PRICE_AGE_SECONDS:
        reasons.append("feed_older_than_30s")
    return {
        "stale": bool(reasons),
        "snapshot_age_seconds": snapshot_age,
        "feed_age_seconds": feed_age,
        "reasons": reasons,
    }


class LiveDashboardState:
    """Mutable, public-only dashboard state, updated by the loop.

    The loop (or a test) calls record_buy / record_sell /
    record_step; snapshot() returns the scrubbed public dict the
    HTTP server serves. No key material is ever stored here.
    """

    def __init__(self):
        self.invested_by_mint = {}  # mint -> invested SOL (open only)
        self.wins_by_mint = {}
        self.losses_by_mint = {}
        self.realized_by_mint = {}
        self.total_wins = 0
        self.total_losses = 0
        self.realized_pnl_sol = 0.0
        self.last_snapshot = None
        self._lock = threading.Lock()

    def record_buy(self, mint: str, invested_sol: float) -> None:
        with self._lock:
            self.invested_by_mint[mint] = float(invested_sol)

    def record_sell(self, mint: str, proceeds_sol: float) -> None:
        """Record a sell: realised P&L + W/L for that coin."""
        with self._lock:
            invested = self.invested_by_mint.pop(mint, None)
            if invested is None:
                # Unknown entry: do not invent a P&L for it.
                return
            pnl = float(proceeds_sol) - float(invested)
            self.realized_by_mint[mint] = (
                self.realized_by_mint.get(mint, 0.0) + pnl)
            self.realized_pnl_sol += pnl
            if pnl > 0:
                self.wins_by_mint[mint] = self.wins_by_mint.get(mint, 0) + 1
                self.total_wins += 1
            elif pnl < 0:
                self.losses_by_mint[mint] = self.losses_by_mint.get(mint, 0) + 1
                self.total_losses += 1
            # Break-even (pnl == 0) counts as neither a win nor a loss.

    def record_step(self, snapshot: dict) -> None:
        with self._lock:
            self.last_snapshot = snapshot

    def snapshot(self) -> dict:
        with self._lock:
            if self.last_snapshot is None:
                return build_snapshot_empty()
            return self.last_snapshot


def build_snapshot(portfolio, prices_sol: dict = None,
                   sol_price_usd=None, sol_price_age_seconds=None,
                   usd_per_cad: float = 0.712,
                   candidates=(), blocked=(), buys=(), sells=(),
                   gate_status: dict = None, feed_status: str = "",
                   route_status: str = "", last_step_at: float = None,
                   last_error: str = "", engine_state: str = "running",
                   dashboard_state: LiveDashboardState = None) -> dict:
    """Build the public snapshot dict for one loop step.

    portfolio: engine.Portfolio (cash_sol + positions).
    prices_sol: mint -> current SOL-per-token mark (may be missing
        for a coin — that coin then shows no mark, never a guess).
    candidates: loop candidates for pipeline entries.
    blocked: loop step blocked reasons ("mint:reason").
    buys/sells: loop step (mint, size) / (mint, reason, proceeds).
    gate_status: dict with live_enabled/network/signer/route keys
        (error codes only — scrubbed again here regardless).
    dashboard_state: LiveDashboardState for realised P&L and W/L;
        when None, realised P&L is 0 and W/L is 0/0 (no history).
    """
    prices_sol = prices_sol or {}
    now = time.time()
    if last_step_at is None:
        last_step_at = now
    positions = []
    unrealized = 0.0
    positions_value = 0.0
    for mint, pos in (portfolio.positions or {}).items():
        mark = prices_sol.get(mint)
        if mark is None or float(mark) <= 0:
            # No mark: value at entry (engine's fail-closed rule),
            # flagged plainly — never an invented mark.
            value = pos.value_sol(pos.entry_price_sol)
            unrealized_coin = 0.0
            gain = 0.0
            stale_mark = True
        else:
            value = pos.value_sol(float(mark))
            unrealized_coin = value - pos.invested_sol
            gain = pos.gain(float(mark))
            stale_mark = False
        positions_value += value
        unrealized += unrealized_coin
        positions.append({
            "mint": mint,
            "invested_sol": pos.invested_sol,
            "tokens": pos.tokens,
            "entry_price_sol": pos.entry_price_sol,
            "current_price_sol": mark,
            "peak_price_sol": pos.peak_price_sol,
            "value_sol": value,
            "unrealized_pnl_sol": unrealized_coin,
            "gain_pct": gain * 100.0,
            "mark_stale_or_missing": stale_mark,
        })
    portfolio_total_sol = float(portfolio.cash_sol) + positions_value
    # CAD total only on a fresh SOL price; otherwise None (never
    # a stale-price CAD total that could hide a halt).
    sol_fresh = (
        sol_price_usd is not None and sol_price_age_seconds is not None
        and float(sol_price_usd) > 0
        and float(sol_price_age_seconds) <= MAX_PRICE_AGE_SECONDS
        and usd_per_cad and float(usd_per_cad) > 0
    )
    portfolio_total_cad = (
        portfolio_total_sol * float(sol_price_usd) / float(usd_per_cad)
        if sol_fresh else None
    )
    halt_distance_cad = (
        portfolio_total_cad - HALT_BANKROLL_CAD
        if portfolio_total_cad is not None else None
    )
    pipeline = []
    for cand in candidates or ():
        pipeline.extend(pipeline_entries_for_candidate(cand))
    # Blocked reasons that name a mint not in the pipeline get a
    # plain entry so nothing the loop blocked is hidden.
    pipeline_mints = {e["mint"] for e in pipeline}
    failures = []
    for reason in blocked or ():
        failures.append(str(reason))
        if ":" in str(reason):
            mint, why = str(reason).split(":", 1)
            if mint not in pipeline_mints:
                pipeline.append({"mint": mint, "stage": "blocked",
                                 "passed": False, "reason": why})
    passed = sum(1 for e in pipeline if e["passed"])
    failed = sum(1 for e in pipeline if not e["passed"])
    if dashboard_state is not None:
        realized = dashboard_state.realized_pnl_sol
        wins = dashboard_state.total_wins
        losses = dashboard_state.total_losses
        wins_by_mint = dict(dashboard_state.wins_by_mint)
        losses_by_mint = dict(dashboard_state.losses_by_mint)
        realized_by_mint = dict(dashboard_state.realized_by_mint)
    else:
        realized, wins, losses = 0.0, 0, 0
        wins_by_mint, losses_by_mint, realized_by_mint = {}, {}, {}
    total_games = wins + losses
    snapshot = {
        "dashboard": "LIVE 5-bot engine — LIVE ONLY, NOT SolBot paper",
        "currency": "SOL only",
        "wallet": WALLET_PUBKEY,
        "network": (gate_status or {}).get("network", ""),
        "engine_state": engine_state,
        "generated_at": now,
        "last_step_at": last_step_at,
        "last_error": last_error,
        "gate_status": gate_status or {},
        "feed_status": feed_status,
        "feed_age_seconds": sol_price_age_seconds,
        "route_status": route_status,
        "balance": {
            "cash_sol": float(portfolio.cash_sol),
            "positions_value_sol": positions_value,
            "portfolio_total_sol": portfolio_total_sol,
            "portfolio_total_cad": portfolio_total_cad,
            "halt_threshold_cad": HALT_BANKROLL_CAD,
            "halt_distance_cad": halt_distance_cad,
            "halted": bool(getattr(portfolio, "halted", False)),
        },
        "positions": positions,
        "pnl": {
            "realized_pnl_sol": realized,
            "unrealized_pnl_sol": unrealized,
            "total_pnl_sol": realized + unrealized,
            "per_coin_realized_pnl_sol": realized_by_mint,
        },
        "pipeline": pipeline,
        "passed_failed": {
            "passed": passed,
            "failed": failed,
            "failures": failures,
        },
        "win_loss": {
            "wins": wins,
            "losses": losses,
            "win_rate_pct": (wins / total_games * 100.0
                             if total_games else None),
            "per_coin": {
                mint: {
                    "wins": wins_by_mint.get(mint, 0),
                    "losses": losses_by_mint.get(mint, 0),
                    "realized_pnl_sol": realized_by_mint.get(mint, 0.0),
                }
                for mint in sorted(set(wins_by_mint) | set(losses_by_mint)
                                   | set(realized_by_mint))
            },
        },
        "recent_buys": [{"mint": m, "size_sol": s} for m, s in (buys or ())],
        "recent_sells": [{"mint": m, "reason": r, "proceeds_sol": p}
                         for m, r, p in (sells or ())],
    }
    return scrub_public(snapshot)


def build_snapshot_empty(network: str = "", gate_status: dict = None) -> dict:
    """The 'no data yet' snapshot: no positions, no prices, no P&L
    invented — every value plainly empty or None."""
    return scrub_public({
        "dashboard": "LIVE 5-bot engine — LIVE ONLY, NOT SolBot paper",
        "currency": "SOL only",
        "wallet": WALLET_PUBKEY,
        "network": network,
        "engine_state": "no_data_yet",
        "generated_at": time.time(),
        "last_step_at": None,
        "last_error": "",
        "gate_status": gate_status or {},
        "feed_status": "no_data_yet",
        "feed_age_seconds": None,
        "route_status": "no_data_yet",
        "balance": {
            "cash_sol": None,
            "positions_value_sol": None,
            "portfolio_total_sol": None,
            "portfolio_total_cad": None,
            "halt_threshold_cad": HALT_BANKROLL_CAD,
            "halt_distance_cad": None,
            "halted": False,
        },
        "positions": [],
        "pnl": {"realized_pnl_sol": None, "unrealized_pnl_sol": None,
                "total_pnl_sol": None, "per_coin_realized_pnl_sol": {}},
        "pipeline": [],
        "passed_failed": {"passed": 0, "failed": 0, "failures": []},
        "win_loss": {"wins": 0, "losses": 0, "win_rate_pct": None,
                     "per_coin": {}},
        "recent_buys": [],
        "recent_sells": [],
    })


def write_snapshot_file(snapshot: dict, path: str) -> None:
    """Atomically-ish write a snapshot JSON file (temp + replace)."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(scrub_public(snapshot), fh)
    os.replace(tmp_path, path)


def read_snapshot_file(path: str):
    """Read a snapshot file, or return the empty snapshot if the
    file is missing/corrupt — never crash the server on bad data."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return scrub_public(data)
    except Exception:  # noqa: BLE001 - corrupt/missing file = no data
        pass
    return build_snapshot_empty()


# ---------------------------------------------------------------------------
# HTTP server (read-only, stdlib only)
# ---------------------------------------------------------------------------
def render_html(snapshot: dict) -> str:
    """Phone-readable HTML for one snapshot. All values escaped."""
    stale = staleness(snapshot)
    state = snapshot.get("engine_state", "no_data_yet")
    bal = snapshot.get("balance", {}) or {}
    pnl = snapshot.get("pnl", {}) or {}
    wl = snapshot.get("win_loss", {}) or {}
    pf = snapshot.get("passed_failed", {}) or {}

    def fmt(value, decimals=4):
        if value is None:
            return "—"
        try:
            return f"{float(value):.{decimals}f}"
        except (TypeError, ValueError):
            return html.escape(str(value))

    def fmt_cad(value):
        if value is None:
            return "— (no fresh SOL price)"
        return f"${float(value):.2f} CAD"

    rows = ""
    for pos in snapshot.get("positions", []) or []:
        mark = ("STALE/MISSING" if pos.get("mark_stale_or_missing")
                else fmt(pos.get("current_price_sol"), 6))
        rows += (
            f"<tr><td>{html.escape(str(pos.get('mint')))}</td>"
            f"<td>{fmt(pos.get('invested_sol'))} SOL</td>"
            f"<td>{fmt(pos.get('tokens'), 2)}</td>"
            f"<td>{fmt(pos.get('entry_price_sol'), 6)}</td>"
            f"<td>{mark}</td>"
            f"<td>{fmt(pos.get('value_sol'))} SOL</td>"
            f"<td>{fmt(pos.get('unrealized_pnl_sol'))} SOL "
            f"({fmt(pos.get('gain_pct'), 2)}%)</td></tr>"
        )
    if not rows:
        rows = "<tr><td colspan='7'>No open positions.</td></tr>"

    pipe_rows = ""
    for entry in snapshot.get("pipeline", []) or []:
        pipe_rows += (
            f"<tr><td>{html.escape(str(entry.get('mint')))}</td>"
            f"<td>{html.escape(str(entry.get('stage')))}</td>"
            f"<td>{'PASS' if entry.get('passed') else 'FAIL'}</td>"
            f"<td>{html.escape(str(entry.get('reason')))}</td></tr>"
        )
    if not pipe_rows:
        pipe_rows = "<tr><td colspan='4'>No coins in the pipeline.</td></tr>"

    wl_rows = ""
    for mint, rec in (wl.get("per_coin", {}) or {}).items():
        wl_rows += (
            f"<tr><td>{html.escape(str(mint))}</td>"
            f"<td>{rec.get('wins', 0)}</td><td>{rec.get('losses', 0)}</td>"
            f"<td>{fmt(rec.get('realized_pnl_sol'))} SOL</td></tr>"
        )
    if not wl_rows:
        wl_rows = "<tr><td colspan='4'>No closed trades yet.</td></tr>"

    gates = snapshot.get("gate_status", {}) or {}
    gates_html = "".join(
        f"<li>{html.escape(str(k))}: {html.escape(str(v))}</li>"
        for k, v in gates.items()
    ) or "<li>No gate data yet.</li>"
    last_step = snapshot.get("last_step_at")
    last_step_text = (
        time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(last_step))
        if last_step else "never"
    )
    stale_banner = (
        "<p><b>⚠ STALE DATA</b> — feed or snapshot older than the "
        "loop step interval; do not act on these numbers.</p>"
        if stale["stale"] else ""
    )
    state_banner = ""
    if state in ("no_data_yet", "engine_off", "refusing"):
        state_banner = (
            f"<p><b>Engine state: {html.escape(state)}</b> — "
            "no live data yet, or the loop is refusing (see gates "
            "below). Nothing shown here is a trade.</p>"
        )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LIVE 5-bot dashboard (devnet)</title>
<style>body{{font-family:sans-serif;margin:12px}}table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:4px;font-size:13px;word-break:break-all}}h2{{font-size:16px}}</style>
</head><body>
<h1>🔴 LIVE 5-bot dashboard — LIVE ONLY, NOT SolBot paper</h1>
<p>Network: {html.escape(str(snapshot.get('network')))} · Wallet: {html.escape(str(snapshot.get('wallet')))} · SOL only · Read-only: no trading buttons, no enable controls.</p>
{stale_banner}{state_banner}
<p>Last step: {html.escape(last_step_text)} · Last error: {html.escape(str(snapshot.get('last_error') or '—'))} · Feed: {html.escape(str(snapshot.get('feed_status')))} · Route: {html.escape(str(snapshot.get('route_status')))}</p>
<h2>Balance</h2>
<p>Cash: {fmt(bal.get('cash_sol'))} SOL · Positions: {fmt(bal.get('positions_value_sol'))} SOL · Total: {fmt(bal.get('portfolio_total_sol'))} SOL ({fmt_cad(bal.get('portfolio_total_cad'))}) · Halt at $20 CAD — distance: {fmt_cad(bal.get('halt_distance_cad'))} · Halted: {bal.get('halted')}</p>
<h2>P&amp;L (SOL)</h2>
<p>Realised: {fmt(pnl.get('realized_pnl_sol'))} · Unrealised: {fmt(pnl.get('unrealized_pnl_sol'))} · Total: {fmt(pnl.get('total_pnl_sol'))}</p>
<h2>Positions</h2>
<table><tr><th>Coin</th><th>Invested</th><th>Tokens</th><th>Entry</th><th>Mark</th><th>Value</th><th>Unrealised P&amp;L</th></tr>{rows}</table>
<h2>Pipeline</h2>
<table><tr><th>Coin</th><th>Stage</th><th>Pass/Fail</th><th>Reason</th></tr>{pipe_rows}</table>
<p>Passed: {pf.get('passed', 0)} · Failed: {pf.get('failed', 0)} · Failures: {html.escape(', '.join(pf.get('failures', [])) or '—')}</p>
<h2>Win / Loss</h2>
<p>Total wins: {wl.get('wins', 0)} · Losses: {wl.get('losses', 0)} · Win rate: {fmt(wl.get('win_rate_pct'), 2) if wl.get('win_rate_pct') is not None else '—'}%</p>
<table><tr><th>Coin</th><th>Wins</th><th>Losses</th><th>Realised P&amp;L</th></tr>{wl_rows}</table>
<h2>Gate status</h2><ul>{gates_html}</ul>
<p><a href="/snapshot.json">JSON snapshot</a> · This page never trades, never signs, never shows a key.</p>
</body></html>"""


def make_handler(get_snapshot):
    """Request handler factory. get_snapshot() -> snapshot dict."""

    class DashboardHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep logs quiet
            pass

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                snapshot = get_snapshot()
                body = render_html(snapshot).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/snapshot.json":
                snapshot = get_snapshot()
                body = json.dumps(scrub_public(snapshot)).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            self.send_response(405)
            self.end_headers()

        def do_PUT(self):
            self.send_response(405)
            self.end_headers()

        def do_DELETE(self):
            self.send_response(405)
            self.end_headers()

        def do_PATCH(self):
            self.send_response(405)
            self.end_headers()

    return DashboardHandler


def dashboard_port() -> int:
    """Port: $PORT (Railway) else $DASHBOARD_PORT else 8000."""
    for var in ("PORT", "DASHBOARD_PORT"):
        raw = os.environ.get(var, "").strip()
        if raw:
            try:
                return int(raw)
            except ValueError:
                continue
    return DEFAULT_PORT


def serve_snapshot_callable(get_snapshot, port: int = None,
                            host: str = "0.0.0.0"):
    """Start a threaded read-only server; returns the server object."""
    server = ThreadingHTTPServer((host, port or dashboard_port()),
                                 make_handler(get_snapshot))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def serve_snapshot_file(path: str, port: int = None):
    """Serve snapshots from a JSON file the loop writes (option B)."""
    return serve_snapshot_callable(lambda: read_snapshot_file(path),
                                   port=port)


def main(argv=None) -> int:
    """Standalone dashboard server (option B): serves a snapshot
    file written by the loop. Usage:
    ``python3 dashboard.py [--snapshot-file PATH] [--port N]``
    Shows 'no data yet' until the first snapshot lands. Read-only."""
    import argparse

    parser = argparse.ArgumentParser(
        description="LIVE 5-bot dashboard server (read-only, no trading).")
    parser.add_argument("--snapshot-file", default=os.environ.get(
        "DASHBOARD_SNAPSHOT_PATH", "live_dashboard_snapshot.json"))
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)
    server = serve_snapshot_file(args.snapshot_file, port=args.port)
    print(f"LIVE dashboard (read-only) on port {server.server_address[1]} "
          f"serving snapshot file {args.snapshot_file} — "
          "no trading buttons, no enable controls, no keys.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
