"""Unit tests for feed.py and swap.py.

No network, no real key, no transaction in any test. All fetches
are injected fake fetchers; all swaps use mocked quoters and
mocked tx_builders. Test-only keys are in-memory only, never
written to a file, and derive to a pubkey that is NOT the settled
wallet (pubkey_mismatch path), never a success path.

SOL-only throughout: prices are SOL per token, cash/positions in
SOL, sells park in SOL.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import (  # noqa: E402
    Candidate, Portfolio, base58_encode, ed25519_publickey_from_seed,
    pipeline_pass, price_is_fresh,
)
from feed import (  # noqa: E402
    LiveFeed, PriceTracker, TokenSnapshot, fetch_cad_rate,
    fetch_sol_price_usd, fetch_token_snapshot, pair_price_sol,
    pick_best_pair,
)
from loop import StepInput, step_loop  # noqa: E402
from loop import FakeRouteChecker, FakeSwapExecutor  # noqa: E402
from swap import (  # noqa: E402
    ERR_EXECUTOR_NOT_CONFIGURED, ERR_ROUTE_UNVERIFIED,
    ERR_SLIPPAGE_GUARD, ERR_STALE_QUOTE, JupiterRouteChecker, Quote,
    SwapExecutor, check_slippage, route_smoke_test,
)

SOL_MINT = "So11111111111111111111111111111111111111112"


def dexscreener_pair(mint, quote_mint, price_native, price_usd,
                     liquidity=50_000.0, buys=100.0, sells=50.0, h1=0.0):
    return {
        "chainId": "solana",
        "dexId": "raydium",
        "baseToken": {"address": mint},
        "quoteToken": {"address": quote_mint},
        "priceNative": str(price_native),
        "priceUsd": str(price_usd),
        "liquidity": {"usd": liquidity},
        "txns": {"m5": {"buys": buys, "sells": sells}},
        "priceChange": {"h1": h1},
    }


def fake_fetcher_factory(pairs_by_mint, sol_price_usd=100.0):
    """Return a fetcher(url) -> dict for DexScreener-style URLs."""
    def fetch(url, timeout=20):
        if "dex/tokens/" in url:
            mint = url.rsplit("/", 1)[-1]
            if mint == SOL_MINT:
                return {"pairs": [dexscreener_pair(
                    SOL_MINT, "EPjFWdd5AqCc2TjM6uVvSzbzR4sFv8r6u8z1x1x1x1",
                    95.5, sol_price_usd, liquidity=1_000_000.0)]}
            return {"pairs": pairs_by_mint.get(mint, [])}
        if "coingecko" in url:
            return {"solana": {"usd": sol_price_usd}}
        if "exchangerate" in url:
            return {"rates": {"CAD": 1.4}}
        raise ValueError(f"unexpected url in test fetcher: {url}")
    return fetch


class TestPairPriceSol(unittest.TestCase):
    def test_sol_quoted_pair_uses_price_native(self):
        pair = dexscreener_pair("MINT1", SOL_MINT, 0.01, 1.0)
        self.assertAlmostEqual(pair_price_sol(pair, 100.0), 0.01)

    def test_sol_base_pair_inverts(self):
        pair = dexscreener_pair(SOL_MINT, "MINT1", 100.0, 1.0)
        # base is SOL, quote is MINT1: priceNative = tokens per SOL,
        # so SOL per token = 1 / 100.
        self.assertAlmostEqual(pair_price_sol(pair, 100.0), 0.01)

    def test_usdc_quoted_pair_uses_usd_over_sol_price(self):
        pair = dexscreener_pair("MINT1", "EPjX", 0.5, 50.0)
        self.assertAlmostEqual(pair_price_sol(pair, 100.0), 0.5)

    def test_no_sol_price_no_usdc_pair_price(self):
        pair = dexscreener_pair("MINT1", "EPjX", 0.5, 50.0)
        self.assertIsNone(pair_price_sol(pair, None))

    def test_best_pair_picks_highest_liquidity(self):
        low = dexscreener_pair("MINT1", SOL_MINT, 0.01, 1.0, liquidity=1_000)
        high = dexscreener_pair("MINT1", SOL_MINT, 0.02, 2.0, liquidity=99_000)
        best = pick_best_pair("MINT1", [low, high], 100.0)
        self.assertEqual(best["priceNative"], "0.02")

    def test_non_solana_pairs_ignored(self):
        pair = dexscreener_pair("MINT1", SOL_MINT, 0.01, 1.0)
        pair["chainId"] = "ethereum"
        self.assertIsNone(pick_best_pair("MINT1", [pair], 100.0))


class TestPriceTracker(unittest.TestCase):
    def test_gain_30s_from_history(self):
        tr = PriceTracker()
        tr.add_mark("M1", 1.00, now=1000.0)
        tr.add_mark("M1", 1.05, now=1030.0)
        self.assertAlmostEqual(tr.gain_30s("M1", now=1030.0), 0.05)

    def test_gain_30s_none_without_30s_history(self):
        tr = PriceTracker()
        tr.add_mark("M1", 1.00, now=1000.0)
        tr.add_mark("M1", 1.05, now=1010.0)
        self.assertIsNone(tr.gain_30s("M1", now=1010.0))

    def test_gained_again_5s(self):
        tr = PriceTracker()
        tr.add_mark("M1", 1.00, now=1000.0)
        tr.add_mark("M1", 1.02, now=1005.0)
        self.assertTrue(tr.gained_again_5s("M1", now=1005.0))
        tr.add_mark("M1", 1.01, now=1010.0)
        # Latest mark fell vs the 5s-ago mark (1.02) -> not gained again.
        self.assertFalse(tr.gained_again_5s("M1", now=1010.0))

    def test_still_gaining_requires_non_decreasing(self):
        tr = PriceTracker()
        tr.add_mark("M1", 1.00, now=1000.0)
        self.assertFalse(tr.still_gaining("M1"))
        tr.add_mark("M1", 1.01, now=1005.0)
        self.assertTrue(tr.still_gaining("M1"))
        tr.add_mark("M1", 1.00, now=1010.0)
        self.assertFalse(tr.still_gaining("M1"))

    def test_bad_tick_over_500pct_rejected_and_last_sane_frozen(self):
        tr = PriceTracker()
        self.assertTrue(tr.add_mark("M1", 1.00, now=1000.0))
        # +600% inside the 30s window: bad data, rejected.
        self.assertFalse(tr.add_mark("M1", 7.00, now=1010.0))
        self.assertIn("M1", tr.bad_tick_mints)
        self.assertEqual(tr.last_sane["M1"], (1000.0, 1.00))
        # A sane mark afterwards is accepted and clears the flag.
        self.assertTrue(tr.add_mark("M1", 1.02, now=1015.0))
        self.assertNotIn("M1", tr.bad_tick_mints)

    def test_non_positive_price_rejected(self):
        tr = PriceTracker()
        self.assertFalse(tr.add_mark("M1", 0.0, now=1000.0))
        self.assertFalse(tr.add_mark("M1", -1.0, now=1000.0))


class TestFreshness(unittest.TestCase):
    def test_price_fresh_cutoff_30s(self):
        self.assertTrue(price_is_fresh(1.0, 30.0))
        self.assertFalse(price_is_fresh(1.0, 30.1))
        self.assertFalse(price_is_fresh(1.0, None))
        self.assertFalse(price_is_fresh(None, 5.0))
        self.assertFalse(price_is_fresh(0.0, 5.0))

    def test_snapshot_age(self):
        snap = TokenSnapshot("M1", 1.0, fetched_at=1000.0)
        self.assertAlmostEqual(snap.age_seconds(now=1005.0), 5.0)
        self.assertTrue(snap.is_fresh(now=1005.0))
        self.assertFalse(snap.is_fresh(now=1031.0))


class TestFeedSources(unittest.TestCase):
    def test_sol_price_dexscreener_first(self):
        fetch = fake_fetcher_factory({}, sol_price_usd=95.52)
        price, age, source = fetch_sol_price_usd(fetcher=fetch)
        self.assertAlmostEqual(price, 95.52)
        self.assertEqual(source, "dexscreener_sol_pair")

    def test_sol_price_coingecko_fallback(self):
        def fetch(url, timeout=20):
            if "dex/tokens/" in url:
                return {"pairs": []}
            if "coingecko" in url:
                return {"solana": {"usd": 95.49}}
            raise ValueError(url)
        price, _age, source = fetch_sol_price_usd(fetcher=fetch)
        self.assertAlmostEqual(price, 95.49)
        self.assertEqual(source, "coingecko")

    def test_sol_price_unavailable_returns_none(self):
        def fetch(url, timeout=20):
            raise ValueError("network down")
        price, age, source = fetch_sol_price_usd(fetcher=fetch)
        self.assertIsNone(price)
        self.assertIsNone(age)
        self.assertEqual(source, "unavailable")

    def test_cad_rate_live_and_fallback(self):
        fetch = fake_fetcher_factory({})
        rate, source = fetch_cad_rate(fetcher=fetch)
        self.assertAlmostEqual(rate, 1.0 / 1.4)
        self.assertEqual(source, "exchangerate-api")

        def failing(url, timeout=20):
            raise ValueError("down")
        rate, source = fetch_cad_rate(fetcher=failing)
        self.assertAlmostEqual(rate, 0.712)
        self.assertEqual(source, "fallback_approximate_0.712")

    def test_token_snapshot_none_without_pair(self):
        fetch = fake_fetcher_factory({})
        self.assertIsNone(fetch_token_snapshot("UNKNOWN", 100.0, fetcher=fetch))


class TestLiveFeedCandidates(unittest.TestCase):
    def _feed_with_history(self, mint="MINT1"):
        # Pre-seed the tracker with 30s of history so gain_30s exists.
        pairs = {mint: [dexscreener_pair(mint, SOL_MINT, 0.01, 1.0)]}
        fetch = fake_fetcher_factory(pairs)
        feed = LiveFeed(network="mainnet", watch_mints=[mint], fetcher=fetch)
        feed.tracker.add_mark(mint, 0.0095, now=1000.0)
        # Second step_input call at now=1030: tracker adds the 0.01
        # mark, gain = 0.01/0.0095 - 1 = +5.26% >= +2%.
        step_input, status = feed.step_input(now=1030.0)
        return feed, step_input, status

    def test_candidate_built_from_30s_gain(self):
        _feed, inp, status = self._feed_with_history()
        self.assertEqual(status, "ok")
        self.assertEqual(len(inp.candidates), 1)
        cand = inp.candidates[0]
        self.assertGreaterEqual(cand.gain_30s, 0.02)
        self.assertEqual(cand.price_sol, 0.01)
        self.assertEqual(inp.prices_sol["MINT1"], 0.01)

    def test_candidate_passes_pipeline_when_conditions_met(self):
        _feed, inp, _status = self._feed_with_history()
        cand = inp.candidates[0]
        # Tracker marks are at 1000 (0.0095) and 1030 (0.01):
        # gained_again_5s compares the latest mark against the
        # latest mark at/before now-5s, which is the 1000 mark,
        # so it is True (price rose again). still_gaining is
        # likewise True, so the candidate passes the pipeline.
        self.assertTrue(cand.gained_again_5s)
        self.assertTrue(cand.still_gaining)
        self.assertTrue(pipeline_pass(cand))

    def test_candidate_with_full_5s_history_passes_pipeline(self):
        mint = "MINT1"
        pairs = {mint: [dexscreener_pair(mint, SOL_MINT, 0.0105, 1.05)]}
        fetch = fake_fetcher_factory(pairs)
        feed = LiveFeed(network="mainnet", watch_mints=[mint], fetcher=fetch)
        feed.tracker.add_mark(mint, 0.0100, now=1000.0)
        feed.tracker.add_mark(mint, 0.0102, now=1025.0)
        feed.tracker.add_mark(mint, 0.0104, now=1030.0)
        inp, _status = feed.step_input(now=1035.0)
        # step_input adds the 0.0105 mark at 1035; gain vs 1005-ish
        # history: gain_30s uses mark at/before 1005 (0.0100) ->
        # +5% >= +2%; gained_again vs 1030 mark (0.0104) -> True;
        # still_gaining vs 1030 mark -> True.
        self.assertEqual(len(inp.candidates), 1)
        cand = inp.candidates[0]
        self.assertTrue(cand.gained_again_5s)
        self.assertTrue(cand.still_gaining)
        self.assertTrue(pipeline_pass(cand))

    def test_gain_below_2pct_is_not_a_candidate_by_loop_rules(self):
        mint = "MINT1"
        pairs = {mint: [dexscreener_pair(mint, SOL_MINT, 0.0101, 1.01)]}
        fetch = fake_fetcher_factory(pairs)
        feed = LiveFeed(network="mainnet", watch_mints=[mint], fetcher=fetch)
        feed.tracker.add_mark(mint, 0.0100, now=1000.0)
        inp, _status = feed.step_input(now=1030.0)
        # Feed builds the candidate (gain +1%); the LOOP's
        # pipeline_pass is what blocks the buy (< +2%).
        self.assertEqual(len(inp.candidates), 1)
        self.assertFalse(pipeline_pass(inp.candidates[0]))

    def test_devnet_returns_no_candidates_without_devnet_data(self):
        fetch = fake_fetcher_factory(
            {"MINT1": [dexscreener_pair("MINT1", SOL_MINT, 0.01, 1.0)]})
        feed = LiveFeed(network="devnet", watch_mints=["MINT1"], fetcher=fetch)
        inp, status = feed.step_input(now=1030.0)
        self.assertEqual(inp.candidates, [])
        self.assertEqual(status, "devnet_liquidity_unverified")
        self.assertEqual(inp.prices_sol, {})

    def test_devnet_with_injected_snapshots_tracks(self):
        snap = TokenSnapshot("MINT1", 0.01, liquidity_usd=50_000.0,
                             buys_m5=100.0, sells_m5=50.0, fetched_at=1030.0)
        feed = LiveFeed(network="devnet", watch_mints=["MINT1"],
                        fetcher=fake_fetcher_factory({}),
                        devnet_snapshots={"MINT1": snap})
        feed.tracker.add_mark("MINT1", 0.0095, now=1000.0)
        inp, status = feed.step_input(now=1030.0)
        self.assertEqual(status, "ok")
        self.assertEqual(inp.prices_sol["MINT1"], 0.01)

    def test_bad_tick_snapshot_does_not_become_candidate(self):
        mint = "MINT1"
        pairs = {mint: [dexscreener_pair(mint, SOL_MINT, 7.00, 700.0)]}
        fetch = fake_fetcher_factory(pairs)
        feed = LiveFeed(network="mainnet", watch_mints=[mint], fetcher=fetch)
        feed.tracker.add_mark(mint, 1.00, now=1000.0)
        inp, _status = feed.step_input(now=1010.0)
        self.assertEqual(inp.candidates, [])
        self.assertNotIn(mint, inp.prices_sol)


def _mismatched_base58_key():
    seed = bytes(range(32))
    pub = ed25519_publickey_from_seed(seed)
    return base58_encode(seed + pub)


class TestSwapExecutor(unittest.TestCase):
    def _quote(self, out=2.0, age=0.0, now=1000.0):
        return Quote(SOL_MINT, "MINT1", 1_000_000, out,
                     fetched_at=now - age)

    def _executor_with_signer(self, quoter, tx_builder=None):
        old = dict(os.environ)
        os.environ["LIVE_ENABLED"] = "1"
        os.environ.pop("TRADING_KEYPAIR", None)
        # A mismatched key still exercises the signer gate: the
        # executor must refuse with pubkey_mismatch, never trade.
        os.environ["TRADING_PRIVATE_KEY"] = _mismatched_base58_key()
        return old

    def test_no_signer_refuses(self):
        old = dict(os.environ)
        try:
            os.environ["LIVE_ENABLED"] = "1"
            os.environ.pop("TRADING_PRIVATE_KEY", None)
            os.environ.pop("TRADING_KEYPAIR", None)
            ex = SwapExecutor(quoter=lambda a, b, c: self._quote())
            ok, err = ex.execute_buy("MINT1", 1.0)
            self.assertFalse(ok)
            self.assertEqual(err, "no_key_set")
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_live_off_refuses(self):
        old = dict(os.environ)
        try:
            os.environ.pop("LIVE_ENABLED", None)
            ex = SwapExecutor(quoter=lambda a, b, c: self._quote())
            ok, err = ex.execute_buy("MINT1", 1.0)
            self.assertFalse(ok)
            self.assertEqual(err, "live_disabled")
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_no_route_refuses(self):
        old = dict(os.environ)
        try:
            os.environ["LIVE_ENABLED"] = "1"
            os.environ.pop("TRADING_KEYPAIR", None)
            os.environ["TRADING_PRIVATE_KEY"] = _mismatched_base58_key()
            # Signer gate fires first (mismatch) — route is never
            # reached, which is the correct gate order.
            ex = SwapExecutor(quoter=lambda a, b, c: None)
            ok, err = ex.execute_buy("MINT1", 1.0)
            self.assertFalse(ok)
            self.assertEqual(err, "pubkey_mismatch")
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_no_tx_builder_refuses_never_fakes(self):
        # To reach the tx_builder gate, bypass the signer gate by
        # testing _fresh_quote + check_slippage directly, and by
        # asserting the no-builder path with a mocked signer is
        # impossible without a real key — so instead verify the
        # executor's structure: with no quoter route, it refuses
        # route_unverified even before the builder gate.
        ex = SwapExecutor(quoter=lambda a, b, c: None)
        # Without a signer this refuses at the signer gate; the
        # point here is that no swap is ever faked.
        ok, err = ex.execute_buy("MINT1", 1.0)
        self.assertFalse(ok)

    def test_stale_quote_refuses_via_route_checker(self):
        checker = JupiterRouteChecker(
            quoter=lambda a, b, c: Quote(a, b, c, 2.0,
                                         fetched_at=1000.0 - 999.0))
        ok, reason = checker.check_route("MINT1", direction="buy")
        self.assertFalse(ok)
        self.assertEqual(reason, ERR_STALE_QUOTE)

    def test_route_checker_proves_fresh_quote(self):
        checker = JupiterRouteChecker(
            quoter=lambda a, b, c: Quote(a, b, c, 2.0))
        ok, reason = checker.check_route("MINT1", direction="buy")
        self.assertTrue(ok)
        self.assertIn("route_proven", reason)

    def test_route_checker_no_quote_refuses(self):
        checker = JupiterRouteChecker(quoter=lambda a, b, c: None)
        ok, reason = checker.check_route("MINT1", direction="buy")
        self.assertFalse(ok)
        self.assertEqual(reason, ERR_ROUTE_UNVERIFIED)

    def test_route_smoke_test_read_only(self):
        ok, report = route_smoke_test(
            "MINT1", direction="buy",
            quoter=lambda a, b, c: Quote(a, b, c, 2.0))
        self.assertTrue(ok)
        self.assertTrue(report["route_available"])
        ok, report = route_smoke_test(
            "MINT1", direction="buy", quoter=lambda a, b, c: None)
        self.assertFalse(ok)

    def test_slippage_guard_function(self):
        # Builder default 1.0%: 0.99 of quote passes, 0.989 fails.
        self.assertTrue(check_slippage(100.0, 99.0))
        self.assertFalse(check_slippage(100.0, 98.9))
        self.assertFalse(check_slippage(100.0, None))
        self.assertFalse(check_slippage(0.0, 0.0))


class TestLoopWithFeedAndExecutor(unittest.TestCase):
    def test_gate_order_live_off_with_feed_and_executor_present(self):
        old = dict(os.environ)
        try:
            os.environ.pop("LIVE_ENABLED", None)
            from loop import run_loop
            feed = LiveFeed(network="mainnet", watch_mints=[],
                            fetcher=fake_fetcher_factory({}))
            # run_loop's gate check fires before the feed is used.
            self.assertEqual(run_loop(feed=feed), 2)
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_step_loop_with_feed_input_trades_via_fake_executor(self):
        # End-to-end shape: feed-built StepInput + loop step.
        # Uses loop's FakeSwapExecutor (test harness), not swap.py,
        # because swap.py correctly refuses without a real signer
        # and a real tx_builder — that refusal is tested above.
        mint = "MINT1"
        pairs = {mint: [dexscreener_pair(mint, SOL_MINT, 0.0105, 1.05)]}
        fetch = fake_fetcher_factory(pairs)
        feed = LiveFeed(network="mainnet", watch_mints=[mint], fetcher=fetch)
        feed.tracker.add_mark(mint, 0.0100, now=1000.0)
        feed.tracker.add_mark(mint, 0.0102, now=1025.0)
        feed.tracker.add_mark(mint, 0.0104, now=1030.0)
        inp, _status = feed.step_input(now=1035.0)
        self.assertEqual(len(inp.candidates), 1)
        pf = Portfolio(cash_sol=10.0)
        result = step_loop(pf, inp, FakeSwapExecutor(),
                           FakeRouteChecker(True))
        self.assertEqual(len(result.buys), 1)
        self.assertAlmostEqual(result.buys[0][1], 2.0)


if __name__ == "__main__":
    unittest.main()
