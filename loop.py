"""Trading loop entrypoint for the SolBot LIVE 5-bot engine (SOL-only).

This module steps the existing 5-bot pipeline using engine.py rules
ONLY. It introduces no new sizing, stop, take-profit, halt, or key
rule. It does NOT invent a live price feed and it does NOT invent a
swap route.

What this loop is
-----------------
A step engine plus safety gates. One step, in order:

  1. LIVE gate: LIVE_ENABLED must be exactly "1", or refuse.
  2. NETWORK gate: NETWORK must parse to devnet or mainnet, or refuse.
  3. Signer gate: exactly one of TRADING_PRIVATE_KEY / TRADING_KEYPAIR,
     validated by engine.load_signer_with_status(); derived pubkey
     must match wallet 9g9R6JvBkvuRAVHVehzoDcokA9R1sxRugiY6G3DkUTFQ,
     or refuse. No key material is ever logged.
  4. Halt gate: portfolio value in CAD via a live SOL price. A stale
     or missing SOL price refuses (trading blocked), never "above
     halt". On halt, sell ALL positions (each attempted, retried
     once), park proceeds in SOL, stop buying, and STOP THE LOOP —
     no new buys until James explicitly restarts it.
  5. Price-freshness gate: no buy and no (non-halt) sell on a price
     older than 30 seconds or missing.
  6. Swap-route gate: no buy or sell unless the configured route
     checker reports a proven route for that mint. The default
     route checker reports NO route, because devnet DEX routing was
     NOT verified in the sandbox (a Jupiter v6 devnet quote returned
     no route; Jupiter's current APIs are mainnet-focused). A
     deployment must supply a route checker that proves a route —
     an Orca/Raydium devnet pool, a direct DEX instruction, or a
     devnet test token with a real pool — before any trade.

Then, per step: Risk sells first (hard stop -7%, TP +10% slow /
hold 31-50% fast with 8% peak-drop in window, 12% past +50%, cap
+1000%), then Executor buys (full pipeline pass only, exactly 20%
of total current bankroll per coin, max 5 open, one per coin,
sells park in SOL).

What this loop is NOT
--------------------
* It is NOT started by the Dockerfile. The Dockerfile CMD remains
  the disabled guard. The loop is started later, as a separate
  deployment step, e.g. Railway Custom Start Command:
  ``python3 loop.py``
  Setting LIVE_ENABLED=1 alone does NOT start trading until that
  start command is configured AND a swap route is proven.
* It is NOT a live price feed. Step inputs (candidates, prices,
  SOL price, market speed) are supplied by the caller through the
  StepInput dataclass. In this build the default caller supplies
  NO feed: ``python3 loop.py`` with no feed configured refuses
  with ``no_live_feed_configured`` and trades nothing. A deployment
  must wire a real feed into step_loop() (or a subclass) — it
  must never be wired to an invented price.
* It is NOT a swap executor. Swap execution is an injected
  SwapExecutor. The default executor refuses every swap
  (``route_unverified``). A deployment must supply an executor
  that performs a real SOL swap on a proven route; this build
  performs no chain signing or sending itself.

Smoke-test mode
---------------
``python3 loop.py --smoke-test`` is READ-ONLY: it reports network,
RPC, wallet, live-gate status, and route-checker status, and
performs NO swap, NO signing, and NO trade. It does NOT require
LIVE_ENABLED=1 or a signer, because it must never sign. In this
build it exits non-zero with ``route_unverified`` because no
route has been proven — that is the correct, fail-closed result
for a deployment smoke test until a route is proven.

Logging: every step, gate decision, buy/sell intent, halt, and
refusal is logged with fixed error codes and public values only
(wallet pubkey, amounts, prices). No key material, secret
fragment, or derived secret is ever logged.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import (  # noqa: E402
    Candidate, Portfolio, Position, WALLET_PUBKEY, buy, can_buy,
    check_halt, evaluate_sell, get_network_config, is_live_enabled,
    load_signer_with_status, parse_network, pipeline_pass,
    price_is_fresh,
)

# Screener cadence: one pipeline step per 30-second window (RULES §3).
STEP_INTERVAL_SECONDS = 30

# Fixed error codes only — never include key material in any of these.
ERR_LIVE_DISABLED = "live_disabled"
ERR_INVALID_NETWORK = "invalid_network"
ERR_NO_LIVE_FEED = "no_live_feed_configured"
ERR_ROUTE_UNVERIFIED = "route_unverified"
ERR_SOL_PRICE_STALE = "sol_price_stale_or_missing"
ERR_HALT = "halt_at_$20_CAD"

logger = logging.getLogger("solbot-live-loop")


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


# ---------------------------------------------------------------------------
# Swap execution + route checking (abstractions, fail-closed by default)
# ---------------------------------------------------------------------------
class SwapExecutor:
    """Performs a real SOL swap. Base class refuses — deployments must
    subclass it with a proven-route executor. Never fake a swap."""

    def execute_buy(self, mint: str, sol_amount: float):
        """Return (ok, error_code_or_None)."""
        return False, ERR_ROUTE_UNVERIFIED

    def execute_sell(self, mint: str, tokens: float):
        """Return (ok, error_code_or_None)."""
        return False, ERR_ROUTE_UNVERIFIED


class RefusingSwapExecutor(SwapExecutor):
    """Default executor: refuses every swap (no proven route)."""


class RouteChecker:
    """Reports whether a SOL swap route is proven for a mint."""

    def check_route(self, mint: str):
        """Return (available: bool, reason: str)."""
        return False, ERR_ROUTE_UNVERIFIED


class UnverifiedRouteChecker(RouteChecker):
    """Default checker: no route proven (devnet routing unverified
    in the sandbox — Jupiter v6 devnet quote returned no route)."""


class FakeSwapExecutor(SwapExecutor):
    """Test / deployment-harness executor: records intents, moves no
    chain funds. Only for tests and for a deployment that replaces
    it with a real executor — never present this as a live swap."""

    def __init__(self):
        self.buys = []
        self.sells = []

    def execute_buy(self, mint: str, sol_amount: float):
        self.buys.append((mint, sol_amount))
        return True, None

    def execute_sell(self, mint: str, tokens: float):
        self.sells.append((mint, tokens))
        return True, None


class FakeRouteChecker(RouteChecker):
    """Test route checker with a caller-supplied availability flag."""

    def __init__(self, available: bool = True):
        self.available = available

    def check_route(self, mint: str):
        if self.available:
            return True, "route_proven_by_test_checker"
        return False, ERR_ROUTE_UNVERIFIED


# ---------------------------------------------------------------------------
# Step inputs / results
# ---------------------------------------------------------------------------
@dataclass
class StepInput:
    """Everything one loop step needs, supplied by the caller/feed.

    prices_sol: mint -> price_sol (scalar, as engine.total_value_sol
        expects); price_age_seconds: mint -> age_seconds.
    sol_price_usd / sol_price_age_seconds: live SOL price for halt.
    market_fast_by_mint: mint -> True (fast) / False (slow) / None
        (unknown — if unknown, only hard-stop and cap sells execute;
        slow-TP and peak-drop sells are held, per RULES §4).
    crash_watch_active: True blocks all new buys (RULES §5).
    usd_per_cad: CAD conversion rate (default engine fallback).
    """
    candidates: list = field(default_factory=list)
    prices_sol: dict = field(default_factory=dict)
    price_age_seconds: dict = field(default_factory=dict)
    sol_price_usd: float = None
    sol_price_age_seconds: float = None
    market_fast_by_mint: dict = field(default_factory=dict)
    crash_watch_active: bool = False
    usd_per_cad: float = 0.712


@dataclass
class StepResult:
    ok: bool
    error: str = ""
    buys: list = field(default_factory=list)   # (mint, sol_amount)
    sells: list = field(default_factory=list)  # (mint, reason, proceeds_sol)
    halted: bool = False
    blocked: list = field(default_factory=list)  # human-readable reasons


# ---------------------------------------------------------------------------
# Gates, in the required order
# ---------------------------------------------------------------------------
def check_gates(live_enabled: bool, network_value,
                signer_error):
    """Gate order: live, then network, then signer.

    signer_error is the error string from load_signer_with_status()
    (None on success). Returns (ok, error_code).
    """
    if not live_enabled:
        return False, ERR_LIVE_DISABLED
    try:
        parse_network(network_value)
    except ValueError:
        return False, ERR_INVALID_NETWORK
    if signer_error:
        # Only the fixed code prefix is used — never a value.
        return False, signer_error.split(":")[0]
    return True, ""


def check_gates_from_env():
    """Real gate check from the process environment, in order."""
    if not is_live_enabled():
        return False, ERR_LIVE_DISABLED
    try:
        parse_network(os.environ.get("NETWORK"))
    except ValueError:
        return False, ERR_INVALID_NETWORK
    _marker, signer_error = load_signer_with_status()
    if signer_error:
        return False, signer_error.split(":")[0]
    return True, ""


# ---------------------------------------------------------------------------
# One loop step
# ---------------------------------------------------------------------------
def step_loop(portfolio: Portfolio, step_input: StepInput,
              executor: SwapExecutor, route_checker: RouteChecker,
              live_enabled: bool = True) -> StepResult:
    """Run one gated step. Never trades on a failed gate.

    Sells are evaluated before buys. A halt sells everything (each
    position retried once via the executor) and stops the loop.
    A refused swap leaves the ledger unchanged.
    """
    blocked = []

    # Halt gate first inside the step: stale/missing SOL price is a
    # refusal (trading blocked), not "above halt".
    try:
        halted = check_halt(
            portfolio, step_input.prices_sol,
            step_input.sol_price_usd, step_input.sol_price_age_seconds,
            step_input.usd_per_cad)
    except ValueError:
        logger.warning("gate_refused reason=%s", ERR_SOL_PRICE_STALE)
        return StepResult(ok=False, error=ERR_SOL_PRICE_STALE,
                          blocked=[ERR_SOL_PRICE_STALE])
    if halted:
        logger.warning("halt reason=%s", ERR_HALT)
        sold, failed = _sell_all_on_halt_via_executor(
            portfolio, step_input, executor, route_checker)
        logger.warning("halt_sold=%s halt_failed=%s", sold, failed)
        return StepResult(ok=False, error=ERR_HALT, halted=True,
                          sells=[(m, ERR_HALT, p) for m, p in sold],
                          blocked=[ERR_HALT] + failed)

    sells = []
    # --- Risk sells first ---
    for mint in list(portfolio.positions.keys()):
        pos = portfolio.positions[mint]
        price_sol = step_input.prices_sol.get(mint)
        age = step_input.price_age_seconds.get(mint)
        if price_sol is None or age is None:
            blocked.append(f"{mint}:stale_or_missing_price")
            continue
        if not price_is_fresh(price_sol, age):
            blocked.append(f"{mint}:stale_or_missing_price")
            continue
        market_fast = step_input.market_fast_by_mint.get(mint)
        reason = evaluate_sell(pos, float(price_sol), age,
                               market_fast=bool(market_fast))
        if reason is None:
            continue
        if market_fast is None and reason not in (
                "hard_stop_-7%", "take_profit_cap_+1000%"):
            # Speed unknown: hold under hard stop / cap only (RULES §4).
            blocked.append(f"{mint}:market_speed_unknown_hold")
            continue
        route_ok, route_reason = route_checker.check_route(mint)
        if not route_ok:
            blocked.append(f"{mint}:{route_reason}")
            continue
        ok, exec_err = executor.execute_sell(mint, pos.tokens)
        if not ok:
            blocked.append(f"{mint}:{exec_err or ERR_ROUTE_UNVERIFIED}")
            continue
        proceeds = pos.value_sol(float(price_sol))
        portfolio.cash_sol += proceeds
        del portfolio.positions[mint]
        sells.append((mint, reason, proceeds))
        logger.info("sell mint=%s reason=%s proceeds_sol=%.6f",
                    mint, reason, proceeds)

    # --- Executor buys (full pipeline pass only) ---
    buys = []
    portfolio_value = portfolio.total_value_sol(step_input.prices_sol)
    for cand in step_input.candidates:
        if portfolio.halted:
            break
        if not can_buy(portfolio, cand, live_enabled=live_enabled):
            continue
        if step_input.crash_watch_active:
            blocked.append(f"{cand.mint}:crash_watch")
            continue
        if not pipeline_pass(cand, step_input.crash_watch_active):
            continue
        route_ok, route_reason = route_checker.check_route(cand.mint)
        if not route_ok:
            blocked.append(f"{cand.mint}:{route_reason}")
            continue
        # Recompute value per buy so sizing compounds within the step.
        portfolio_value = portfolio.total_value_sol(step_input.prices_sol)
        size = 0.20 * portfolio_value
        if size <= 0 or size > portfolio.cash_sol:
            blocked.append(f"{cand.mint}:insufficient_cash_sol")
            continue
        ok, exec_err = executor.execute_buy(cand.mint, size)
        if not ok:
            blocked.append(f"{cand.mint}:{exec_err or ERR_ROUTE_UNVERIFIED}")
            continue
        pos = buy(portfolio, cand, portfolio_value,
                  live_enabled=live_enabled)
        if pos is None:
            blocked.append(f"{cand.mint}:buy_refused_by_engine")
            continue
        buys.append((cand.mint, pos.invested_sol))
        logger.info("buy mint=%s size_sol=%.6f price_sol=%.6f",
                    cand.mint, pos.invested_sol, cand.price_sol)

    return StepResult(ok=True, buys=buys, sells=sells, blocked=blocked)


def _sell_all_on_halt_via_executor(portfolio: Portfolio,
                                   step_input: StepInput,
                                   executor: SwapExecutor,
                                   route_checker: RouteChecker):
    """Halt sells via the executor, each attempted + retried once.
    Failed positions are kept visible and recorded on the portfolio
    (mirrors engine.sell_all_on_halt, with the executor gate)."""
    portfolio.halted = True
    portfolio.halt_reason = ERR_HALT
    sold, failed = [], []  # sold entries are (mint, proceeds_sol)
    for mint in list(portfolio.positions.keys()):
        pos = portfolio.positions[mint]
        proceeds = None
        last_error = ""
        for _attempt in range(2):
            price_sol = step_input.prices_sol.get(mint)
            age = step_input.price_age_seconds.get(mint)
            if price_sol is None or age is None:
                last_error = "stale_or_missing_price"
                continue
            if not price_is_fresh(price_sol, age):
                last_error = "stale_or_missing_price"
                continue
            route_ok, route_reason = route_checker.check_route(mint)
            if not route_ok:
                last_error = route_reason
                continue
            ok, exec_err = executor.execute_sell(mint, pos.tokens)
            if not ok:
                last_error = exec_err or ERR_ROUTE_UNVERIFIED
                continue
            proceeds = pos.value_sol(float(price_sol))
            break
        if proceeds is None:
            failed.append(mint)
            portfolio.sell_failures.append({"mint": mint,
                                            "error": last_error})
            continue
        portfolio.cash_sol += proceeds
        del portfolio.positions[mint]
        sold.append((mint, proceeds))
    return sold, failed


# ---------------------------------------------------------------------------
# Smoke-test mode (read-only) and loop runner
# ---------------------------------------------------------------------------
def smoke_test(route_checker: RouteChecker = None) -> int:
    """Read-only smoke test: no swap, no signing, no trade.

    Reports network, RPC, wallet, live-gate status, and route status.
    Exits 0 only if a route is proven; in this build the default
    checker reports no route, so the correct result is non-zero
    (fail-closed) until a deployment proves a route.
    """
    checker = route_checker or UnverifiedRouteChecker()
    try:
        net = get_network_config()
    except ValueError:
        print("NETWORK: INVALID — refusing, fail closed")
        return 2
    print(f"Network: {net.name}")
    print(f"RPC: {net.rpc_url}")
    print(f"Wallet: {WALLET_PUBKEY}")
    print(f"LIVE_ENABLED: {'1 (on)' if is_live_enabled() else 'off'} "
          "(smoke test does not require live on, and never signs)")
    print("Currency: SOL ONLY — buys SOL -> token, sells token -> SOL")
    route_ok, route_reason = checker.check_route(WALLET_PUBKEY)
    print(f"Swap route: {'PROVEN' if route_ok else 'UNVERIFIED'} "
          f"({route_reason})")
    if not route_ok:
        print("SMOKE TEST: ROUTE UNVERIFIED — do not trade. Deployment "
              "must prove a devnet SOL swap route (Orca/Raydium "
              "devnet pool, direct DEX instruction, or a devnet test "
              "token with a real pool) before the first trade. "
              "Devnet DEX routing was NOT verified in the sandbox "
              "(Jupiter v6 devnet quote returned no route).")
        return 2
    print("SMOKE TEST: route reported proven by the configured "
          "checker — this does not by itself validate a signer or "
          "start trading.")
    return 0


def run_loop(interval_seconds: int = STEP_INTERVAL_SECONDS,
              max_steps: int = None,
              feed=None, executor=None, route_checker=None,
              portfolio=None) -> int:
    """Run the loop until halt, a gate refusal, or max_steps.

    feed, if supplied, must be a callable returning a StepInput for
    each step (the deployment's real price feed). executor and
    route_checker, if supplied, are the deployment's proven-route
    implementations; if omitted they default to refusing
    implementations, so the loop trades nothing on unverified
    routing. portfolio, if omitted, starts empty (a deployment
    must supply its funded starting cash — never invent it here).
    With no feed, this refuses immediately — a loop must never
    trade on invented prices.
    Returns 0 on a clean halt stop, 2 on any refusal.
    """
    configure_logging()
    ok, err = check_gates_from_env()
    if not ok:
        logger.warning("gate_refused reason=%s", err)
        return 2
    if feed is None:
        logger.warning("gate_refused reason=%s", ERR_NO_LIVE_FEED)
        return 2
    try:
        net = get_network_config()
    except ValueError:
        logger.warning("gate_refused reason=%s", ERR_INVALID_NETWORK)
        return 2
    logger.info("loop_start network=%s wallet=%s",
                net.name, WALLET_PUBKEY)
    if executor is None:
        executor = RefusingSwapExecutor()
    if route_checker is None:
        route_checker = UnverifiedRouteChecker()
    if portfolio is None:
        portfolio = Portfolio(cash_sol=0.0)
    step = 0
    while max_steps is None or step < max_steps:
        step += 1
        step_input = feed()
        result = step_loop(portfolio, step_input, executor, route_checker,
                           live_enabled=True)
        logger.info("step=%d ok=%s buys=%s sells=%s blocked=%s",
                    step, result.ok, result.buys, result.sells,
                    result.blocked)
        if result.halted:
            logger.warning("loop_stopped reason=%s", ERR_HALT)
            return 0
        if not result.ok:
            return 2
        if max_steps is None:
            time.sleep(interval_seconds)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="SolBot LIVE 5-bot loop (SOL-only, gated).")
    parser.add_argument("--smoke-test", action="store_true",
                        help="Read-only route/gate report; no swap, "
                        "no signing, no trade.")
    parser.add_argument("--interval-seconds", type=int,
                        default=STEP_INTERVAL_SECONDS,
                        help="Seconds between steps (default 30).")
    parser.add_argument("--max-steps", type=int, default=None,
                        help="Stop after this many steps (deployment "
                        "testing only).")
    args = parser.parse_args(argv)
    if args.smoke_test:
        return smoke_test()
    # No feed is wired in this build, so run_loop refuses with
    # no_live_feed_configured rather than trading on invented data.
    return run_loop(interval_seconds=args.interval_seconds,
                    max_steps=args.max_steps, feed=None)


if __name__ == "__main__":
    sys.exit(main())
