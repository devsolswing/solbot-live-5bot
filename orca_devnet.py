"""Orca Whirlpool devnet pool reader and quoter — QUOTE ONLY.

This module reads a Whirlpool pool account from devnet RPC and
computes its spot price. It does NOT build a swap transaction,
does NOT sign, and does NOT send anything. A price quote from
this module proves price discovery on devnet; it does NOT prove
a swap route for trading, because no Whirlpool swap-transaction
provider has been built or tested on devnet yet. Do NOT wire
this quoter into the loop's trading route checker until such a
provider exists and has been tested against a real devnet pool
with a throwaway wallet. Until then, trading swaps on devnet
still refuse with route_unverified, by design.

CORRECTION, verified live on devnet RPC 2026-11-02 (this
supersedes the 2026-11-01 claim in tx_builder.py that the
Orca Whirlpool program had no account on devnet — that claim
is WRONG as of this check):
* Whirlpool program whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc
  IS an executable program on devnet (owner BPFLoaderUpgrade,
  executable=True).
* The devnet WhirlpoolsConfig account exists and is owned by
  that program.
* A documented devnet test pool exists on devnet, is owned by
  that program, and is funded (its SOL vault and its second
  vault both hold balances — see the verification tool).
* The Jupiter aggregator program address returns NO account on
  devnet, so Jupiter-built swap transactions cannot execute
  there. Jupiter is a mainnet route source only.
* Raydium devnet programs (CPMM, CLMM, AMM v4) also exist as
  executable programs on devnet with their devnet config
  accounts present — but no Raydium devnet pool was verified
  here, and no Raydium swap provider is built here either.
* Orca and Raydium pool-list APIs return mainnet data only
  (chainId 101). They must NEVER be used to price a devnet
  swap. Only on-chain devnet RPC reads are used here.

Whirlpool account layout used here (verified byte-for-byte
against the live devnet test pool on 2026-11-02):
  0-8    discriminator
  8-40   whirlpoolsConfig (32 bytes)
  40     whirlpoolBump (u8)
  41-43  tickSpacing (u16, little-endian)
  43-45  tickSpacingSeed (2 bytes)
  45-47  feeRate (u16)
  47-49  protocolFeeRate (u16)
  49-65  liquidity (u128)
  65-81  sqrtPrice, Q64.96 fixed point (u128)
  81-85  tickCurrentIndex (i32)
  85-93  protocolFeeOwedA (u64)
  93-101 protocolFeeOwedB (u64)
  101-133 tokenMintA (32 bytes)
  133-165 tokenVaultA (32 bytes)
  165-181 feeGrowthGlobalA (u128)
  181-213 tokenMintB (32 bytes)
  213-245 tokenVaultB (32 bytes)
  245-261 feeGrowthGlobalB (u128)
  261-269 rewardLastUpdatedTimestamp (u64)
  (total 653 bytes; reward infos follow)

Spot price: sqrtPrice is sqrt(price) in Q64.96, where price is
(tokenB smallest units) / (tokenA smallest units) at the
current tick. So:
  spot_raw = (sqrtPrice / 2**64) ** 2
  spot_B_per_A (token units) = spot_raw * 10**decA / 10**decB
  output_smallest for an A->B swap = input_smallest * spot_raw
  output_smallest for a B->A swap = input_smallest / spot_raw
The decimals cancel for the smallest-unit quote, which is
what the engine quotes in. Decimals are still fetched for
the price-in-SOL report used by the verification tool.

This module is stdlib only, read-only, and never touches a
key. Tests use mocked RPC data only.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import SOL_MINT, base58_encode  # noqa: E402
from swap import Quote  # noqa: E402

WHIRLPOOL_PROGRAM = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
WHIRLPOOL_DEVNET_CONFIG = "FcrweFY1G9HJAHG5inkGB6pKg1HZ6x9UC2WioAfWrGkR"
DEVNET_RPC_URL = "https://api.devnet.solana.com"
POOL_ACCOUNT_MIN_LEN = 261

ERR_NO_POOL = "no_devnet_pool"
ERR_WRONG_OWNER = "pool_wrong_owner"
ERR_SHORT_DATA = "pool_data_too_short"
ERR_NO_SOL_PAIR = "pool_not_sol_pair"
ERR_BAD_PRICE = "pool_bad_price"
ERR_RPC_FAILED = "rpc_failed"


def parse_whirlpool(data: bytes):
    """Parse Whirlpool account bytes. Returns a dict, or None.

    Returns None on short data. Does not verify the owner —
    callers must check the account's owner against
    WHIRLPOOL_PROGRAM themselves (see read_pool).
    """
    if data is None or len(data) < POOL_ACCOUNT_MIN_LEN:
        return None
    sqrt_u128 = int.from_bytes(data[65:81], "little")
    sqrt_float = sqrt_u128 / (2 ** 64)
    spot_raw = sqrt_float * sqrt_float
    return {
        "whirlpools_config": base58_encode(data[8:40]),
        "whirlpool_bump": data[40],
        "tick_spacing": struct.unpack_from("<H", data, 41)[0],
        "tick_spacing_seed": list(data[43:45]),
        "fee_rate": struct.unpack_from("<H", data, 45)[0],
        "protocol_fee_rate": struct.unpack_from("<H", data, 47)[0],
        "liquidity": int.from_bytes(data[49:65], "little"),
        "sqrt_price_u128": sqrt_u128,
        "sqrt_price_float": sqrt_float,
        "tick_current": struct.unpack_from("<i", data, 81)[0],
        "protocol_fee_owed_a": struct.unpack_from("<Q", data, 85)[0],
        "protocol_fee_owed_b": struct.unpack_from("<Q", data, 93)[0],
        "token_mint_a": base58_encode(data[101:133]),
        "token_vault_a": base58_encode(data[133:165]),
        "fee_growth_global_a": int.from_bytes(data[165:181], "little"),
        "token_mint_b": base58_encode(data[181:213]),
        "token_vault_b": base58_encode(data[213:245]),
        "fee_growth_global_b": int.from_bytes(data[245:261], "little"),
        "spot_raw": spot_raw,
    }


def spot_price_token_units(pool: dict, decimals_a: int, decimals_b: int):
    """TokenB per tokenA, in token units. None if unusable."""
    if pool is None or pool.get("spot_raw", 0) <= 0:
        return None
    try:
        return pool["spot_raw"] * (10 ** int(decimals_a)) / (10 ** int(decimals_b))
    except (TypeError, ValueError, OverflowError):
        return None


def price_sol_per_token(pool: dict, token_mint: str,
                        decimals_a: int, decimals_b: int):
    """SOL per one token unit, for the pool's non-SOL mint.

    Returns None if the pool is not a SOL pair or the token is
    not one of the pool's mints. This is the price_sol value a
    DEVNET_POOLS_JSON entry needs for that mint.
    """
    if pool is None:
        return None
    if token_mint == pool["token_mint_a"] and pool["token_mint_b"] == SOL_MINT:
        # token is A, SOL is B: spot_B_per_A IS SOL per token.
        spot = spot_price_token_units(pool, decimals_a, decimals_b)
        if spot is None or spot <= 0:
            return None
        return spot
    if token_mint == pool["token_mint_b"] and pool["token_mint_a"] == SOL_MINT:
        # token is B, SOL is A: SOL per token = 1 / spot_B_per_A
        # where spot_B_per_A is token per SOL — same formula.
        spot = spot_price_token_units(pool, decimals_a, decimals_b)
        if spot is None or spot <= 0:
            return None
        return 1.0 / spot
    return None


class DevnetRpcReader:
    """Minimal devnet RPC reader (read-only). http_post, if
    supplied, must be (url, payload_dict) -> response_dict
    (tests inject it; production uses urllib)."""

    def __init__(self, rpc_url: str = DEVNET_RPC_URL, http_post=None,
                 timeout=30):
        self.rpc_url = rpc_url
        self.http_post = http_post or self._default_post
        self.timeout = timeout

    def _default_post(self, url, payload):
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "User-Agent": "solbot-live-orca-devnet"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _call(self, method, params):
        try:
            resp = self.http_post(
                self.rpc_url,
                {"jsonrpc": "2.0", "id": 1, "method": method,
                 "params": params})
        except Exception:  # noqa: BLE001 - RPC failure = no quote
            return None
        if not isinstance(resp, dict) or "error" in resp:
            return None
        return resp.get("result")

    def get_account(self, address: str):
        """Returns (value_dict_or_None). value has owner,
        executable, lamports, data (base64 list)."""
        result = self._call(
            "getAccountInfo", [address, {"encoding": "base64"}])
        if not result:
            return None
        return result.get("value")

    def get_token_balance(self, address: str):
        """Returns (amount_str, decimals, ui_amount) or None."""
        result = self._call("getTokenAccountBalance", [address])
        if not result:
            return None
        value = result.get("value")
        if not value:
            return None
        return (value.get("amount"), value.get("decimals"),
                value.get("uiAmount"))

    def get_mint_decimals(self, mint: str):
        """Returns decimals (int) or None, via parsed mint info."""
        result = self._call(
            "getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        if not result:
            return None
        value = result.get("value")
        if not value:
            return None
        try:
            return int(value["data"]["parsed"]["info"]["decimals"])
        except (KeyError, TypeError, ValueError):
            return None


def read_pool(rpc: DevnetRpcReader, pool_address: str,
              program_id: str = WHIRLPOOL_PROGRAM):
    """Read + parse a devnet Whirlpool pool. Returns
    (pool_dict_or_None, error_code_or_None). The pool dict
    includes vault balances and mint decimals when RPC
    returns them; missing vault/decimals data is reported
    in the dict, never guessed."""
    account = rpc.get_account(pool_address)
    if account is None:
        return None, ERR_NO_POOL
    if account.get("owner") != program_id:
        return None, ERR_WRONG_OWNER
    raw = account.get("data")
    if not isinstance(raw, list) or not raw:
        return None, ERR_SHORT_DATA
    try:
        data = base64.b64decode(raw[0])
    except Exception:  # noqa: BLE001 - bad data = no quote
        return None, ERR_SHORT_DATA
    pool = parse_whirlpool(data)
    if pool is None:
        return None, ERR_SHORT_DATA
    pool["pool_address"] = pool_address
    pool["program_id"] = program_id
    pool["lamports"] = account.get("lamports")
    pool["executable"] = account.get("executable")
    # Vault balances (read-only, for cross-check only — the
    # spot price comes from sqrtPrice, not the vault ratio,
    # because a concentrated pool's vault ratio is not its
    # spot price).
    pool["vault_a_balance"] = rpc.get_token_balance(pool["token_vault_a"])
    pool["vault_b_balance"] = rpc.get_token_balance(pool["token_vault_b"])
    pool["decimals_a"] = rpc.get_mint_decimals(pool["token_mint_a"])
    pool["decimals_b"] = rpc.get_mint_decimals(pool["token_mint_b"])
    return pool, None


class OrcaWhirlpoolQuoter:
    """Quote-only quoter for one devnet Whirlpool pool.

    quoter(input_mint, output_mint, input_amount_smallest)
    -> Quote or None. The quote's source is
    "orca_whirlpool_devnet_quote_only_no_execution" — it must
    NOT be treated as a proven trading route. A swap still
    needs a Whirlpool swap-transaction provider, which does
    not exist in this build, so any swap attempted from this
    quote alone must refuse at the provider stage.
    """

    def __init__(self, rpc: DevnetRpcReader, pool_address: str,
                 program_id: str = WHIRLPOOL_PROGRAM):
        self.rpc = rpc
        self.pool_address = pool_address
        self.program_id = program_id

    def quote(self, input_mint, output_mint, input_amount_smallest):
        try:
            amount = float(input_amount_smallest)
        except (TypeError, ValueError):
            return None
        if amount <= 0:
            return None
        pool, err = read_pool(self.rpc, self.pool_address, self.program_id)
        if pool is None or err:
            return None
        if pool.get("spot_raw", 0) <= 0:
            return None
        if input_mint == pool["token_mint_a"] and output_mint == pool["token_mint_b"]:
            output_smallest = amount * pool["spot_raw"]
        elif input_mint == pool["token_mint_b"] and output_mint == pool["token_mint_a"]:
            output_smallest = amount / pool["spot_raw"]
        else:
            return None
        if output_smallest <= 0:
            return None
        return Quote(input_mint=input_mint, output_mint=output_mint,
                     input_amount=amount, output_amount=output_smallest,
                     source="orca_whirlpool_devnet_quote_only_no_execution",
                     raw={"pool_address": self.pool_address,
                          "program_id": self.program_id,
                          "tick_current": pool["tick_current"],
                          "spot_raw": pool["spot_raw"],
                          "token_mint_a": pool["token_mint_a"],
                          "token_mint_b": pool["token_mint_b"]})

    def quote_status(self):
        """Read-only status: quote_verified is True only if
        the pool reads, parses, and has a positive spot price
        and a SOL pair. execution_verified is ALWAYS False in
        this build — no swap provider exists yet."""
        pool, err = read_pool(self.rpc, self.pool_address, self.program_id)
        if pool is None:
            return {"quote_verified": False, "execution_verified": False,
                    "reason": err or ERR_NO_POOL,
                    "pool_address": self.pool_address}
        is_sol_pair = SOL_MINT in (pool["token_mint_a"], pool["token_mint_b"])
        ok = bool(is_sol_pair and pool.get("spot_raw", 0) > 0)
        return {"quote_verified": ok,
                "execution_verified": False,
                "reason": ("quote_only_no_execution"
                           if ok else ERR_NO_SOL_PAIR),
                "pool_address": self.pool_address,
                "token_mint_a": pool["token_mint_a"],
                "token_mint_b": pool["token_mint_b"],
                "tick_current": pool["tick_current"],
                "spot_raw": pool["spot_raw"]}
