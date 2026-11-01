"""Unit tests for the DISABLED live 5-bot engine (engine.py), SOL-only.

No real key, no network, no transaction in any test. The dummy
TRADING_KEYPAIR value used in the signer tests is a TEST-ONLY in-memory
byte pattern (1..64); it is never written to a file, never used for
mainnet, and never represents a real wallet.

SOL-only (James, 2026-10-31): cash, positions, buys, sells, and parking
are all in SOL. Token prices are in SOL per token. The halt is computed
in CAD from the SOL portfolio via a live SOL price, and a stale/missing
SOL price raises (fail-closed) instead of returning a value.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import (  # noqa: E402
    Candidate, Portfolio, Position, base58_encode, buy, buy_size_sol,
    can_buy, check_halt, crash_watch_active, ed25519_publickey_from_seed,
    evaluate_sell, get_network_config, is_live_enabled, load_signer,
    load_signer_with_status, parse_network, pipeline_pass, price_is_fresh,
    sell_all_on_halt, should_halt,
)

# SOL price used in halt tests: $100 USD/SOL, fresh (5s old).
SOL_PRICE_USD = 100.0
SOL_PRICE_AGE = 5.0
# At $100/SOL and 0.712 USD/CAD: 0.1424 SOL = $14.24 USD = $20.00 CAD.
HALT_SOL = 0.1424
START_SOL = 0.6073  # 0.6073 SOL * $100 / 0.712 = ~$85.30 CAD (above halt)


def fresh_candidate(mint="MINT1", price_sol=0.01, **kw):
    base = dict(mint=mint, gain_30s=0.05, gained_again_5s=True,
                still_gaining=True, liquidity_usd=50_000.0,
                buys=100.0, sells=50.0, price_sol=price_sol,
                price_age_seconds=5.0)
    base.update(kw)
    return Candidate(**base)


def portfolio_with_cash(cash_sol=START_SOL):
    return Portfolio(cash_sol=cash_sol)


def halt_cad(portfolio, prices_sol=None):
    return portfolio.total_value_cad(
        prices_sol or {}, SOL_PRICE_USD, SOL_PRICE_AGE)


class TestSizing(unittest.TestCase):
    def test_buy_size_is_exactly_20_percent(self):
        self.assertAlmostEqual(buy_size_sol(1.0), 0.2)
        self.assertAlmostEqual(buy_size_sol(0.6073), 0.12146)

    def test_buy_size_compounds_with_bankroll(self):
        self.assertAlmostEqual(buy_size_sol(2.0), 0.4)
        self.assertAlmostEqual(buy_size_sol(10.0), 2.0)
        self.assertAlmostEqual(buy_size_sol(0.5), 0.1)

    def test_buy_uses_20_percent_and_reduces_cash_sol(self):
        pf = portfolio_with_cash(0.6073)
        pos = buy(pf, fresh_candidate(), 0.6073, live_enabled=True)
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(pos.invested_sol, 0.12146)
        self.assertAlmostEqual(pf.cash_sol, 0.6073 - 0.12146)
        # Tokens = SOL spent / SOL price per token.
        self.assertAlmostEqual(pos.tokens, 0.12146 / 0.01)


class TestHalt(unittest.TestCase):
    def test_halt_exact_20_cad_triggers(self):
        self.assertTrue(should_halt(20.0))

    def test_halt_below_20_cad_triggers(self):
        self.assertTrue(should_halt(19.99))
        self.assertTrue(should_halt(0.0))

    def test_halt_above_20_cad_does_not_trigger(self):
        self.assertFalse(should_halt(20.01))
        self.assertFalse(should_halt(85.0))

    def test_check_halt_sets_halted_flag_in_sol_terms(self):
        pf = portfolio_with_cash(0.10)  # 0.10 SOL * $100 / 0.712 = ~$14.04 CAD
        self.assertTrue(check_halt(pf, {}, SOL_PRICE_USD, SOL_PRICE_AGE))
        self.assertTrue(pf.halted)

    def test_check_halt_does_not_trigger_above_threshold(self):
        pf = portfolio_with_cash(START_SOL)  # ~$85 CAD
        self.assertFalse(check_halt(pf, {}, SOL_PRICE_USD, SOL_PRICE_AGE))
        self.assertFalse(pf.halted)

    def test_check_halt_exact_threshold_in_sol(self):
        pf = portfolio_with_cash(HALT_SOL)
        self.assertTrue(check_halt(pf, {}, SOL_PRICE_USD, SOL_PRICE_AGE))

    def test_sell_all_on_halt_parks_in_sol(self):
        pf = portfolio_with_cash(0.20)
        pf.positions["A"] = Position("A", 0.01, 0.10, 10.0, 0.01)
        pf.positions["B"] = Position("B", 0.02, 0.20, 10.0, 0.02)
        sold, failed = sell_all_on_halt(
            pf, {}, lambda mint: ({"A": 0.015, "B": 0.02}[mint], 5.0))
        self.assertEqual(sorted(sold), ["A", "B"])
        self.assertEqual(failed, [])
        self.assertEqual(pf.positions, {})
        # Cash: 0.20 + 10*0.015 + 10*0.02 = 0.55 SOL, all parked in SOL.
        self.assertAlmostEqual(pf.cash_sol, 0.55)
        self.assertTrue(pf.halted)

    def test_no_buy_when_halted(self):
        pf = portfolio_with_cash(START_SOL)
        pf.halted = True
        self.assertFalse(can_buy(pf, fresh_candidate(), live_enabled=True))
        self.assertIsNone(buy(pf, fresh_candidate(), START_SOL, live_enabled=True))

    def test_no_buy_when_live_disabled(self):
        pf = portfolio_with_cash(START_SOL)
        self.assertFalse(can_buy(pf, fresh_candidate(), live_enabled=False))
        self.assertIsNone(buy(pf, fresh_candidate(), START_SOL, live_enabled=False))

    def test_sell_all_retries_once_and_reports_failure_loudly(self):
        pf = portfolio_with_cash(0.20)
        pf.positions["A"] = Position("A", 0.01, 0.10, 10.0, 0.01)
        pf.positions["B"] = Position("B", 0.02, 0.20, 10.0, 0.02)
        calls = {"B": 0}

        def provider(mint):
            if mint == "A":
                return 0.015, 5.0
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


class TestSolPriceFailClosed(unittest.TestCase):
    """Halt via live SOL price: stale/missing SOL price must raise,
    never return a value that could hide a halt."""

    def test_missing_sol_price_raises(self):
        pf = portfolio_with_cash(0.10)  # would halt if priced
        with self.assertRaises(ValueError):
            pf.total_value_cad({}, None, 5.0)
        with self.assertRaises(ValueError):
            check_halt(pf, {}, None, 5.0)
        self.assertFalse(pf.halted)  # raise, not a silent halt decision

    def test_stale_sol_price_raises(self):
        pf = portfolio_with_cash(0.10)
        with self.assertRaises(ValueError):
            pf.total_value_cad({}, SOL_PRICE_USD, 31.0)
        with self.assertRaises(ValueError):
            pf.total_value_cad({}, SOL_PRICE_USD, None)
        with self.assertRaises(ValueError):
            check_halt(pf, {}, SOL_PRICE_USD, 999.0)

    def test_zero_sol_price_raises(self):
        pf = portfolio_with_cash(0.10)
        with self.assertRaises(ValueError):
            pf.total_value_cad({}, 0.0, 5.0)

    def test_fresh_sol_price_computes_cad(self):
        pf = portfolio_with_cash(HALT_SOL)
        self.assertAlmostEqual(halt_cad(pf), 20.0, places=2)
        pf2 = portfolio_with_cash(START_SOL)
        self.assertGreater(halt_cad(pf2), 20.0)

    def test_sol_drop_alone_moves_toward_halt(self):
        # Same SOL balance, lower SOL price -> lower CAD value -> halt.
        # This is the stated consequence of SOL-only (no stable park).
        pf = portfolio_with_cash(0.20)  # $20 USD at $100 = ~$28 CAD, above halt
        self.assertFalse(check_halt(pf, {}, 100.0, 5.0))
        pf2 = portfolio_with_cash(0.20)
        self.assertTrue(check_halt(pf2, {}, 50.0, 5.0))  # $10 USD = ~$14 CAD


class TestFailClosedPricing(unittest.TestCase):
    def test_stale_price_blocks_pipeline(self):
        self.assertFalse(pipeline_pass(fresh_candidate(price_age_seconds=31.0)))
        self.assertFalse(pipeline_pass(fresh_candidate(price_age_seconds=999.0)))

    def test_missing_price_blocks_pipeline(self):
        self.assertFalse(pipeline_pass(fresh_candidate(price_sol=None)))
        self.assertFalse(pipeline_pass(fresh_candidate(price_sol=0.0)))

    def test_price_is_fresh_boundaries(self):
        self.assertTrue(price_is_fresh(0.01, 30.0))
        self.assertFalse(price_is_fresh(0.01, 30.01))
        self.assertFalse(price_is_fresh(None, 5.0))
        self.assertFalse(price_is_fresh(0.01, None))

    def test_stale_price_never_sells(self):
        pos = Position("A", 0.01, 0.10, 10.0, 0.01)
        self.assertIsNone(evaluate_sell(pos, 0.009, 999.0))
        self.assertIsNone(evaluate_sell(pos, 0.009, None))


class TestMaxOpenAndOnePerCoin(unittest.TestCase):
    def test_max_five_open(self):
        pf = portfolio_with_cash(10.0)
        for i in range(5):
            pf.positions[f"M{i}"] = Position(f"M{i}", 0.01, 0.10, 10.0, 0.01)
        self.assertFalse(can_buy(pf, fresh_candidate(mint="M5"), live_enabled=True))

    def test_one_position_per_coin(self):
        pf = portfolio_with_cash(10.0)
        pf.positions["MINT1"] = Position("MINT1", 0.01, 0.10, 10.0, 0.01)
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
        pos = Position("A", 0.01, 0.10, 10.0, 0.01)
        self.assertIsNone(evaluate_sell(pos, 0.00931, 5.0))   # -6.9%
        self.assertEqual(evaluate_sell(pos, 0.0093, 5.0), "hard_stop_-7%")  # -7.0%
        self.assertEqual(evaluate_sell(pos, 0.0092, 5.0), "hard_stop_-7%")  # -8%

    def test_slow_take_profit_boundaries(self):
        # 0.55/0.5 is exactly +10% in float; 0.011/0.01 is 9.999...% and
        # would miss the boundary, so the boundary pair uses 0.5/0.55.
        pos = Position("A", 0.5, 0.10, 0.20, 0.5)
        self.assertIsNone(evaluate_sell(pos, 0.549, 5.0, market_fast=False))  # +9.8%
        self.assertEqual(evaluate_sell(pos, 0.55, 5.0, market_fast=False),
                         "take_profit_slow_+10-30%")  # +10%

    def test_fast_window_holds_then_peak_drop_sells(self):
        pos = Position("A", 0.01, 0.10, 10.0, 0.014)  # peak +40%
        self.assertIsNone(evaluate_sell(pos, 0.013, 5.0, market_fast=True))
        # 8% drop from 0.014 peak = 0.01288
        self.assertEqual(evaluate_sell(pos, 0.01288, 5.0, market_fast=True),
                         "peak_drop_8%_in_31-50%_window")
        pos2 = Position("A", 0.01, 0.10, 10.0, 0.014)
        self.assertIsNone(evaluate_sell(pos2, 0.01289, 5.0, market_fast=True))

    def test_past_50_peak_drop_boundaries(self):
        pos = Position("A", 0.01, 0.10, 10.0, 0.016)  # peak +60%
        self.assertIsNone(evaluate_sell(pos, 0.0141, 5.0))   # 11.9% drop
        self.assertEqual(evaluate_sell(pos, 0.01408, 5.0),
                         "peak_drop_12%_past_+50%")  # 12% drop

    def test_take_profit_cap(self):
        pos = Position("A", 0.01, 0.10, 10.0, 0.11)
        self.assertEqual(evaluate_sell(pos, 0.11, 5.0), "take_profit_cap_+1000%")


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


class TestNetwork(unittest.TestCase):
    """NETWORK selection (devnet-first, mainnet path preserved, SOL-only).

    No network access in these tests; they pin config selection only.
    """

    def test_unset_defaults_to_mainnet_unchanged(self):
        old = os.environ.pop("NETWORK", None)
        try:
            from engine import network_from_env
            cfg = network_from_env()
            self.assertEqual(cfg.name, "mainnet")
            self.assertEqual(cfg.rpc_url,
                             "https://api.mainnet-beta.solana.com")
        finally:
            if old is not None:
                os.environ["NETWORK"] = old

    def test_parse_network_values(self):
        self.assertEqual(parse_network("devnet"), "devnet")
        self.assertEqual(parse_network("mainnet"), "mainnet")
        self.assertEqual(parse_network("  DevNet "), "devnet")
        self.assertEqual(parse_network(""), "mainnet")
        self.assertEqual(parse_network(None), "mainnet")

    def test_parse_network_invalid_fails_closed(self):
        for bad in ("dev", "main", "testnet", "solana", "devnet1"):
            with self.assertRaises(ValueError, msg=bad):
                parse_network(bad)
            with self.assertRaises(ValueError, msg=bad):
                get_network_config(bad)

    def test_devnet_config(self):
        cfg = get_network_config("devnet")
        self.assertEqual(cfg.name, "devnet")
        self.assertEqual(cfg.rpc_url, "https://api.devnet.solana.com")
        # SOL-only: no stablecoin mint on either network; same SOL mint.
        self.assertFalse(hasattr(cfg, "usdc_mint"))
        self.assertEqual(cfg.sol_mint,
                         get_network_config("mainnet").sol_mint)
        # Routing is NOT verified for either network in this build.
        self.assertFalse(cfg.swap_route_verified_in_build)
        self.assertFalse(get_network_config("mainnet").swap_route_verified_in_build)

    def test_rules_unchanged_under_devnet(self):
        # Network selection must not change any trading rule: sizing,
        # pipeline, sells, and halt are network-independent.
        _ = get_network_config("devnet")  # selection itself is the setup
        self.assertAlmostEqual(buy_size_sol(0.6073), 0.12146)
        self.assertTrue(pipeline_pass(fresh_candidate()))
        self.assertTrue(should_halt(20.0))
        self.assertFalse(should_halt(20.01))
        pf = portfolio_with_cash(START_SOL)  # ~$85 CAD at $100/SOL, 0.712
        self.assertFalse(check_halt(pf, {}, SOL_PRICE_USD, SOL_PRICE_AGE))
        pos = Position("A", 0.01, 0.10, 10.0, 0.01)
        self.assertEqual(evaluate_sell(pos, 0.0093, 5.0), "hard_stop_-7%")
        pf2 = portfolio_with_cash(0.20)
        pf2.positions["A"] = Position("A", 0.01, 0.10, 10.0, 0.01)
        sold, failed = sell_all_on_halt(
            pf2, {}, lambda mint: (0.015, 5.0))
        self.assertEqual(sold, ["A"])
        self.assertEqual(failed, [])
        self.assertTrue(pf2.halted)


class TestSolOnly(unittest.TestCase):
    """SOL-only trade paths: no stablecoin anywhere in code trade paths.

    The word 'stablecoin' in docs is history (RULES §0 superseded note)
    and is NOT a live instruction; code files must not contain a
    stablecoin mint, conversion, or parking at all.
    """

    def test_no_stablecoin_references_in_code_files(self):
        import pathlib
        base = pathlib.Path(__file__).resolve().parent.parent
        for name in ("engine.py", "dry_run.py"):
            text = (base / name).read_text().lower()
            self.assertNotIn("usdc", text, name)
            self.assertNotIn("usdt", text, name)
            self.assertNotIn("stablecoin", text, name)

    def test_no_stablecoin_mint_in_network_config(self):
        for net in ("devnet", "mainnet"):
            cfg = get_network_config(net)
            self.assertFalse(hasattr(cfg, "usdc_mint"))
            self.assertFalse(hasattr(cfg, "stable_mint"))

    def test_buy_is_sol_to_token_and_sell_parks_in_sol(self):
        pf = portfolio_with_cash(1.0)
        pos = buy(pf, fresh_candidate(price_sol=0.02), 1.0, live_enabled=True)
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(pos.invested_sol, 0.20)
        self.assertAlmostEqual(pf.cash_sol, 0.80)
        self.assertAlmostEqual(pos.tokens, 0.20 / 0.02)
        # Sell the position at +50%: proceeds in SOL, parked in SOL cash.
        sold, failed = sell_all_on_halt(
            pf, {}, lambda mint: (0.03, 5.0))
        self.assertEqual(sold, ["MINT1"])
        self.assertEqual(failed, [])
        self.assertAlmostEqual(pf.cash_sol, 0.80 + 10.0 * 0.03)
        self.assertEqual(pf.positions, {})

    def test_rules_unchanged_except_currency(self):
        # 20% sizing, max 5, one per coin, -7% stop, TP rules, pipeline:
        # all unchanged, only the currency changed to SOL.
        self.assertAlmostEqual(buy_size_sol(1.0), 0.2)
        self.assertTrue(pipeline_pass(fresh_candidate()))
        pos = Position("A", 0.01, 0.10, 10.0, 0.01)
        self.assertEqual(evaluate_sell(pos, 0.0093, 5.0), "hard_stop_-7%")
        self.assertEqual(
            evaluate_sell(Position("A", 0.5, 0.10, 0.20, 0.5),
                          0.55, 5.0, market_fast=False),
            "take_profit_slow_+10-30%")


if __name__ == "__main__":
    unittest.main()
