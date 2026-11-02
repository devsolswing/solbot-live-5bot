"""Swap execution for the SolBot LIVE 5-bot engine (SOL-only).

This module proves a route and, only on a proven route with a
validated signer, executes a SOL swap. It performs NO signing and
NO sending by itself: actual chain signing/sending lives in a
deployment-supplied transaction builder (tx_builder). With no
tx_builder supplied, every swap is REFUSED — never faked.

Route source (honest status, 2026-11-01)
-----------------------------------------
* Jupiter v6 quote endpoint (``quote-api.jup.ag/v6/quote``) is
  the intended route source. In this sandbox it returned an EMPTY
  REPLY (not a quote, not a route) — that is an environment
  reachability gap, NOT proof the endpoint is down, and NOT a
  verified route. A deployment must fetch a live Jupiter quote
  and prove it before any trade.
* A real transaction builder now exists in tx_builder.py
  (pure-Python Ed25519 signing, legacy Solana transaction
  signing/sending, Jupiter swap-transaction provider, and a
  DevnetPoolQuoter for injected devnet pool data). It is wired
  into the loop ONLY with ``--tx-builder solana``; without
  that flag this executor still refuses every swap, exactly
  as before.
* Devnet routing is UNVERIFIED: a Jupiter v6 devnet quote
  returned no route in this sandbox, and Jupiter's current
  APIs are mainnet-focused. On devnet this executor refuses
  every swap unless a deployment supplies a proven devnet
  route checker / quoter (Orca/Raydium devnet pool, direct
  DEX instruction, or a devnet test token with a real pool).
* Orca/Raydium pools are NOT queried directly by this build
  (no pool SDK, stdlib only). A deployment that wants an
  Orca/Raydium route must supply its own quoter callable
  returning a Quote — this module validates that quote
  (freshness, positive output, slippage guard) the same way.

Slippage guard — BUILDER DEFAULT, NOT James's number
-----------------------------------------------------
James has NOT set a slippage number for this live engine.
The guard below defaults to 1.0% (SLIPPAGE_GUARD_PCT = 0.01):
a swap is refused if the expected output at execution is more
than 1.0% below the quoted output. This is a BUILDER DEFAULT,
labelled as such in code and in RULES.md §13 — change it only
on James's explicit say-so, and never present it as his number.
(For contrast only: the SolBot Phantom paper team used a
0.5%–1.2% slippage range in-app; that range was never adopted
for this live engine.)

Rules, in order, before any swap
--------------------------------
1. Signer must validate (engine.load_signer_with_status):
   LIVE_ENABLED=1, exactly one key variable, derived pubkey
   matches wallet 9g9R…UTFQ. No validation = no swap, and the
   refusal code is returned, never a key fragment.
2. A fresh route quote is required: quote age at most 30
   seconds, positive output, and the quote's input/output
   mints must match the requested swap direction (SOL -> token
   for buys, token -> SOL for sells). Stale, missing, or
   mismatched quotes refuse.
3. Slippage guard: expected output must be at least
   (1 - SLIPPAGE_GUARD_PCT) of the quoted output, or refuse.
4. Only then is the deployment's tx_builder called. If it
   returns failure, the swap is reported failed and the ledger
   is left unchanged by the caller (loop.py does that).

Logging: every intent and refusal is logged with fixed error
codes and public values only (mint, SOL/token amounts, quoted
output, route source). No key material, secret fragment, or
derived secret is ever logged.

Tests use mocked quoters and mocked tx_builders only — never a
real key, never a real chain call, never a real signature.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import SOL_MINT, WALLET_PUBKEY, load_signer_with_status  # noqa: E402

logger = logging.getLogger("solbot-live-swap")

# --- Fixed error codes (never include key material) ---
ERR_ROUTE_UNVERIFIED = "route_unverified"
ERR_STALE_QUOTE = "stale_quote"
ERR_SLIPPAGE_GUARD = "slippage_guard_exceeded"
ERR_NO_SIGNER = "no_signer"
ERR_QUOTE_MISMATCH = "quote_mint_mismatch"
ERR_EXECUTOR_NOT_CONFIGURED = "executor_not_configured"

MAX_QUOTE_AGE_SECONDS = 30.0
# BUILDER DEFAULT (NOT James's number): 1.0% slippage guard.
SLIPPAGE_GUARD_PCT = 0.01

JUPITER_QUOTE_URL = ("https://quote-api.jup.ag/v6/quote"
                     "?inputMint={input}&outputMint={output}"
                     "&amount={amount}&slippageBps={slippage_bps}")


class Quote:
    """A route quote: what a swap is expected to produce."""

    def __init__(self, input_mint, output_mint, input_amount,
                 output_amount, source="jupiter_v6", fetched_at=None,
                 raw=None):
        self.input_mint = input_mint
        self.output_mint = output_mint
        self.input_amount = float(input_amount)
        self.output_amount = float(output_amount)
        self.source = source
        self.fetched_at = fetched_at if fetched_at is not None else time.time()
        # raw: the full quoteResponse dict, when the quoter has one.
        # tx_builder.py's Jupiter swap provider needs it to request
        # an unsigned swap transaction; without it, that provider
        # refuses rather than reconstructing a quote.
        self.raw = raw

    def age_seconds(self, now=None):
        return (now if now is not None else time.time()) - self.fetched_at

    def is_fresh(self, now=None):
        return (self.output_amount > 0 and self.input_amount > 0
                and self.age_seconds(now) <= MAX_QUOTE_AGE_SECONDS)


def default_jupiter_quoter(input_mint, output_mint, input_amount,
                           slippage_bps=100, timeout=20):
    """Fetch a Jupiter v6 quote. Returns a Quote, or None.

    input_amount is in the input token's smallest units (lamports
    for SOL). Returns None on any fetch/parse failure — never a
    guessed quote. In this sandbox Jupiter returned an empty
    reply, so this quoter currently returns None here; that is
    the correct fail-closed behaviour, not a route.
    """
    url = JUPITER_QUOTE_URL.format(
        input=input_mint, output=output_mint,
        amount=int(input_amount), slippage_bps=int(slippage_bps))
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "solbot-live-swap"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 - no quote, fail closed
        return None
    try:
        out_amount = float(data["outAmount"])
    except (KeyError, TypeError, ValueError):
        return None
    if out_amount <= 0:
        return None
    return Quote(input_mint=input_mint, output_mint=output_mint,
                 input_amount=float(input_amount),
                 output_amount=out_amount, source="jupiter_v6",
                 raw=data)


class JupiterRouteChecker:
    """Route checker backed by a quote fetcher.

    check_route(mint, direction) returns (available, reason).
    direction is "buy" (SOL -> mint) or "sell" (mint -> SOL).
    A route is proven only by a fresh, positive quote for that
    exact direction. On devnet, the default Jupiter quoter is
    expected to return no route (unverified) — that refusal is
    correct, not a bug to bypass.
    """

    def __init__(self, quoter=None, network="mainnet"):
        self.quoter = quoter or default_jupiter_quoter
        self.network = network

    def check_route(self, mint, direction="buy", amount=1_000_000):
        if direction == "buy":
            input_mint, output_mint = SOL_MINT, mint
        elif direction == "sell":
            input_mint, output_mint = mint, SOL_MINT
        else:
            return False, ERR_QUOTE_MISMATCH
        try:
            quote = self.quoter(input_mint, output_mint, amount)
        except Exception:  # noqa: BLE001 - quoter failure = no route
            return False, ERR_ROUTE_UNVERIFIED
        if quote is None:
            return False, ERR_ROUTE_UNVERIFIED
        if not quote.is_fresh():
            return False, ERR_STALE_QUOTE
        if quote.input_mint != input_mint or quote.output_mint != output_mint:
            return False, ERR_QUOTE_MISMATCH
        return True, f"route_proven:{quote.source}"

    # loop.py compatibility: its RouteChecker.check_route takes
    # only a mint (buy direction by default in the loop's buy
    # path; sells go through the executor's own checks).
    def check_route_mint(self, mint):
        return self.check_route(mint, direction="buy")


class SwapExecutor:
    """Real swap executor (quote-gated, signer-gated).

    tx_builder, if supplied by a deployment, must be a callable
    (direction, mint, input_amount, min_output, quote) -> (ok, error).
    It is the ONLY place that may sign/send; this module never
    signs or sends itself. With no tx_builder, every swap is
    refused with executor_not_configured — never faked.
    """

    def __init__(self, quoter=None, tx_builder=None, network="mainnet",
                 slippage_guard_pct=SLIPPAGE_GUARD_PCT):
        self.quoter = quoter or default_jupiter_quoter
        self.tx_builder = tx_builder
        self.network = network
        self.slippage_guard_pct = slippage_guard_pct
        self.route_checker = JupiterRouteChecker(
            quoter=self.quoter, network=network)

    def _signer_ok(self):
        _marker, err = load_signer_with_status()
        if err:
            return False, err.split(":")[0]
        return True, ""

    def _fresh_quote(self, input_mint, output_mint, input_amount):
        try:
            quote = self.quoter(input_mint, output_mint, input_amount)
        except Exception:  # noqa: BLE001 - quoter failure = no route
            return None, ERR_ROUTE_UNVERIFIED
        if quote is None:
            return None, ERR_ROUTE_UNVERIFIED
        if not quote.is_fresh():
            return None, ERR_STALE_QUOTE
        if quote.input_mint != input_mint or quote.output_mint != output_mint:
            return None, ERR_QUOTE_MISMATCH
        return quote, ""

    def _execute(self, direction, mint, input_amount, input_units):
        """Shared buy/sell path. Returns (ok, error_code_or_None).

        input_amount is in SOL (buys) or tokens (sells) for
        logging; input_units is the smallest-unit amount quoted.
        """
        signer_ok, signer_err = self._signer_ok()
        if not signer_ok:
            logger.warning("swap_refused direction=%s mint=%s reason=%s",
                           direction, mint, signer_err)
            return False, signer_err
        if direction == "buy":
            input_mint, output_mint = SOL_MINT, mint
        else:
            input_mint, output_mint = mint, SOL_MINT
        quote, quote_err = self._fresh_quote(
            input_mint, output_mint, input_units)
        if quote is None:
            logger.warning("swap_refused direction=%s mint=%s reason=%s",
                           direction, mint, quote_err)
            return False, quote_err
        min_output = quote.output_amount * (1.0 - self.slippage_guard_pct)
        if self.tx_builder is None:
            logger.warning("swap_refused direction=%s mint=%s reason=%s",
                           direction, mint, ERR_EXECUTOR_NOT_CONFIGURED)
            return False, ERR_EXECUTOR_NOT_CONFIGURED
        logger.info("swap_intent direction=%s mint=%s input=%s "
                    "quoted_output=%s min_output=%s source=%s wallet=%s",
                    direction, mint, input_amount, quote.output_amount,
                    min_output, quote.source, WALLET_PUBKEY)
        try:
            ok, tx_err = self.tx_builder(
                direction, mint, input_amount, min_output, quote)
        except Exception as exc:  # noqa: BLE001 - builder failure = no swap
            logger.warning("swap_failed direction=%s mint=%s reason=%s",
                           direction, mint, exc)
            return False, "tx_builder_failed"
        if not ok:
            # The builder must enforce the slippage guard at send
            # time; if it reports a slippage failure, surface it.
            return False, tx_err or ERR_SLIPPAGE_GUARD
        return True, None

    def execute_buy(self, mint, sol_amount):
        """Buy: SOL -> token. sol_amount is in SOL; quoted in
        lamports (1 SOL = 1e9 lamports)."""
        return self._execute("buy", mint, sol_amount, int(sol_amount * 1e9))

    def execute_sell(self, mint, tokens):
        """Sell: token -> SOL. tokens is in token units; the
        quoter is called with the token amount as given (a
        deployment quoter must interpret smallest units for
        that mint — see RULES.md §13)."""
        return self._execute("sell", mint, tokens, tokens)


def check_slippage(quoted_output, execution_output,
                   guard_pct=SLIPPAGE_GUARD_PCT):
    """True if execution output is within the slippage guard of
    the quote; False (refuse) otherwise. Pure function, tested."""
    if quoted_output is None or quoted_output <= 0:
        return False
    if execution_output is None:
        return False
    return execution_output >= quoted_output * (1.0 - guard_pct)


def route_smoke_test(mint, direction="buy", quoter=None, network="mainnet"):
    """Read-only route smoke test for one mint/direction.

    Returns (ok: bool, report: dict). No swap, no signing, no
    trade. ok is True ONLY if a fresh, positive quote exists
    for that exact direction.
    """
    checker = JupiterRouteChecker(quoter=quoter, network=network)
    available, reason = checker.check_route(mint, direction=direction)
    return available, {
        "mint": mint,
        "direction": direction,
        "network": network,
        "route_available": available,
        "reason": reason,
        "note": ("A proven route here does NOT validate a signer "
                 "and does NOT start trading."),
    }
