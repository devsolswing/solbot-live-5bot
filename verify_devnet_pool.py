"""Read-only devnet pool verifier (NOT part of the trading loop).

Usage (from this folder, devnet only, no key needed, no signing):
  python3 verify_devnet_pool.py --pool <POOL_ADDRESS>
  python3 verify_devnet_pool.py --pool <POOL_ADDRESS> --rpc https://api.devnet.solana.com

What it does: reads the pool account, its program account,
its two mints and its two vaults from devnet RPC, parses the
Whirlpool layout (see orca_devnet.py), and prints a JSON
report plus a DEVNET_POOLS_JSON entry template and a
WATCH_MINTS value for the pool's non-SOL mint.

What it does NOT do: it never signs, sends, or trades, never
reads a key, and never claims a trading route is proven. A
pool that passes this check proves QUOTE data only (a real
pool, a real spot price). A swap still needs a Whirlpool
swap-transaction provider, which does not exist in this
build, so trading swaps still refuse with route_unverified
until that provider is built and tested against a real
devnet pool with a throwaway wallet.

SOL-only rule: if the pool's non-SOL mint is a pegged test
mint, do NOT wire it as a trading mint — the live engine
is SOL-only and parks proceeds in SOL, never in a pegged
mint. Use a non-pegged test token for a trading proof, and
use a pegged test pool only as infrastructure evidence
(program exists, pool reads, spot price computes).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import SOL_MINT  # noqa: E402
from orca_devnet import (  # noqa: E402
    DEVNET_RPC_URL, WHIRLPOOL_PROGRAM, DevnetRpcReader,
    price_sol_per_token, read_pool, spot_price_token_units,
)


def build_report(pool_address: str, rpc_url: str = DEVNET_RPC_URL,
                 program_id: str = WHIRLPOOL_PROGRAM):
    rpc = DevnetRpcReader(rpc_url=rpc_url)
    program = rpc.get_account(program_id)
    pool, err = read_pool(rpc, pool_address, program_id)
    report = {
        "network": "devnet",
        "pool_address": pool_address,
        "program_id": program_id,
        "program_exists": program is not None,
        "program_executable": bool(program.get("executable")) if program else False,
        "program_owner": program.get("owner") if program else None,
        "quote_verified": False,
        "execution_verified": False,
        "execution_note": ("No Whirlpool swap-transaction provider "
                           "exists in this build. This check proves "
                           "quote data only; trading swaps still "
                           "refuse with route_unverified."),
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if pool is None:
        report["error"] = err
        return report
    report["pool_owner"] = pool["program_id"]
    report["pool_lamports"] = pool["lamports"]
    report["tick_spacing"] = pool["tick_spacing"]
    report["fee_rate"] = pool["fee_rate"]
    report["tick_current"] = pool["tick_current"]
    report["spot_raw"] = pool["spot_raw"]
    report["token_mint_a"] = pool["token_mint_a"]
    report["token_mint_b"] = pool["token_mint_b"]
    report["token_vault_a"] = pool["token_vault_a"]
    report["token_vault_b"] = pool["token_vault_b"]
    report["vault_a_balance"] = pool["vault_a_balance"]
    report["vault_b_balance"] = pool["vault_b_balance"]
    report["decimals_a"] = pool["decimals_a"]
    report["decimals_b"] = pool["decimals_b"]
    is_sol_pair = SOL_MINT in (pool["token_mint_a"], pool["token_mint_b"])
    report["is_sol_pair"] = is_sol_pair
    if is_sol_pair and pool["spot_raw"] > 0:
        report["quote_verified"] = True
        spot = spot_price_token_units(pool, pool["decimals_a"],
                                      pool["decimals_b"]) if (
            pool["decimals_a"] is not None
            and pool["decimals_b"] is not None) else None
        report["spot_token_b_per_token_a"] = spot
        non_sol = (pool["token_mint_b"] if pool["token_mint_a"] == SOL_MINT
                   else pool["token_mint_a"])
        report["non_sol_mint"] = non_sol
        report["watch_mints"] = non_sol
        if pool["decimals_a"] is not None and pool["decimals_b"] is not None:
            price_sol = price_sol_per_token(
                pool, non_sol, pool["decimals_a"], pool["decimals_b"])
            report["price_sol_per_token"] = price_sol
            if price_sol is not None and price_sol > 0:
                report["devnet_pools_json_entry"] = {
                    non_sol: {
                        "price_sol": price_sol,
                        "decimals": (pool["decimals_b"]
                                     if non_sol == pool["token_mint_b"]
                                     else pool["decimals_a"]),
                        "pool_address": pool_address,
                        "program_id": program_id,
                        "source": "orca_whirlpool_devnet_verified_pool",
                        "verified_at": report["checked_at"],
                    }
                }
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", required=True,
                        help="Whirlpool pool address on devnet")
    parser.add_argument("--rpc", default=DEVNET_RPC_URL,
                        help="Devnet RPC URL (default: public devnet RPC)")
    parser.add_argument("--program", default=WHIRLPOOL_PROGRAM,
                        help="Whirlpool program ID (default: devnet program)")
    args = parser.parse_args(argv)
    if args.rpc != DEVNET_RPC_URL and "devnet" not in args.rpc:
        print("REFUSED: this verifier is devnet-only; refusing a "
              "non-devnet RPC URL.", file=sys.stderr)
        return 2
    report = build_report(args.pool, args.rpc, args.program)
    print(json.dumps(report, indent=2, default=str))
    if report.get("quote_verified"):
        print("\nQUOTE VERIFIED for this pool (price data only).")
        print("EXECUTION NOT VERIFIED: no swap provider exists yet.")
    else:
        print("\nQUOTE NOT VERIFIED for this pool; do not use it.")
    return 0 if report.get("quote_verified") else 1


if __name__ == "__main__":
    raise SystemExit(main())
