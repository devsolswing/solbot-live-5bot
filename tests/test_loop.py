"""Unit tests for loop.py (the trading loop step engine).

No real key, no network, no transaction in any test. Test-only keys
are generated in-memory from a deterministic seed, never written to
a file, never used for mainnet, and never represent a real wallet.
The in-memory key derives to a pubkey that is NOT the settled wallet,
so it exercises the pubkey_mismatch refusal, not a success path.

SOL-only: cash, positions, buys, sells, and parking are all in SOL.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import (  # noqa: E402
    Candidate, Portfolio, Position, WALLET_PUBKEY, base58_encode,
    ed25519_publickey_from_seed, load_signer_with_status,
)
from loop import (  # noqa: E402
    ERR_HALT, ERR_INVALID_NETWORK, ERR_LIVE_DISABLED,
    ERR_NO_LIVE_FEED, ERR_ROUTE_UNVERIFIED, ERR_SOL_PRICE_STALE,
    FakeRouteChecker, FakeSwapExecutor, RefusingSwapExecutor,
    StepInput, UnverifiedRouteChecker, check_gates, run_loop,
    smoke_test, step_loop,
)


def fresh_candidate(mint="MINT1", price_sol=0.01, **kw):
    base = dict(mint=mint, gain_30s=0.05, gained_again_5s=True,
                still_gaining=True, liquidity_usd=50_000.0,
                buys=100.0, sells=50.0, price_sol=price_sol,
                price_age_seconds=5.0)
    base.update(kw)
    return Candidate(**base)


def step_input(candidates=(), prices=None, sol_price=100.0, sol_age=5.0,
               market_fast=None, crash_watch=False):
    # Tests pass prices as {mint: price_sol}; ages default to 5s
    # (fresh) for every priced mint, matching engine's scalar API.
    prices = prices or {}
    return StepInput(
        candidates=list(candidates),
        prices_sol=prices,
        price_age_seconds={m: 5.0 for m in prices},
        sol_price_usd=sol_price, sol_price_age_seconds=sol_age,
        market_fast_by_mint=market_fast or {},
        crash_watch_active=crash_watch)


def _mismatched_base58_key():
    """In-memory test-only key whose pubkey != WALLET_PUBKEY."""
    seed = bytes(range(32))
    pub = ed25519_publickey_from_seed(seed)
    return base58_encode(seed + pub)


class TestGateOrder(unittest.TestCase):
    def test_live_off_refuses_first(self):
        ok, err = check_gates(False, "bogus-network", "both_keys_set")
        self.assertFalse(ok)
        self.assertEqual(err, ERR_LIVE_DISABLED)

    def test_invalid_network_refuses_before_signer(self):
        ok, err = check_gates(True, "bogus-network", "both_keys_set")
        self.assertFalse(ok)
        self.assertEqual(err, ERR_INVALID_NETWORK)

    def test_signer_error_after_live_and_network(self):
        ok, err = check_gates(True, "devnet", "both_keys_set")
        self.assertFalse(ok)
        self.assertEqual(err, "both_keys_set")
        ok, err = check_gates(True, "mainnet", None)
        self.assertTrue(ok)
        self.assertEqual(err, "")

    def test_real_signer_no_key_refuses(self):
        old = dict(os.environ)
        try:
            os.environ["LIVE_ENABLED"] = "1"
            os.environ.pop("TRADING_PRIVATE_KEY", None)
            os.environ.pop("TRADING_KEYPAIR", None)
            _m, err = load_signer_with_status()
            self.assertEqual(err, "no_key_set")
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_real_signer_both_keys_refuse(self):
        old = dict(os.environ)
        try:
            os.environ["LIVE_ENABLED"] = "1"
            os.environ["TRADING_PRIVATE_KEY"] = "dummy"
            os.environ["TRADING_KEYPAIR"] = "[1,2,3]"
            _m, err = load_signer_with_status()
            self.assertTrue(err.startswith("both_keys_set"))
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_real_signer_pubkey_mismatch_refuses(self):
        old = dict(os.environ)
        try:
            os.environ["LIVE_ENABLED"] = "1"
            os.environ.pop("TRADING_KEYPAIR", None)
            os.environ["TRADING_PRIVATE_KEY"] = _mismatched_base58_key()
            _m, err = load_signer_with_status()
            self.assertEqual(err, "pubkey_mismatch")
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_run_loop_live_off_refuses(self):
        old = dict(os.environ)
        try:
            os.environ.pop("LIVE_ENABLED", None)
            self.assertEqual(run_loop(feed=lambda: step_input()), 2)
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_run_loop_no_feed_refuses(self):
        old = dict(os.environ)
        try:
            os.environ["LIVE_ENABLED"] = "1"
            os.environ["NETWORK"] = "devnet"
            os.environ.pop("TRADING_PRIVATE_KEY", None)
            os.environ.pop("TRADING_KEYPAIR", None)
            # Signer gate fires before the feed gate in run_loop.
            self.assertEqual(run_loop(feed=lambda: step_input()), 2)
        finally:
            os.environ.clear()
            os.environ.update(old)


class TestStepGates(unittest.TestCase):
    def test_halt_blocks_buys_and_sells_all(self):
        pf = Portfolio(cash_sol=0.05)
        pf.positions["MINT1"] = Position(
            mint="MINT1", entry_price_sol=0.01, invested_sol=0.02,
            tokens=2.0, peak_price_sol=0.01)
        inp = step_input(
            candidates=[fresh_candidate()],
            prices={"MINT1": 0.01}, sol_price=100.0, sol_age=5.0)
        # 0.05 cash + 0.02 position = 0.07 SOL * $100 / 0.712 = $9.83 CAD
        # -> below $20 halt.
        result = step_loop(pf, inp, FakeSwapExecutor(),
                           FakeRouteChecker(True))
        self.assertTrue(result.halted)
        self.assertEqual(result.error, ERR_HALT)
        self.assertEqual(result.buys, [])
        self.assertEqual(pf.positions, {})

    def test_stale_sol_price_blocks_trading(self):
        pf = Portfolio(cash_sol=10.0)
        inp = step_input(candidates=[fresh_candidate()],
                         sol_price=100.0, sol_age=999.0)
        result = step_loop(pf, inp, FakeSwapExecutor(),
                           FakeRouteChecker(True))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, ERR_SOL_PRICE_STALE)
        self.assertEqual(result.buys, [])

    def test_missing_sol_price_blocks_trading(self):
        pf = Portfolio(cash_sol=10.0)
        inp = step_input(candidates=[fresh_candidate()],
                         sol_price=None, sol_age=None)
        result = step_loop(pf, inp, FakeSwapExecutor(),
                           FakeRouteChecker(True))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, ERR_SOL_PRICE_STALE)

    def test_stale_candidate_price_blocks_buy(self):
        pf = Portfolio(cash_sol=10.0)
        cand = fresh_candidate(price_age_seconds=999.0)
        inp = step_input(candidates=[cand],
                         prices={"MINT1": 0.01})
        result = step_loop(pf, inp, FakeSwapExecutor(),
                           FakeRouteChecker(True))
        self.assertEqual(result.buys, [])
        self.assertEqual(pf.cash_sol, 10.0)

    def test_no_route_blocks_buy_and_leaves_ledger_unchanged(self):
        pf = Portfolio(cash_sol=10.0)
        inp = step_input(candidates=[fresh_candidate()])
        result = step_loop(pf, inp, FakeSwapExecutor(),
                           FakeRouteChecker(False))
        self.assertEqual(result.buys, [])
        self.assertEqual(pf.cash_sol, 10.0)
        self.assertEqual(pf.positions, {})
        self.assertTrue(any(ERR_ROUTE_UNVERIFIED in b
                            for b in result.blocked))

    def test_refusing_executor_blocks_buy(self):
        pf = Portfolio(cash_sol=10.0)
        inp = step_input(candidates=[fresh_candidate()])
        result = step_loop(pf, inp, RefusingSwapExecutor(),
                           FakeRouteChecker(True))
        self.assertEqual(result.buys, [])
        self.assertEqual(pf.positions, {})


class TestSizingAndCaps(unittest.TestCase):
    def test_buy_is_exactly_20_percent_of_bankroll(self):
        pf = Portfolio(cash_sol=10.0)
        inp = step_input(candidates=[fresh_candidate()])
        result = step_loop(pf, inp, FakeSwapExecutor(),
                           FakeRouteChecker(True))
        self.assertEqual(len(result.buys), 1)
        self.assertAlmostEqual(result.buys[0][1], 2.0)
        self.assertAlmostEqual(pf.cash_sol, 8.0)

    def test_max_5_open_positions(self):
        pf = Portfolio(cash_sol=100.0)
        cands = [fresh_candidate(mint=f"M{i}") for i in range(7)]
        inp = step_input(candidates=cands)
        result = step_loop(pf, inp, FakeSwapExecutor(),
                           FakeRouteChecker(True))
        self.assertEqual(len(result.buys), 5)
        self.assertEqual(len(pf.positions), 5)

    def test_one_position_per_coin(self):
        pf = Portfolio(cash_sol=100.0)
        pf.positions["MINT1"] = Position(
            mint="MINT1", entry_price_sol=0.01, invested_sol=2.0,
            tokens=200.0, peak_price_sol=0.01)
        inp = step_input(candidates=[fresh_candidate()])
        result = step_loop(pf, inp, FakeSwapExecutor(),
                           FakeRouteChecker(True))
        self.assertEqual(result.buys, [])


class TestSellBoundariesSol(unittest.TestCase):
    def _position(self, entry=1.0, invested=2.0, peak=None):
        return Position(mint="MINT1", entry_price_sol=entry,
                        invested_sol=invested, tokens=invested / entry,
                        peak_price_sol=peak if peak is not None else entry)

    def test_hard_stop_minus_7_percent_sells(self):
        pf = Portfolio(cash_sol=10.0)
        pf.positions["MINT1"] = self._position()
        inp = step_input(prices={"MINT1": 0.93},
                         market_fast={"MINT1": False})
        result = step_loop(pf, inp, FakeSwapExecutor(),
                           FakeRouteChecker(True))
        self.assertEqual(len(result.sells), 1)
        self.assertEqual(result.sells[0][1], "hard_stop_-7%")
        self.assertAlmostEqual(result.sells[0][2], 2.0 * 0.93)

    def test_slow_tp_plus_10_percent_sells(self):
        pf = Portfolio(cash_sol=10.0)
        pf.positions["MINT1"] = self._position()
        inp = step_input(prices={"MINT1": 1.10},
                         market_fast={"MINT1": False})
        result = step_loop(pf, inp, FakeSwapExecutor(),
                           FakeRouteChecker(True))
        self.assertEqual(len(result.sells), 1)
        self.assertEqual(result.sells[0][1], "take_profit_slow_+10-30%")

    def test_fast_window_peak_drop_8_percent_sells(self):
        pf = Portfolio(cash_sol=10.0)
        pf.positions["MINT1"] = self._position(peak=1.40)
        inp = step_input(prices={"MINT1": 1.28},
                         market_fast={"MINT1": True})
        result = step_loop(pf, inp, FakeSwapExecutor(),
                           FakeRouteChecker(True))
        self.assertEqual(len(result.sells), 1)
        self.assertIn("peak_drop_8%", result.sells[0][1])

    def test_unknown_market_speed_holds_slow_tp(self):
        pf = Portfolio(cash_sol=10.0)
        pf.positions["MINT1"] = self._position()
        inp = step_input(prices={"MINT1": 1.10},
                         market_fast={})
        result = step_loop(pf, inp, FakeSwapExecutor(),
                           FakeRouteChecker(True))
        self.assertEqual(result.sells, [])
        self.assertIn("MINT1", pf.positions)

    def test_unknown_market_speed_still_sells_hard_stop(self):
        pf = Portfolio(cash_sol=10.0)
        pf.positions["MINT1"] = self._position()
        inp = step_input(prices={"MINT1": 0.90},
                         market_fast={})
        result = step_loop(pf, inp, FakeSwapExecutor(),
                           FakeRouteChecker(True))
        self.assertEqual(len(result.sells), 1)

    def test_cap_plus_1000_percent_sells(self):
        pf = Portfolio(cash_sol=10.0)
        pf.positions["MINT1"] = self._position(entry=0.01, invested=2.0,
                                               peak=0.11)
        inp = step_input(prices={"MINT1": 0.11},
                         market_fast={})
        result = step_loop(pf, inp, FakeSwapExecutor(),
                           FakeRouteChecker(True))
        self.assertEqual(len(result.sells), 1)
        self.assertEqual(result.sells[0][1], "take_profit_cap_+1000%")


class TestSmokeTestMode(unittest.TestCase):
    def test_smoke_test_default_checker_fails_closed(self):
        self.assertEqual(smoke_test(), 2)
        self.assertEqual(smoke_test(UnverifiedRouteChecker()), 2)

    def test_smoke_test_does_not_require_live_or_signer(self):
        old = dict(os.environ)
        try:
            os.environ.pop("LIVE_ENABLED", None)
            os.environ.pop("TRADING_PRIVATE_KEY", None)
            os.environ.pop("TRADING_KEYPAIR", None)
            # Still fails closed on route, not on live/signer.
            self.assertEqual(smoke_test(), 2)
        finally:
            os.environ.clear()
            os.environ.update(old)


if __name__ == "__main__":
    unittest.main()
