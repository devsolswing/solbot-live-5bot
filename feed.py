"""Live price feed for the SolBot LIVE 5-bot engine (SOL-only).

This module supplies what loop.py's step inputs need, from real
public sources only. It invents NO price, NO gain, and NO candidate.

Sources (verified live from this sandbox, 2026-11-01)
-----------------------------------------------------
* DexScreener ``/latest/dex/tokens/{mint}`` — returns Solana pairs
  for a mint, with priceNative, priceUsd, liquidity, txns (m5 buys /
  sells), and priceChange (m5/h1/h24). Verified: the wrapped-SOL
  mint returns an Orca SOL pair at $95.52 USD in this sandbox.
* CoinGecko ``/simple/price?ids=solana&vs_currencies=usd`` — SOL
  price in USD. Verified: $95.49 USD in this sandbox. Used only as
  a fallback SOL-price source when DexScreener's SOL pair is
  unavailable.
* exchangerate-api ``/v4/latest/USD`` — CAD rate, as in dry_run.py.
  Fallback 0.712 USD per CAD is the engine's approximate default,
  labelled as such in RULES.md §7; it is NOT a trading price.

What DexScreener does NOT give
------------------------------
DexScreener gives m5/h1/h24 changes, NOT a 30-second change. The
Screener's +2%-in-30s rule therefore cannot be read off a single
snapshot. This feed computes it itself: PriceTracker keeps a
timestamped history of sane marks per mint, and gain_30s is
(price_now / price_~30s_ago - 1) from that history. A mint with
less than 30 seconds of history is NOT a candidate — fail closed,
never an invented gain. Analyst confirmation (gained again within
5s) and Secondary (still gaining) are likewise computed from the
tracker's own marks, not from a snapshot field.

Bad-tick rejection (existing Screener rule, engine.py)
------------------------------------------------------
A single new mark that jumps more than +500% in 30 seconds
(SPIKE_MAX_GAIN_30S = 5.00) against the last sane mark is treated
as bad data: the mark is REJECTED, the last sane mark is frozen,
and the mint is flagged stale_bad_tick. No candidate, no buy, and
no sell is ever computed from a rejected mark. A non-positive
price is rejected the same way. This applies the Screener's
spike rule to valuation as well as to screening.

Freshness (engine.py price_is_fresh)
------------------------------------
A price is usable only if present, positive, and at most 30
seconds old. Anything older or missing is stale — no trade,
never an invented price. The feed reports age on every snapshot
so the loop can enforce this.

Devnet — read this before trading on devnet
-------------------------------------------
DexScreener pairs are MAINNET Solana pairs. They do NOT price
devnet tokens, devnet pools, or devnet trenches liquidity, and
they must NEVER be used to price a devnet swap: a mainnet price
for a mint is not a devnet pool price for that mint. On
NETWORK=devnet this feed therefore returns NO candidates and
reports ``devnet_liquidity_unverified`` unless a deployment
explicitly supplies devnet pair data through an injected fetcher
(a devnet test token with a real, verified devnet pool). That is
not a bug to work around — it is the fail-closed rule from
RULES.md §11. Devnet trenches liquidity was NOT verified in this
sandbox (Jupiter v6 devnet quote returned no route; DexScreener
showed no pairs for the devnet mint checked earlier), and this
feed does not pretend otherwise.

Scanning / discovery — honest limit
------------------------------------
There is no verified free public endpoint in this sandbox that
reliably returns "all brand-new Solana trenches coins sorted by
30-second gain". This feed therefore tracks a caller-supplied
watchlist of mints (plus the loop's open-position mints, which
are always tracked for sells). A deployment that wants
autonomous discovery must supply a discovery callable returning
mints; the default discovery returns NO mints, so with no
watchlist the feed produces NO candidates and trades nothing.
That is fail-closed, not a missing feature to fake.

No key, no signing, no sending in this module. All fetches are
read-only public HTTP. Fetcher functions are injectable so tests
never touch the network.
"""

from __future__ import annotations

import json
import time
import urllib.request

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import (  # noqa: E402
    Candidate, MAX_PRICE_AGE_SECONDS, SPIKE_MAX_GAIN_30S,
    crash_watch_active, price_is_fresh,
)

SOL_MINT = "So11111111111111111111111111111111111111112"
DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/{}"
COINGECKO_SOL_URL = ("https://api.coingecko.com/api/v3/simple/price"
                     "?ids=solana&vs_currencies=usd")
EXCHANGERATE_URL = "https://api.exchangerate-api.com/v4/latest/USD"
USD_PER_CAD_FALLBACK = 0.712

# Builder defaults (NOT James's numbers — see RULES.md §5). The
# spike rule below is James's engine rule (SPIKE_MAX_GAIN_30S);
# the history windows are feed mechanics, not trading rules.
GAIN_WINDOW_SECONDS = 30.0
CONFIRM_WINDOW_SECONDS = 5.0

ERR_STALE = "stale_or_missing_price"
ERR_BAD_TICK = "bad_tick_rejected"
ERR_DEVNET_UNVERIFIED = "devnet_liquidity_unverified"
ERR_NO_FEED_DATA = "no_feed_data"


def default_fetcher(url, timeout=20):
    """Read-only public JSON fetch (urllib, stdlib only)."""
    req = urllib.request.Request(url, headers={"User-Agent": "solbot-live-feed"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_sol_price_usd(fetcher=None):
    """Return (sol_price_usd_or_None, age_seconds_or_None, source).

    Tries DexScreener's SOL pair first, then CoinGecko. A fresh
    fetch has age 0 by construction (the caller timestamps it);
    a failed fetch returns (None, None, source) — never a guess.
    """
    fetch = fetcher or default_fetcher
    try:
        data = fetch(DEXSCREENER_TOKENS_URL.format(SOL_MINT))
        pairs = (data or {}).get("pairs") or []
        for pair in pairs:
            if pair.get("chainId") == "solana" and pair.get("priceUsd"):
                return float(pair["priceUsd"]), 0.0, "dexscreener_sol_pair"
    except Exception:  # noqa: BLE001 - fall through to CoinGecko
        pass
    try:
        data = fetch(COINGECKO_SOL_URL)
        price = (data or {}).get("solana", {}).get("usd")
        if price:
            return float(price), 0.0, "coingecko"
    except Exception:  # noqa: BLE001 - no SOL price, fail closed
        pass
    return None, None, "unavailable"


def fetch_cad_rate(fetcher=None):
    """Return (usd_per_cad, source). Fallback is the engine's
    approximate default, labelled as such — never presented as live."""
    fetch = fetcher or default_fetcher
    try:
        data = fetch(EXCHANGERATE_URL)
        cad_per_usd = float(data["rates"]["CAD"])
        if cad_per_usd > 0:
            return 1.0 / cad_per_usd, "exchangerate-api"
    except Exception:  # noqa: BLE001 - fallback, labelled approximate
        pass
    return USD_PER_CAD_FALLBACK, "fallback_approximate_0.712"


def pair_price_sol(pair, sol_price_usd):
    """SOL per token for one DexScreener pair, or None.

    Preferred: quote token is wrapped SOL -> priceNative IS SOL
    per token. If the pair is SOL/base (base is SOL), invert it.
    Otherwise fall back to priceUsd / sol_price_usd, and only if
    a SOL USD price is available. Never invent a conversion.
    """
    if not pair:
        return None
    base = (pair.get("baseToken") or {}).get("address")
    quote = (pair.get("quoteToken") or {}).get("address")
    try:
        price_native = float(pair.get("priceNative"))
    except (TypeError, ValueError):
        return None
    if price_native <= 0:
        return None
    if quote == SOL_MINT:
        return price_native
    if base == SOL_MINT:
        return 1.0 / price_native
    if sol_price_usd:
        try:
            price_usd = float(pair.get("priceUsd"))
        except (TypeError, ValueError):
            return None
        if price_usd > 0:
            return price_usd / float(sol_price_usd)
    return None


def pick_best_pair(mint, pairs, sol_price_usd):
    """Highest-liquidity Solana pair for mint that yields a SOL
    price, or None. Non-Solana pairs are ignored."""
    best = None
    best_liq = -1.0
    for pair in pairs or []:
        if pair.get("chainId") != "solana":
            continue
        base = (pair.get("baseToken") or {}).get("address")
        quote = (pair.get("quoteToken") or {}).get("address")
        if mint not in (base, quote):
            continue
        if pair_price_sol(pair, sol_price_usd) is None:
            continue
        try:
            liq = float((pair.get("liquidity") or {}).get("usd") or 0)
        except (TypeError, ValueError):
            liq = 0.0
        if liq > best_liq:
            best_liq = liq
            best = pair
    return best


class TokenSnapshot:
    """One fetched, sanity-checked mark for a mint."""

    def __init__(self, mint, price_sol, price_usd=None, liquidity_usd=0.0,
                 buys_m5=0.0, sells_m5=0.0, price_change_h1=None,
                 fetched_at=None, stale=False, bad_tick=False):
        self.mint = mint
        self.price_sol = price_sol
        self.price_usd = price_usd
        self.liquidity_usd = liquidity_usd
        self.buys_m5 = buys_m5
        self.sells_m5 = sells_m5
        self.price_change_h1 = price_change_h1
        self.fetched_at = fetched_at if fetched_at is not None else time.time()
        self.stale = stale
        self.bad_tick = bad_tick

    def age_seconds(self, now=None):
        return (now if now is not None else time.time()) - self.fetched_at

    def is_fresh(self, now=None):
        return price_is_fresh(self.price_sol, self.age_seconds(now))


def fetch_token_snapshot(mint, sol_price_usd=None, fetcher=None,
                         now=None):
    """Fetch one mint's best DexScreener pair and return a snapshot.

    Returns None if no usable pair exists (no candidate, no trade
    — never a guessed price). sol_price_usd, if supplied, is used
    only for non-SOL-quoted pairs (see pair_price_sol).
    """
    fetch = fetcher or default_fetcher
    try:
        data = fetch(DEXSCREENER_TOKENS_URL.format(mint))
    except Exception:  # noqa: BLE001 - no data, fail closed
        return None
    pair = pick_best_pair(mint, (data or {}).get("pairs"), sol_price_usd)
    if pair is None:
        return None
    price_sol = pair_price_sol(pair, sol_price_usd)
    if price_sol is None or price_sol <= 0:
        return None
    try:
        price_usd = float(pair.get("priceUsd")) if pair.get("priceUsd") else None
    except (TypeError, ValueError):
        price_usd = None
    try:
        liq = float((pair.get("liquidity") or {}).get("usd") or 0)
    except (TypeError, ValueError):
        liq = 0.0
    txns = (pair.get("txns") or {}).get("m5") or {}
    try:
        h1 = float((pair.get("priceChange") or {}).get("h1"))
    except (TypeError, ValueError):
        h1 = None
    return TokenSnapshot(
        mint=mint, price_sol=price_sol, price_usd=price_usd,
        liquidity_usd=liq, buys_m5=float(txns.get("buys") or 0),
        sells_m5=float(txns.get("sells") or 0), price_change_h1=h1,
        fetched_at=now if now is not None else time.time())


class PriceTracker:
    """Timestamped history of SANE marks per mint.

    Bad ticks never enter the history: a mark that jumps more
    than +500% in 30 seconds against the last sane mark is
    rejected, the last sane mark is frozen, and the mint is
    flagged (bad_tick=True on the rejected snapshot path).
    """

    def __init__(self):
        self.marks = {}  # mint -> [(timestamp, price_sol), ...]
        self.last_sane = {}  # mint -> (timestamp, price_sol)
        self.bad_tick_mints = set()

    def add_mark(self, mint, price_sol, now=None):
        """Add a mark. Returns True if accepted, False if rejected
        as a bad tick (or non-positive price)."""
        now = now if now is not None else time.time()
        if price_sol is None or float(price_sol) <= 0:
            return False
        price_sol = float(price_sol)
        last = self.last_sane.get(mint)
        if last is not None:
            last_time, last_price = last
            elapsed = now - last_time
            if elapsed <= GAIN_WINDOW_SECONDS and last_price > 0:
                jump = price_sol / last_price - 1.0
                if jump > SPIKE_MAX_GAIN_30S:
                    self.bad_tick_mints.add(mint)
                    return False
        self.marks.setdefault(mint, []).append((now, price_sol))
        # Keep only the last ~2 minutes of marks (enough for 30s/5s
        # windows plus slack); older marks cannot affect a gain.
        cutoff = now - 120.0
        self.marks[mint] = [m for m in self.marks[mint] if m[0] >= cutoff]
        self.last_sane[mint] = (now, price_sol)
        self.bad_tick_mints.discard(mint)
        return True

    def _price_at_or_before(self, mint, target_time):
        """Latest mark at or before target_time, or None."""
        chosen = None
        for ts, price in self.marks.get(mint, []):
            if ts <= target_time:
                chosen = (ts, price)
            else:
                break
        return chosen

    def gain_30s(self, mint, now=None):
        """(price_now / price_~30s_ago - 1), or None if fewer than
        30 seconds of history exist. Never invented."""
        now = now if now is not None else time.time()
        marks = self.marks.get(mint, [])
        if not marks:
            return None
        now_price = marks[-1][1]
        old = self._price_at_or_before(mint, now - GAIN_WINDOW_SECONDS)
        if old is None:
            return None
        old_price = old[1]
        if old_price <= 0:
            return None
        return now_price / old_price - 1.0

    def gained_again_5s(self, mint, now=None):
        """True only if the price gained again in the last 5s
        window (latest mark above the mark ~5s ago)."""
        now = now if now is not None else time.time()
        marks = self.marks.get(mint, [])
        if not marks:
            return False
        old = self._price_at_or_before(mint, now - CONFIRM_WINDOW_SECONDS)
        if old is None:
            return False
        return marks[-1][1] > old[1]

    def still_gaining(self, mint):
        """True if the latest mark is at or above the previous one
        (non-decreasing). Requires at least 2 marks."""
        marks = self.marks.get(mint, [])
        if len(marks) < 2:
            return False
        return marks[-1][1] >= marks[-2][1]


class LiveFeed:
    """Caller-supplied watchlist + open positions -> loop StepInput.

    network: "devnet" or "mainnet". On devnet, candidates are
    ALWAYS empty unless devnet_snapshots (injected, verified
    devnet pair data) are supplied — mainnet DexScreener data is
    never used to price a devnet trade.
    discovery: optional callable returning [mint, ...] to watch.
    Default discovery returns [] (no autonomous discovery claimed).
    """

    def __init__(self, network="mainnet", watch_mints=(),
                 fetcher=None, discovery=None, devnet_snapshots=None):
        self.network = network
        self.watch_mints = list(watch_mints)
        self.fetcher = fetcher or default_fetcher
        self.discovery = discovery or (lambda: [])
        self.devnet_snapshots = devnet_snapshots  # mint -> TokenSnapshot
        self.tracker = PriceTracker()
        self.last_status = ""

    def step_input(self, portfolio_positions=(), now=None):
        """Build a loop StepInput for this step (read-only fetch).

        Returns (StepInput, status_string). On devnet without
        injected devnet snapshots, candidates are empty and the
        status reports devnet_liquidity_unverified — prices for
        open positions are still fetched for sells ONLY if the
        caller supplies devnet snapshots; otherwise sells are
        blocked by the loop's freshness gate, as they must be.
        """
        from loop import StepInput  # late import: loop imports feed lazily

        now = now if now is not None else time.time()
        sol_price_usd, _sol_age, sol_source = fetch_sol_price_usd(
            fetcher=self.fetcher)
        usd_per_cad, _cad_source = fetch_cad_rate(fetcher=self.fetcher)
        mints = list(dict.fromkeys(
            list(self.watch_mints) + list(self.discovery())
            + list(portfolio_positions)))
        prices_sol = {}
        price_age = {}
        snapshots = {}
        crash_down = 0
        crash_tracked = 0
        if self.network == "devnet" and not self.devnet_snapshots:
            self.last_status = ERR_DEVNET_UNVERIFIED
            # Still report SOL price / CAD for halt arithmetic, but
            # NO token prices and NO candidates on devnet without
            # verified devnet pair data.
            return (StepInput(
                candidates=[], prices_sol={}, price_age_seconds={},
                sol_price_usd=sol_price_usd, sol_price_age_seconds=0.0
                if sol_price_usd else None, market_fast_by_mint={},
                crash_watch_active=False, usd_per_cad=usd_per_cad),
                self.last_status)
        for mint in mints:
            snap = None
            if self.network == "devnet" and self.devnet_snapshots:
                snap = self.devnet_snapshots.get(mint)
            else:
                snap = fetch_token_snapshot(
                    mint, sol_price_usd=sol_price_usd,
                    fetcher=self.fetcher, now=now)
            if snap is None:
                continue
            accepted = self.tracker.add_mark(mint, snap.price_sol, now=now)
            if not accepted:
                snap.bad_tick = True
                continue
            snapshots[mint] = snap
            prices_sol[mint] = snap.price_sol
            price_age[mint] = 0.0
            if snap.price_change_h1 is not None:
                crash_tracked += 1
                if snap.price_change_h1 <= -5.0:
                    crash_down += 1
        candidates = []
        market_fast = {}
        for mint, snap in snapshots.items():
            if mint in portfolio_positions:
                continue
            gain = self.tracker.gain_30s(mint, now=now)
            if gain is None:
                continue
            # Spike rule is enforced again here via pipeline_pass
            # in the loop; the tracker already rejected >+500%
            # single-window jumps.
            cand = Candidate(
                mint=mint, gain_30s=gain,
                gained_again_5s=self.tracker.gained_again_5s(mint, now=now),
                still_gaining=self.tracker.still_gaining(mint),
                liquidity_usd=snap.liquidity_usd,
                buys=snap.buys_m5, sells=snap.sells_m5,
                price_sol=snap.price_sol, price_age_seconds=0.0)
            candidates.append(cand)
            # Market speed: positive 30s gain with continued gains
            # is treated as fast; a flat/fading gain is slow. If
            # the tracker cannot show a gain, speed is unknown and
            # the loop holds slow-TP/peak-drop sells (loop rule).
            market_fast[mint] = bool(gain > 0 and cand.still_gaining)
        self.last_status = "ok" if snapshots else ERR_NO_FEED_DATA
        return (StepInput(
            candidates=candidates, prices_sol=prices_sol,
            price_age_seconds=price_age, sol_price_usd=sol_price_usd,
            sol_price_age_seconds=0.0 if sol_price_usd else None,
            market_fast_by_mint=market_fast,
            crash_watch_active=crash_watch_active(crash_down, crash_tracked),
            usd_per_cad=usd_per_cad), self.last_status)

    def feed_callable(self, portfolio_positions_getter):
        """Return a loop feed callable: () -> StepInput.

        portfolio_positions_getter is a callable returning the
        current open-position mint list (so sells stay priced even
        when a coin leaves the watchlist).
        """
        def _feed():
            step_input, _status = self.step_input(
                portfolio_positions=portfolio_positions_getter())
            return step_input
        return _feed


def watch_mints_from_env():
    """WATCH_MINTS (comma-separated env var) -> [mint, ...].

    Empty / unset = [] (no candidates, trades nothing — the
    fail-closed default). Whitespace-only entries are dropped.
    This is the single place loop.py reads the watchlist from,
    so the env contract lives here, not scattered in callers.
    """
    return [m.strip() for m in
            os.environ.get("WATCH_MINTS", "").split(",") if m.strip()]


def feed_smoke_test(network="mainnet", fetcher=None):
    """Read-only feed check: SOL price, CAD rate, and (mainnet
    only) a SOL-pair fetch. No swap, no signing, no trade.
    Returns (ok: bool, report: dict)."""
    sol_price, _age, sol_source = fetch_sol_price_usd(fetcher=fetcher)
    usd_per_cad, cad_source = fetch_cad_rate(fetcher=fetcher)
    report = {
        "network": network,
        "sol_price_usd": sol_price,
        "sol_price_source": sol_source,
        "usd_per_cad": usd_per_cad,
        "cad_source": cad_source,
        "devnet_note": None,
    }
    if network == "devnet":
        report["devnet_note"] = (
            "Devnet token prices are NOT supplied by this feed: "
            "DexScreener pairs are mainnet pairs. A devnet smoke "
            "test must supply verified devnet pair data, or it "
            "must fail closed with devnet_liquidity_unverified.")
        return (sol_price is not None, report)
    return (sol_price is not None, report)
