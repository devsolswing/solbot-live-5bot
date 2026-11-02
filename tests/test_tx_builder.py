"""Tests for tx_builder.py. All mocked: in-memory test-only keys
(generated fresh per test, never written to a file, never the
real wallet key), mocked swap providers, mocked RPC. No real
signing of a real transaction, no real chain send, ever."""

import base64
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import (  # noqa: E402
    SOL_MINT, WALLET_PUBKEY, base58_decode, base58_encode,
    ed25519_publickey_from_seed,
)
from feed import watch_mints_from_env  # noqa: E402
from swap import Quote  # noqa: E402
from tx_builder import (  # noqa: E402
    DevnetPoolQuoter, SolanaTxBuilder, build_message, devnet_route_report,
    ed25519_sign, ed25519_verify, parse_message, parse_serialized_tx,
    sign_serialized_tx, compact_u16_encode,
)

TEST_MINT = "TestMint1111111111111111111111111111111111111"
BLOCKHASH = base58_encode(b"\x02" * 32)


def make_test_key():
    """Fresh in-memory test-only key. Returns (seed, pubkey_b58,
    base58_64byte_secret). Never the real wallet key."""
    seed = os.urandom(32)
    pub = ed25519_publickey_from_seed(seed)
    secret64 = seed + pub
    return seed, base58_encode(pub), base58_encode(secret64)


def make_unsigned_tx(fee_payer_b58):
    msg = build_message(fee_payer_b58, [
        {"program_id": "11111111111111111111111111111111",
         "accounts": [fee_payer_b58], "data": b""},
    ], BLOCKHASH)
    return base64.b64encode(
        compact_u16_encode(1) + b"\x00" * 64 + msg).decode("ascii")


def make_quote(input_mint, output_mint, in_amount, out_amount,
               fetched_at=None):
    return Quote(input_mint=input_mint, output_mint=output_mint,
                 input_amount=in_amount, output_amount=out_amount,
                 source="test", fetched_at=fetched_at)


class FakeRpc:
    def __init__(self, send_ok=True):
        self.send_ok = send_ok
        self.sent = []

    def send_transaction(self, signed_b64):
        self.sent.append(signed_b64)
        if not self.send_ok:
            return None, "tx_send_failed"
        return "FakeSig111111111111111111111111111111111111", None

    def confirm_transaction(self, signature, attempts=5, delay_seconds=0.0):
        return True, None

    def get_recent_blockhash(self):
        return BLOCKHASH, None


def provider_ok(fee_payer_b58, expected_output=None):
    def _provider(direction, mint, input_amount, min_output, quote,
                  wallet_pubkey):
        return {
            "serialized_tx_b64": make_unsigned_tx(fee_payer_b58),
            "expected_output": (expected_output if expected_output is not None
                                else float(quote.output_amount)),
            "input_mint": quote.input_mint,
            "output_mint": quote.output_mint,
            "source": "test_provider",
        }, None
    return _provider


class TxBuilderTest(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("LIVE_ENABLED", "NETWORK", "TRADING_PRIVATE_KEY",
                        "TRADING_KEYPAIR", "WATCH_MINTS")}
        self.seed, self.pubkey, self.secret_b58 = make_test_key()
        os.environ["LIVE_ENABLED"] = "1"
        os.environ["NETWORK"] = "devnet"
        os.environ["TRADING_PRIVATE_KEY"] = self.secret_b58
        os.environ.pop("TRADING_KEYPAIR", None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _builder(self, provider=None, rpc=None, expected=None):
        return SolanaTxBuilder(
            network="devnet", rpc_client=rpc or FakeRpc(),
            swap_tx_provider=provider,
            expected_pubkey=expected or self.pubkey, confirm=True)

    def _buy_quote(self, out=2_000_000):
        return make_quote(SOL_MINT, TEST_MINT, 1_000_000, out)

    # --- Ed25519 + message helpers ---
    def test_sign_verify_roundtrip(self):
        msg = b"hello solana"
        sig = ed25519_sign(self.seed, msg)
        pub_bytes = base58_decode(self.pubkey)
        self.assertTrue(ed25519_verify(pub_bytes, msg, sig))
        self.assertFalse(ed25519_verify(pub_bytes, b"other", sig))
        self.assertFalse(ed25519_verify(pub_bytes, msg, b"\x00" * 64))

    def test_build_parse_message_roundtrip(self):
        msg = build_message(self.pubkey, [
            {"program_id": "11111111111111111111111111111111",
             "accounts": [self.pubkey], "data": b"\x01\x02"},
        ], BLOCKHASH)
        parsed = parse_message(msg)
        self.assertEqual(parsed["account_keys"][0], self.pubkey)
        self.assertEqual(parsed["blockhash"], BLOCKHASH)
        self.assertEqual(len(parsed["instructions"]), 1)

    def test_sign_serialized_tx_fee_payer_mismatch(self):
        tx = make_unsigned_tx(self.pubkey)
        other = base58_encode(b"\x03" * 32)
        with self.assertRaises(ValueError):
            sign_serialized_tx(tx, self.seed, other)

    def test_sign_serialized_tx_verifies(self):
        tx = make_unsigned_tx(self.pubkey)
        signed = sign_serialized_tx(tx, self.seed, self.pubkey)
        sigs, message = parse_serialized_tx(signed)
        self.assertEqual(len(sigs), 1)
        self.assertTrue(ed25519_verify(
            base58_decode(self.pubkey), message, sigs[0]))

    # --- Builder gate refusals (no signing, no sending) ---
    def test_live_off_refuses(self):
        os.environ["LIVE_ENABLED"] = "0"
        rpc = FakeRpc()
        ok, err = self._builder(provider_ok(self.pubkey), rpc)(
            "buy", TEST_MINT, 0.001, 1_980_000, self._buy_quote())
        self.assertFalse(ok)
        self.assertEqual(err, "live_disabled")
        self.assertEqual(rpc.sent, [])

    def test_no_key_refuses(self):
        os.environ.pop("TRADING_PRIVATE_KEY", None)
        rpc = FakeRpc()
        ok, err = self._builder(provider_ok(self.pubkey), rpc)(
            "buy", TEST_MINT, 0.001, 1_980_000, self._buy_quote())
        self.assertFalse(ok)
        self.assertEqual(rpc.sent, [])

    def test_both_keys_refuses(self):
        os.environ["TRADING_KEYPAIR"] = "[1,2,3]"
        rpc = FakeRpc()
        ok, err = self._builder(provider_ok(self.pubkey), rpc)(
            "buy", TEST_MINT, 0.001, 1_980_000, self._buy_quote())
        self.assertFalse(ok)
        self.assertEqual(err, "both_keys_set")
        self.assertEqual(rpc.sent, [])

    def test_pubkey_mismatch_refuses(self):
        rpc = FakeRpc()
        ok, err = self._builder(provider_ok(self.pubkey), rpc,
                                expected=WALLET_PUBKEY)(
            "buy", TEST_MINT, 0.001, 1_980_000, self._buy_quote())
        self.assertFalse(ok)
        self.assertEqual(err, "pubkey_mismatch")
        self.assertEqual(rpc.sent, [])

    def test_stale_quote_refuses(self):
        rpc = FakeRpc()
        quote = make_quote(SOL_MINT, TEST_MINT, 1_000_000, 2_000_000,
                           fetched_at=time.time() - 120)
        ok, err = self._builder(provider_ok(self.pubkey), rpc)(
            "buy", TEST_MINT, 0.001, 1_980_000, quote)
        self.assertFalse(ok)
        self.assertEqual(err, "stale_quote")
        self.assertEqual(rpc.sent, [])

    def test_mint_mismatch_refuses(self):
        rpc = FakeRpc()
        quote = make_quote(SOL_MINT, "OtherMint111111111111111111111111111",
                           1_000_000, 2_000_000)
        ok, err = self._builder(provider_ok(self.pubkey), rpc)(
            "buy", TEST_MINT, 0.001, 1_980_000, quote)
        self.assertFalse(ok)
        self.assertEqual(err, "quote_mint_mismatch")
        self.assertEqual(rpc.sent, [])

    def test_min_output_below_guard_floor_refuses(self):
        # Guard 1%: floor = 0.99 * 2,000,000 = 1,980,000.
        # A caller passing 1,900,000 allows more slippage -> refuse.
        rpc = FakeRpc()
        ok, err = self._builder(provider_ok(self.pubkey), rpc)(
            "buy", TEST_MINT, 0.001, 1_900_000, self._buy_quote())
        self.assertFalse(ok)
        self.assertEqual(err, "slippage_guard_exceeded")
        self.assertEqual(rpc.sent, [])

    def test_expected_output_below_min_output_refuses(self):
        rpc = FakeRpc()
        ok, err = self._builder(
            provider_ok(self.pubkey, expected_output=1_000_000), rpc)(
            "buy", TEST_MINT, 0.001, 1_980_000, self._buy_quote())
        self.assertFalse(ok)
        self.assertEqual(err, "slippage_guard_exceeded")
        self.assertEqual(rpc.sent, [])

    def test_no_provider_refuses(self):
        rpc = FakeRpc()
        ok, err = self._builder(provider=None, rpc=rpc)(
            "buy", TEST_MINT, 0.001, 1_980_000, self._buy_quote())
        self.assertFalse(ok)
        self.assertEqual(err, "tx_builder_not_configured")
        self.assertEqual(rpc.sent, [])

    def test_provider_failure_refuses(self):
        def _fail(*args):
            return None, "route_unverified"
        rpc = FakeRpc()
        ok, err = self._builder(_fail, rpc)(
            "buy", TEST_MINT, 0.001, 1_980_000, self._buy_quote())
        self.assertFalse(ok)
        self.assertEqual(err, "route_unverified")
        self.assertEqual(rpc.sent, [])

    def test_fee_payer_mismatch_plan_refuses(self):
        other = base58_encode(b"\x04" * 32)
        rpc = FakeRpc()
        ok, err = self._builder(provider_ok(other), rpc)(
            "buy", TEST_MINT, 0.001, 1_980_000, self._buy_quote())
        self.assertFalse(ok)
        self.assertEqual(rpc.sent, [])

    def test_rpc_send_failure_refuses(self):
        rpc = FakeRpc(send_ok=False)
        ok, err = self._builder(provider_ok(self.pubkey), rpc)(
            "buy", TEST_MINT, 0.001, 1_980_000, self._buy_quote())
        self.assertFalse(ok)
        self.assertEqual(err, "tx_send_failed")

    def test_success_path_signs_and_sends_via_mocks(self):
        rpc = FakeRpc()
        builder = self._builder(provider_ok(self.pubkey), rpc)
        ok, err = builder("buy", TEST_MINT, 0.001, 1_980_000,
                          self._buy_quote())
        self.assertTrue(ok, err)
        self.assertIsNone(err)
        self.assertEqual(len(rpc.sent), 1)
        self.assertEqual(builder.last_signature,
                         "FakeSig111111111111111111111111111111111111")
        sigs, message = parse_serialized_tx(rpc.sent[0])
        self.assertTrue(ed25519_verify(
            base58_decode(self.pubkey), message, sigs[0]))
        parsed = parse_message(message)
        self.assertEqual(parsed["account_keys"][0], self.pubkey)

    def test_sell_direction_mints(self):
        rpc = FakeRpc()
        builder = self._builder(provider_ok(self.pubkey), rpc)
        quote = make_quote(TEST_MINT, SOL_MINT, 1000, 500_000)
        ok, err = builder("sell", TEST_MINT, 1000, 495_000, quote)
        self.assertTrue(ok, err)

    # --- Devnet quoter + route report ---
    def test_devnet_quoter_no_data_refuses(self):
        quoter = DevnetPoolQuoter(pools={}, network="devnet")
        self.assertIsNone(quoter.quote(SOL_MINT, TEST_MINT, 1_000_000))
        ok, reason = quoter.check_route(TEST_MINT, "buy")
        self.assertFalse(ok)
        self.assertEqual(reason, "route_unverified")

    def test_devnet_quoter_with_data(self):
        pools = {TEST_MINT: {
            "price_sol": 0.001, "liquidity_usd": 50000,
            "decimals": 6, "pool_address": "Pool111",
            "program_id": "Prog111", "source": "devnet_pool_verified",
            "verified_at": "2026-11-01"}}
        quoter = DevnetPoolQuoter(pools=pools, network="devnet")
        # Buy: 1,000,000 lamports (0.001 SOL) / 0.001 SOL-per-token
        # = 1 token = 1,000,000 smallest units.
        buy = quoter.quote(SOL_MINT, TEST_MINT, 1_000_000)
        self.assertIsNotNone(buy)
        self.assertAlmostEqual(buy.output_amount, 1_000_000)
        # Sell via executor wrapper: 2 token UNITS -> smallest,
        # then * price_sol * 1e9 lamports = 2,000,000 lamports.
        sell = quoter.quote_for_executor(TEST_MINT, SOL_MINT, 2)
        self.assertIsNotNone(sell)
        self.assertAlmostEqual(sell.output_amount, 2_000_000)
        ok, _reason = quoter.check_route(TEST_MINT, "buy")
        self.assertTrue(ok)

    def test_devnet_quoter_missing_decimals_refuses(self):
        pools = {TEST_MINT: {"price_sol": 0.001, "liquidity_usd": 1}}
        quoter = DevnetPoolQuoter(pools=pools, network="devnet")
        self.assertIsNone(quoter.quote(SOL_MINT, TEST_MINT, 1_000_000))

    def test_devnet_quoter_mainnet_network_refuses(self):
        pools = {TEST_MINT: {"price_sol": 0.001, "decimals": 6}}
        quoter = DevnetPoolQuoter(pools=pools, network="mainnet")
        self.assertIsNone(quoter.quote(SOL_MINT, TEST_MINT, 1_000_000))

    def test_devnet_route_report(self):
        report = devnet_route_report(pools={})
        self.assertFalse(report["route_verified"])
        self.assertEqual(report["reason"], "route_unverified")
        report2 = devnet_route_report(pools={TEST_MINT: {}})
        self.assertTrue(report2["route_verified"])
        self.assertIn(TEST_MINT, report2["mints"])

    # --- Watchlist env contract ---
    def test_watch_mints_from_env(self):
        os.environ["WATCH_MINTS"] = " AAA , ,BBB,"
        self.assertEqual(watch_mints_from_env(), ["AAA", "BBB"])
        os.environ.pop("WATCH_MINTS", None)
        self.assertEqual(watch_mints_from_env(), [])


if __name__ == "__main__":
    unittest.main()
