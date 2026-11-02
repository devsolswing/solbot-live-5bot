"""Tests for orca_devnet.py — mocked RPC only, never a real key,
never a real chain call. The synthetic pool uses fictional
mints (not any real pool's mints) so no real pool data is
hardcoded into the tests."""

import base64
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import SOL_MINT, base58_decode, base58_encode  # noqa: E402
from orca_devnet import (  # noqa: E402
    WHIRLPOOL_PROGRAM, DevnetRpcReader, OrcaWhirlpoolQuoter,
    parse_whirlpool, price_sol_per_token, read_pool,
    spot_price_token_units,
)

TEST_MINT = base58_encode(b"\x07" * 32)
TEST_VAULT_A = base58_encode(b"\x08" * 32)
TEST_VAULT_B = base58_encode(b"\x09" * 32)


def make_pool_bytes(sqrt_price_float=0.5, tick=-10000):
    """Build a 653-byte Whirlpool account in the verified layout."""
    data = bytearray(653)
    data[8:40] = base58_decode("FcrweFY1G9HJAHG5inkGB6pKg1HZ6x9UC2WioAfWrGkR")
    data[40] = 255
    struct.pack_into("<H", data, 41, 64)
    data[43:45] = bytes([64, 0])
    struct.pack_into("<H", data, 45, 2000)
    struct.pack_into("<H", data, 47, 300)
    struct.pack_into("<Q", data, 49, 76581459811)
    struct.pack_into("<Q", data, 49 + 8, 0)
    sqrt_u128 = int(sqrt_price_float * (2 ** 64))
    data[65:81] = sqrt_u128.to_bytes(16, "little")
    struct.pack_into("<i", data, 81, tick)
    struct.pack_into("<Q", data, 85, 100)
    struct.pack_into("<Q", data, 93, 200)
    data[101:133] = base58_decode(SOL_MINT)
    data[133:165] = base58_decode(TEST_VAULT_A)
    struct.pack_into("<Q", data, 165, 0)
    struct.pack_into("<Q", data, 173, 0)
    data[181:213] = base58_decode(TEST_MINT)
    data[213:245] = base58_decode(TEST_VAULT_B)
    struct.pack_into("<Q", data, 245, 0)
    struct.pack_into("<Q", data, 253, 0)
    struct.pack_into("<Q", data, 261, 1700000000)
    return bytes(data)


class FakeRpc:
    """Mock RPC reader: pool account, mint decimals, vault balances."""

    def __init__(self, pool_bytes=None, owner=WHIRLPOOL_PROGRAM):
        self.pool_bytes = pool_bytes if pool_bytes is not None else make_pool_bytes()
        self.owner = owner

    def get_account(self, address):
        if address == WHIRLPOOL_PROGRAM:
            return {"owner": "BPFLoaderUpgradeab1e11111111111111111111111",
                    "executable": True, "lamports": 2141440, "data": ["", "base64"]}
        return {"owner": self.owner, "executable": False,
                "lamports": 106435760,
                "data": [base64.b64encode(self.pool_bytes).decode(), "base64"]}

    def get_token_balance(self, address):
        if address == TEST_VAULT_A:
            return ("398279272797", 9, 398.279272797)
        if address == TEST_VAULT_B:
            return ("6353911374", 6, 6353.911374)
        return None

    def get_mint_decimals(self, mint):
        if mint == SOL_MINT:
            return 9
        if mint == TEST_MINT:
            return 6
        return None


class TestParseWhirlpool(unittest.TestCase):
    def test_layout_fields(self):
        pool = parse_whirlpool(make_pool_bytes(sqrt_price_float=0.5, tick=-10000))
        self.assertEqual(pool["tick_spacing"], 64)
        self.assertEqual(pool["tick_spacing_seed"], [64, 0])
        self.assertEqual(pool["fee_rate"], 2000)
        self.assertEqual(pool["tick_current"], -10000)
        self.assertEqual(pool["token_mint_a"], SOL_MINT)
        self.assertEqual(pool["token_mint_b"], TEST_MINT)
        self.assertEqual(pool["token_vault_a"], TEST_VAULT_A)
        self.assertEqual(pool["token_vault_b"], TEST_VAULT_B)
        self.assertAlmostEqual(pool["spot_raw"], 0.25, places=6)

    def test_short_data_returns_none(self):
        self.assertIsNone(parse_whirlpool(b"\x00" * 100))
        self.assertIsNone(parse_whirlpool(None))

    def test_spot_price_token_units(self):
        pool = parse_whirlpool(make_pool_bytes(sqrt_price_float=0.5))
        # spot_raw 0.25, decA 9, decB 6 -> 0.25 * 1000 = 250 token per SOL
        self.assertAlmostEqual(spot_price_token_units(pool, 9, 6), 250.0)
        # SOL per token = 1 / 250
        self.assertAlmostEqual(price_sol_per_token(pool, TEST_MINT, 9, 6), 1.0 / 250.0)
        self.assertIsNone(price_sol_per_token(pool, SOL_MINT, 9, 6))
        self.assertIsNone(price_sol_per_token(pool, "nope", 9, 6))


class TestReadPool(unittest.TestCase):
    def test_read_ok(self):
        rpc = FakeRpc()
        pool, err = read_pool(rpc, "pool-address-here")
        self.assertIsNone(err)
        self.assertEqual(pool["token_mint_a"], SOL_MINT)
        self.assertEqual(pool["decimals_a"], 9)
        self.assertEqual(pool["decimals_b"], 6)
        self.assertEqual(pool["vault_a_balance"][2], 398.279272797)

    def test_wrong_owner_refused(self):
        rpc = FakeRpc(owner="SomeOtherProgram11111111111111111111111111")
        pool, err = read_pool(rpc, "pool-address-here")
        self.assertIsNone(pool)
        self.assertEqual(err, "pool_wrong_owner")


class TestQuoter(unittest.TestCase):
    def test_buy_sol_to_token(self):
        quoter = OrcaWhirlpoolQuoter(FakeRpc(), "pool-address-here")
        # sqrt 0.5 -> spot_raw 0.25; 1 SOL = 1e9 lamports -> 0.25e9 token smallest
        quote = quoter.quote(SOL_MINT, TEST_MINT, 1_000_000_000)
        self.assertIsNotNone(quote)
        self.assertAlmostEqual(quote.output_amount, 250_000_000.0)
        self.assertTrue(quote.is_fresh())
        self.assertIn("quote_only", quote.source)

    def test_sell_token_to_sol(self):
        quoter = OrcaWhirlpoolQuoter(FakeRpc(), "pool-address-here")
        # 250 token smallest (decimals 6 -> 250e6) -> 1e9 lamports at spot_raw 0.25
        quote = quoter.quote(TEST_MINT, SOL_MINT, 250_000_000)
        self.assertIsNotNone(quote)
        self.assertAlmostEqual(quote.output_amount, 1_000_000_000.0)

    def test_wrong_pair_refused(self):
        quoter = OrcaWhirlpoolQuoter(FakeRpc(), "pool-address-here")
        self.assertIsNone(quoter.quote(TEST_MINT, TEST_MINT, 1000))
        self.assertIsNone(quoter.quote(SOL_MINT, SOL_MINT, 1000))

    def test_zero_price_refused(self):
        quoter = OrcaWhirlpoolQuoter(
            FakeRpc(pool_bytes=make_pool_bytes(sqrt_price_float=0.0)),
            "pool-address-here")
        self.assertIsNone(quoter.quote(SOL_MINT, TEST_MINT, 1000))

    def test_quote_status_execution_always_false(self):
        quoter = OrcaWhirlpoolQuoter(FakeRpc(), "pool-address-here")
        status = quoter.quote_status()
        self.assertTrue(status["quote_verified"])
        self.assertFalse(status["execution_verified"])
        self.assertEqual(status["reason"], "quote_only_no_execution")


if __name__ == "__main__":
    unittest.main()
