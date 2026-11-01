"""Read-only dry run for the DISABLED live 5-bot build.

Reads ONLY public data. Network is selected by the NETWORK environment
variable (devnet | mainnet, unset = mainnet — see engine.py). It NEVER
signs, sends, funds, or starts a loop. Any failed fetch is reported
plainly; nothing is invented to fill a gap.

Devnet note: the devnet USDC mint is Circle's Solana Devnet USDC
(4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU, verified via Circle
docs and a live devnet getAccountInfo check). Devnet swap routing is
UNVERIFIED in this sandbox: a Jupiter v6 quote for devnet USDC -> SOL
returned no route and DexScreener returned no pairs for that mint.
A devnet live test must prove a route in the deployment environment
and fail closed if none exists.
"""

import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import get_network_config, WALLET_PUBKEY  # noqa: E402

MAINNET_SOL_MINT = "So11111111111111111111111111111111111111112"


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
    print(f"USDC mint: {net.usdc_mint}")
    print(f"Wallet: {WALLET_PUBKEY}")
    print(f"Routing: {net.routing_note}")
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

    # 2. SOL price — only meaningful on mainnet tracked pairs. On devnet
    # DexScreener has no pairs for devnet USDC, which is reported as-is.
    try:
        data = fetch_json(
            f"https://api.dexscreener.com/latest/dex/tokens/{net.sol_mint}")
        pairs = data.get("pairs") or []
        sol_pairs = [p for p in pairs
                     if p.get("quoteToken", {}).get("symbol") == "USDC"]
        price = sol_pairs[0]["priceUsd"] if sol_pairs else None
        results["sol_price_usd"] = price
        print(f"SOL price (DexScreener, USDC pair): {price}")
        if net.name == "devnet" and price is None:
            print("  (Expected on devnet: DexScreener tracks mainnet pairs; "
                  "no devnet USDC pair price is a routing gap, not a price.)")
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

    # 4. Quote: 1 unit of trading USDC -> SOL (read-only quote, no swap).
    # Mainnet: Jupiter v6 (execution still unverified). Devnet: the same
    # endpoint is attempted and its result reported plainly; a missing
    # route is an unverified-routing result, NOT a verified price.
    try:
        data = fetch_json(
            "https://quote-api.jup.ag/v6/quote"
            f"?inputMint={net.usdc_mint}&outputMint={net.sol_mint}"
            "&amount=1000000&slippageBps=50")
        results["jupiter_quote"] = {
            "in_amount": data.get("inAmount"),
            "out_amount": data.get("outAmount"),
            "price_impact_pct": data.get("priceImpactPct"),
        }
        print(f"Jupiter quote (1 USDC -> SOL, read-only, {net.name}): "
              f"{results['jupiter_quote']}")
        if net.name == "devnet" and not results["jupiter_quote"]["out_amount"]:
            print("  DEVNET ROUTING UNVERIFIED: quote returned no route. "
                  "Do not trade on devnet until a route is proven in the "
                  "deployment environment.")
    except Exception as exc:  # noqa: BLE001
        results["jupiter_quote"] = None
        print(f"Jupiter quote ({net.name}): UNAVAILABLE ({exc}) — "
              "routing unverified, fail closed")

    # 5. Halt arithmetic check (no chain data needed). Same CAD numbers
    # in both modes (devnet: counted in devnet USDC at the peg — see
    # RULES.md §11; this is an assumption for James to correct).
    usd_per_cad = results["usd_per_cad"] or 0.712
    start_usd = 85.0 * usd_per_cad
    halt_usd = 20.0 * usd_per_cad
    peg_note = ("devnet USDC at the peg for devnet" if net.name == "devnet"
                else "mainnet USDC")
    print(f"Halt arithmetic at this rate: $85 CAD = ${start_usd:.2f} "
          f"trading-currency units; $20 CAD halt = ${halt_usd:.2f} units "
          f"({peg_note})")
    print("DRY RUN COMPLETE — no transaction signed, sent, or funded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
