"""Unit tests for the DISABLED live 5-bot engine (engine.py).

No real key, no network, no mainnet transaction in any test. The dummy
TRADING_KEYPAIR value used in the signer tests is a TEST-ONLY in-memory
byte pattern (1..64); it is never written to a file, never used for
mainnet, and never represents a real wallet.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import (  # noqa: E402
    Candidate, Portfolio, Position, base58_encode, buy, buy_size_usd,
    can_buy, check_halt, crash_watch_active, ed25519_publickey_from_seed,
    evaluate_sell, is_live_enabled, load_signer,
    load_signer_with_status, pipeline_pass, price_is_fresh,
    sell_all_on_halt, should_halt,
)


def fresh_candidate(mint="MINT1", price=1.0, **kw):
    base = dict(mint=mint, gain_30s=0.05, gained_again_5s=True,
                still_gaining=True, liquidity_usd=50_000.0,
                buys=100.0, sells=50.0, price_usd=price,
                price_age_seconds=5.0)
    base.update(kw)
    return Candidate(**base)


def portfolio_with_cash(cash=60.52):
    return Portfolio(cash_usdc=cash)


class TestSizing(unittest.TestCase):
    def test_buy_size_is_exactly_20_percent(self):
        self.assertAlmostEqual(buy_size_usd(100.0), 20.0)
        self.assertAlmostEqual(buy_size_usd(60.52), 12.104)

    def test_buy_size_compounds_with_bankroll(self):
        # Bankroll grows -> next buy grows; bankroll shrinks -> next buy shrinks.
        self.assertAlmostEqual(buy_size_usd(200.0), 40.0)
        self.assertAlmostEqual(buy_size_usd(1000.0), 200.0)
        self.assertAlmostEqual(buy_size_usd(50.0), 10.0)

    def test_buy_uses_20_percent_and_reduces_cash(self):
        pf = portfolio_with_cash(60.52)
        pos = buy(pf, fresh_candidate(), 60.52, live_enabled=True)
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(pos.invested_usd, 12.104)
        self.assertAlmostEqual(pf.cash_usdc, 60.52 - 12.104)


class TestHalt(unittest.TestCase):
    def test_halt_exact_20_cad_triggers(self):
        self.assertTrue(should_halt(20.0))

    def test_halt_below_20_cad_triggers(self):
        self.assertTrue(should_halt(19.99))
        self.assertTrue(should_halt(0.0))

    def test_halt_above_20_cad_does_not_trigger(self):
        self.assertFalse(should_halt(20.01))
        self.assertFalse(should_halt(85.0))

    def test_check_halt_sets_halted_flag(self):
        pf = portfolio_with_cash(10.0)  # 10 USD / 0.712 = ~14.04 CAD
        self.assertTrue(check_halt(pf, {}))
        self.assertTrue(pf.halted)

    def test_check_halt_does_not_trigger_above_threshold(self):
        pf = portfolio_with_cash(60.52)  # ~85 CAD
        self.assertFalse(check_halt(pf, {}))
        self.assertFalse(pf.halted)

    def test_sell_all_on_halt_parks_in_usdc(self):
        pf = portfolio_with_cash(20.0)
        pf.positions["A"] = Position("A", 1.0, 10.0, 10.0, 1.0)
        pf.positions["B"] = Position("B", 2.0, 20.0, 10.0, 2.0)
        sold, failed = sell_all_on_halt(
            pf, {}, lambda mint: ({"A": 1.5, "B": 2.0}[mint], 5.0))
        self.assertEqual(sorted(sold), ["A", "B"])
        self.assertEqual(failed, [])
        self.assertEqual(pf.positions, {})
        # Cash: 20 + 10*1.5 + 10*2.0 = 55 USDC, all parked in USDC.
        self.assertAlmostEqual(pf.cash_usdc, 55.0)
        self.assertTrue(pf.halted)

    def test_no_buy_when_halted(self):
        pf = portfolio_with_cash(60.52)
        pf.halted = True
        self.assertFalse(can_buy(pf, fresh_candidate(), live_enabled=True))
        self.assertIsNone(buy(pf, fresh_candidate(), 60.52, live_enabled=True))

    def test_no_buy_when_live_disabled(self):
        pf = portfolio_with_cash(60.52)
        self.assertFalse(can_buy(pf, fresh_candidate(), live_enabled=False))
        self.assertIsNone(buy(pf, fresh_candidate(), 60.52, live_enabled=False))

    def test_sell_all_retries_once_and_reports_failure_loudly(self):
        pf = portfolio_with_cash(20.0)
        pf.positions["A"] = Position("A", 1.0, 10.0, 10.0, 1.0)
        pf.positions["B"] = Position("B", 2.0, 20.0, 10.0, 2.0)
        calls = {"B": 0}

        def provider(mint):
            if mint == "A":
                return 1.5, 5.0
            calls["B"] += 1
            raise RuntimeError("rpc down")

        sold, failed = sell_all_on_halt(pf, {}, provider)
        self.assertEqual(sold, ["A"])
        self.assertEqual(failed, ["B"])
        self.assertEqual(calls["B"], 2)  # first try + one retry
        self.assertEqual(len(pf.sell_failures), 1)
        self.assertEqual(pf.sell_failures[0]["mint"], "B")
        # Failed position is KEPT (visible), portfolio stays halted.
        self.assertIn("B", pf.positions)
        self.assertTrue(pf.halted)
        self.assertFalse(can_buy(pf, fresh_candidate(), live_enabled=True))


class TestFailClosedPricing(unittest.TestCase):
    def test_stale_price_blocks_pipeline(self):
        self.assertFalse(pipeline_pass(fresh_candidate(price_age_seconds=31.0)))
        self.assertFalse(pipeline_pass(fresh_candidate(price_age_seconds=999.0)))

    def test_missing_price_blocks_pipeline(self):
        self.assertFalse(pipeline_pass(fresh_candidate(price_usd=None)))
        self.assertFalse(pipeline_pass(fresh_candidate(price_usd=0.0)))

    def test_price_is_fresh_boundaries(self):
        self.assertTrue(price_is_fresh(1.0, 30.0))
        self.assertFalse(price_is_fresh(1.0, 30.01))
        self.assertFalse(price_is_fresh(None, 5.0))
        self.assertFalse(price_is_fresh(1.0, None))

    def test_stale_price_never_sells(self):
        pos = Position("A", 1.0, 10.0, 10.0, 1.0)
        self.assertIsNone(evaluate_sell(pos, 0.90, 999.0))
        self.assertIsNone(evaluate_sell(pos, 0.90, None))


class TestMaxOpenAndOnePerCoin(unittest.TestCase):
    def test_max_five_open(self):
        pf = portfolio_with_cash(1000.0)
        for i in range(5):
            pf.positions[f"M{i}"] = Position(f"M{i}", 1.0, 10.0, 10.0, 1.0)
        self.assertFalse(can_buy(pf, fresh_candidate(mint="M5"), live_enabled=True))

    def test_one_position_per_coin(self):
        pf = portfolio_with_cash(1000.0)
        pf.positions["MINT1"] = Position("MINT1", 1.0, 10.0, 10.0, 1.0)
        self.assertFalse(can_buy(pf, fresh_candidate(mint="MINT1"), live_enabled=True))
        self.assertTrue(can_buy(pf, fresh_candidate(mint="MINT2"), live_enabled=True))


class TestPipeline(unittest.TestCase):
    def test_full_pass(self):
        self.assertTrue(pipeline_pass(fresh_candidate()))

    def test_screener_below_2_percent_fails(self):
        self.assertFalse(pipeline_pass(fresh_candidate(gain_30s=0.019)))
        self.assertTrue(pipeline_pass(fresh_candidate(gain_30s=0.02)))

    def test_analyst_no_reconfirm_fails(self):
        self.assertFalse(pipeline_pass(fresh_candidate(gained_again_5s=False)))

    def test_secondary_not_still_gaining_fails(self):
        self.assertFalse(pipeline_pass(fresh_candidate(still_gaining=False)))

    def test_low_liquidity_fails(self):
        self.assertFalse(pipeline_pass(fresh_candidate(liquidity_usd=9_999.0)))

    def test_seller_dominance_fails(self):
        self.assertFalse(pipeline_pass(fresh_candidate(buys=100.0, sells=120.0)))
        self.assertTrue(pipeline_pass(fresh_candidate(buys=100.0, sells=119.0)))

    def test_crash_watch_blocks_buys(self):
        self.assertTrue(crash_watch_active(4, 10))
        self.assertFalse(crash_watch_active(3, 10))
        self.assertFalse(pipeline_pass(fresh_candidate(), crash_watch_active=True))


class TestSellBoundaries(unittest.TestCase):
    def test_hard_stop_boundaries(self):
        pos = Position("A", 1.0, 10.0, 10.0, 1.0)
        self.assertIsNone(evaluate_sell(pos, 0.931, 5.0))   # -6.9%
        self.assertEqual(evaluate_sell(pos, 0.93, 5.0), "hard_stop_-7%")  # -7.0%
        self.assertEqual(evaluate_sell(pos, 0.92, 5.0), "hard_stop_-7%")  # -8%

    def test_slow_take_profit_boundaries(self):
        pos = Position("A", 1.0, 10.0, 10.0, 1.0)
        self.assertIsNone(evaluate_sell(pos, 1.099, 5.0, market_fast=False))  # +9.9%
        self.assertEqual(evaluate_sell(pos, 1.10, 5.0, market_fast=False),
                         "take_profit_slow_+10-30%")  # +10%

    def test_fast_window_holds_then_peak_drop_sells(self):
        pos = Position("A", 1.0, 10.0, 10.0, 1.40)  # peak +40%
        self.assertIsNone(evaluate_sell(pos, 1.30, 5.0, market_fast=True))
        # 8% drop from 1.40 peak = 1.288
        self.assertEqual(evaluate_sell(pos, 1.288, 5.0, market_fast=True),
                         "peak_drop_8%_in_31-50%_window")
        pos2 = Position("A", 1.0, 10.0, 10.0, 1.40)
        self.assertIsNone(evaluate_sell(pos2, 1.289, 5.0, market_fast=True))

    def test_past_50_peak_drop_boundaries(self):
        pos = Position("A", 1.0, 10.0, 10.0, 1.60)  # peak +60%
        self.assertIsNone(evaluate_sell(pos, 1.41, 5.0))   # 11.9% drop
        self.assertEqual(evaluate_sell(pos, 1.408, 5.0),
                         "peak_drop_12%_past_+50%")  # 12% drop

    def test_take_profit_cap(self):
        pos = Position("A", 1.0, 10.0, 10.0, 11.0)
        self.assertEqual(evaluate_sell(pos, 11.0, 5.0), "take_profit_cap_+1000%")


def _test_keypair(tag=7):
    """Test-only keypair, generated in memory, never written to disk.

    Returns (seed, secret64, base58_secret, json_secret, pubkey_b58).
    Never a real wallet; never used for mainnet.
    """
    seed = bytes(((i * 37 + tag) % 256) for i in range(32))
    pub = ed25519_publickey_from_seed(seed)
    secret = seed + pub
    return (seed, secret, base58_encode(secret), str(list(secret)),
            base58_encode(pub))


def _set_key_env(live="1", pair=None, b58=None):
    if live is None:
        os.environ.pop("LIVE_ENABLED", None)
    else:
        os.environ["LIVE_ENABLED"] = live
    if pair is None:
        os.environ.pop("TRADING_KEYPAIR", None)
    else:
        os.environ["TRADING_KEYPAIR"] = pair
    if b58 is None:
        os.environ.pop("TRADING_PRIVATE_KEY", None)
    else:
        os.environ["TRADING_PRIVATE_KEY"] = b58


class TestLiveGateAndKey(unittest.TestCase):
    def setUp(self):
        self._old_live = os.environ.pop("LIVE_ENABLED", None)
        self._old_key = os.environ.pop("TRADING_KEYPAIR", None)
        self._old_b58 = os.environ.pop("TRADING_PRIVATE_KEY", None)

    def tearDown(self):
        if self._old_live is not None:
            os.environ["LIVE_ENABLED"] = self._old_live
        else:
            os.environ.pop("LIVE_ENABLED", None)
        if self._old_key is not None:
            os.environ["TRADING_KEYPAIR"] = self._old_key
        else:
            os.environ.pop("TRADING_KEYPAIR", None)
        if self._old_b58 is not None:
            os.environ["TRADING_PRIVATE_KEY"] = self._old_b58
        else:
            os.environ.pop("TRADING_PRIVATE_KEY", None)

    def test_live_defaults_off(self):
        os.environ.pop("LIVE_ENABLED", None)
        self.assertFalse(is_live_enabled())

    def test_signer_refused_without_key(self):
        os.environ["LIVE_ENABLED"] = "1"
        os.environ.pop("TRADING_KEYPAIR", None)
        self.assertIsNone(load_signer())

    def test_signer_refused_when_live_off_even_with_key(self):
        os.environ["LIVE_ENABLED"] = "0"
        # TEST-ONLY dummy pattern, never a real wallet, never written to disk.
        os.environ["TRADING_KEYPAIR"] = str(list(range(1, 65)))
        self.assertIsNone(load_signer())

    def test_signer_marker_never_exposes_key_material(self):
        # A valid-shape key whose derived pubkey is NOT the settled live
        # wallet is refused, and the refusal exposes no key material.
        _seed, _secret, _b58, json_secret, _pub = _test_keypair(11)
        _set_key_env(live="1", pair=json_secret)
        marker, error = load_signer_with_status()
        self.assertIsNone(marker)
        self.assertEqual(error, "pubkey_mismatch")
        self.assertNotIn(json_secret, repr(marker) + error)
        # A key that DOES match its expected pubkey is accepted, and its
        # marker still contains no key bytes.
        _seed2, _secret2, b58_2, _json2, pub2 = _test_keypair(13)
        _set_key_env(live="1", pair=_json2)
        marker2, error2 = load_signer_with_status(pub2)
        self.assertIsNotNone(marker2)
        self.assertIsNone(error2)
        self.assertNotIn(b58_2, repr(marker2))
        self.assertNotIn(_json2, repr(marker2))


class TestBase58Key(unittest.TestCase):
    """TRADING_PRIVATE_KEY (Phantom base58 export form).

    All keys below are test-only in-memory patterns, never written to
    disk, never a real wallet, never used for mainnet.
    """

    def setUp(self):
        self._old = {}
        for name in ("LIVE_ENABLED", "TRADING_KEYPAIR", "TRADING_PRIVATE_KEY"):
            self._old[name] = os.environ.pop(name, None)

    def tearDown(self):
        for name, value in self._old.items():
            if value is not None:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)

    def test_base58_key_accepted_and_validated(self):
        _seed, _secret, b58, _json, pub = _test_keypair(21)
        _set_key_env(live="1", b58=b58)
        marker, error = load_signer_with_status(pub)
        self.assertIsNotNone(marker)
        self.assertIsNone(error)
        self.assertEqual(marker["source"], "TRADING_PRIVATE_KEY")
        self.assertEqual(marker["validated_pubkey"], pub)
        self.assertIsNotNone(load_signer(pub))

    def test_base58_seed_only_accepted(self):
        seed, _secret, _b58, _json, pub = _test_keypair(23)
        _set_key_env(live="1", b58=base58_encode(seed))
        marker, error = load_signer_with_status(pub)
        self.assertIsNotNone(marker)
        self.assertIsNone(error)

    def test_base58_wrong_pubkey_refused(self):
        _seed, _secret, b58, _json, _pub = _test_keypair(29)
        _set_key_env(live="1", b58=b58)
        # Default expected pubkey is the settled live wallet, which this
        # test key does not belong to.
        marker, error = load_signer_with_status()
        self.assertIsNone(marker)
        self.assertEqual(error, "pubkey_mismatch")
        self.assertIsNone(load_signer())
        self.assertNotIn(b58, error)

    def test_base58_invalid_format_refused(self):
        _set_key_env(live="1", b58="not-base58!!0OIl")
        marker, error = load_signer_with_status()
        self.assertIsNone(marker)
        self.assertEqual(error, "invalid_key_format")
        _set_key_env(live="1", b58=base58_encode(b"short"))
        marker, error = load_signer_with_status()
        self.assertIsNone(marker)
        self.assertEqual(error, "invalid_key_format")

    def test_base58_mismatched_secret_halves_refused(self):
        # Seed A with a different seed's public key embedded: malformed.
        seed_a, _s, _b, _j, _p = _test_keypair(31)
        other_pub = ed25519_publickey_from_seed(
            bytes([(i * 37 + 41) % 256 for i in range(32)]))
        bad_secret = seed_a + other_pub
        _set_key_env(live="1", b58=base58_encode(bad_secret))
        marker, error = load_signer_with_status()
        self.assertIsNone(marker)
        self.assertEqual(error, "invalid_key_format")

    def test_both_vars_set_refused(self):
        _seed, _secret, b58, json_secret, pub = _test_keypair(43)
        _set_key_env(live="1", pair=json_secret, b58=b58)
        marker, error = load_signer_with_status(pub)
        self.assertIsNone(marker)
        self.assertEqual(error.split(":")[0], "both_keys_set")
        self.assertIsNone(load_signer(pub))
        self.assertNotIn(b58, error)
        self.assertNotIn(json_secret, error)

    def test_json_array_behaviour_unchanged(self):
        # Valid JSON array, matching its expected pubkey: accepted.
        _seed, _secret, b58, json_secret, pub = _test_keypair(47)
        _set_key_env(live="1", pair=json_secret)
        marker, error = load_signer_with_status(pub)
        self.assertIsNotNone(marker)
        self.assertIsNone(error)
        self.assertEqual(marker["source"], "TRADING_KEYPAIR")
        # Malformed JSON array forms: refused, same as before.
        for bad in ("[1, 2, 3]", "[256]" + ", 0" * 0, "not json",
                    str([0] * 64)[:10]):
            _set_key_env(live="1", pair=bad)
            marker, error = load_signer_with_status(pub)
            self.assertIsNone(marker, bad)
            self.assertEqual(error, "invalid_key_format", bad)
        # Valid shape, wrong pubkey: refused by validation.
        _set_key_env(live="1", pair=json_secret)
        marker, error = load_signer_with_status()
        self.assertIsNone(marker)
        self.assertEqual(error, "pubkey_mismatch")

    def test_no_key_material_in_output_or_logs(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr

        _seed, _secret, b58, json_secret, pub = _test_keypair(53)
        fragment = b58[:8]
        cases = [
            dict(live="1", b58=b58),
            dict(live="1", pair=json_secret, b58=b58),
            dict(live="1", pair=json_secret),
            dict(live="0", b58=b58),
        ]
        for case in cases:
            _set_key_env(**case)
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                marker, error = load_signer_with_status(pub)
                _ = load_signer(pub)
            blob = stdout.getvalue() + stderr.getvalue() + repr(marker) + (error or "")
            self.assertNotIn(b58, blob)
            self.assertNotIn(json_secret, blob)
            self.assertNotIn(fragment, blob)
            self.assertEqual(stdout.getvalue(), "")
            self.assertEqual(stderr.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
