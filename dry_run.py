"""Read-only dry run for the DISABLED live 5-bot build (SOL-only).

Reads ONLY public data. Network is selected by the NETWORK environment
variable (devnet | mainnet, unset = mainnet — see engine.py). It NEVER
signs, sends, funds, or starts a loop. Any failed fetch is reported
plainly; nothing is invented to fill a gap.

SOL-only since 2026-10-31 (James's SOL-only order): the bankroll is SOL, buys are SOL -> token, sells are token ->
SOL, and the halt is computed from the SOL portfolio via a live SOL
price in USD. If the SOL price is unavailable, the halt arithmetic in
SOL is NOT computed — a missing price must never be replaced by a
hard-coded SOL amount.

Swap routing is UNVERIFIED in this sandbox on both networks: a Jupiter
v6 quote returned no route for a devnet swap, and Jupiter's current
Swap/Ultra APIs are mainnet-focused. A live test must prove a SOL swap
route in the deployment environment and fail closed if none exists.
"""

import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import get_network_config, WALLET_PUBKEY  # noqa: E402


def fetch_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "solbot-live-dryrun"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def rpc_call(rpc_url, method, params, timeout=20):
    req = urllib.request.Request(
        rpc_url,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                         "params": params}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "User-Agent": "solbot-live-dryrun"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    try:
        net = get_network_config()
    except ValueError as exc:
        print(f"NETWORK: INVALID ({exc}) — refusing to proceed, fail closed")
        return 2
    print(f"Network (NETWORK env): {net.name}")
    print(f"RPC: {net.rpc_url}")
    print(f"SOL mint: {net.sol_mint}")
    print(f"Wallet: {WALLET_PUBKEY}")
    print(f"Routing: {net.routing_note}")
    print("Currency: SOL ONLY — buys SOL -> token, sells token -> SOL, "
          "proceeds park in SOL. No other-currency is used anywhere.")
    results = {}

    # 1. Wallet SOL balance on the selected network (read-only).
    try:
        data = rpc_call(net.rpc_url, "getBalance", [WALLET_PUBKEY])
        lamports = data["result"]["value"]
        print(f"Wallet SOL balance ({net.name}): {lamports / 1e9} SOL "
              f"({lamports} lamports)")
        results["wallet_sol"] = lamports / 1e9
    except Exception as exc:  # noqa: BLE001 - report, don't fake
        results["wallet_sol"] = None
        print(f"Wallet SOL balance: UNAVAILABLE ({exc})")

    # 2. SOL price in USD (DexScreener, first tracked SOL pair's USD
    # price — used only as a USD price source, not as a trading pair).
    sol_price_usd = None
    try:
        data = fetch_json(
            f"https://api.dexscreener.com/latest/dex/tokens/{net.sol_mint}")
        pairs = data.get("pairs") or []
        if pairs and pairs[0].get("priceUsd"):
            sol_price_usd = float(pairs[0]["priceUsd"])
        results["sol_price_usd"] = sol_price_usd
        print(f"SOL price (DexScreener, USD): {sol_price_usd}")
    except Exception as exc:  # noqa: BLE001
        results["sol_price_usd"] = None
        print(f"SOL price: UNAVAILABLE ({exc})")

    # 3. CAD rate (USD base).
    try:
        data = fetch_json("https://api.exchangerate-api.com/v4/latest/USD")
        cad_per_usd = data["rates"]["CAD"]
        usd_per_cad = 1.0 / cad_per_usd
        results["cad_per_usd"] = cad_per_usd
        results["usd_per_cad"] = usd_per_cad
        print(f"CAD rate (exchangerate-api): 1 USD = {cad_per_usd} CAD "
              f"({usd_per_cad:.4f} USD per CAD) — approximate, see RULES.md")
    except Exception as exc:  # noqa: BLE001
        results["cad_per_usd"] = None
        results["usd_per_cad"] = None
        print(f"CAD rate: UNAVAILABLE ({exc}); fallback 0.712 USD per CAD")

    # 4. Halt arithmetic in SOL (conversion method only — no SOL amount
    # is hard-coded as James's number). Requires a live SOL price;
    # without one, this step is skipped, fail-closed.
    usd_per_cad = results["usd_per_cad"] or 0.712
    if sol_price_usd:
        start_sol = 85.0 * usd_per_cad / sol_price_usd
        halt_sol = 20.0 * usd_per_cad / sol_price_usd
        print(f"Halt arithmetic at this SOL price: $85 CAD = {start_sol:.4f} "
              f"SOL; $20 CAD halt = {halt_sol:.4f} SOL (at "
              f"${sol_price_usd:.2f}/SOL and {usd_per_cad:.4f} USD per CAD; "
              "both rates move, so these SOL figures move with them)")
    else:
        print("Halt arithmetic in SOL: SKIPPED — SOL price unavailable, "
              "fail closed (no SOL amount invented)")

    # 5. Routing: no generic SOL swap quote is attempted here (there is
    # no single token to quote against). Routing is unverified in this
    # sandbox and must be proven in the deployment environment.
    print("SOL swap routing: UNVERIFIED in this sandbox — deployment must "
          "prove a SOL swap route (Orca/Raydium pool, direct DEX "
          "instruction, or a test token with a real pool) before any "
          "live test, and fail closed if none exists.")
    print("DRY RUN COMPLETE — no transaction signed, sent, or funded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
