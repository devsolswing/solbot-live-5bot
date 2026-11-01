"""Read-only dry run for the DISABLED live 5-bot build.

Reads ONLY public data:
  * SOL/USDC price from DexScreener (public API, no key)
  * USD->CAD rate from exchangerate-api (public, no key)
  * A Jupiter v6 swap quote (public quote endpoint, no signing)

It NEVER signs, sends, funds, or starts a loop. Any failed fetch is
reported plainly; nothing is invented to fill a gap.
"""

import json
import sys
import urllib.request

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def fetch_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "solbot-live-dryrun"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    results = {}
    # 1. SOL price (DexScreener token endpoint).
    try:
        data = fetch_json(
            f"https://api.dexscreener.com/latest/dex/tokens/{SOL_MINT}")
        pairs = data.get("pairs") or []
        sol_pairs = [p for p in pairs if p.get("quoteToken", {}).get("symbol") == "USDC"]
        price = sol_pairs[0]["priceUsd"] if sol_pairs else None
        results["sol_price_usd"] = price
        print(f"SOL price (DexScreener, USDC pair): {price}")
    except Exception as exc:  # noqa: BLE001 - report, don't fake
        results["sol_price_usd"] = None
        print(f"SOL price: UNAVAILABLE ({exc})")

    # 2. CAD rate (USD base).
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

    # 3. Jupiter quote: 1 USDC -> SOL (read-only quote, no swap).
    try:
        data = fetch_json(
            "https://quote-api.jup.ag/v6/quote"
            f"?inputMint={USDC_MINT}&outputMint={SOL_MINT}&amount=1000000"
            "&slippageBps=50")
        results["jupiter_quote"] = {
            "in_amount": data.get("inAmount"),
            "out_amount": data.get("outAmount"),
            "price_impact_pct": data.get("priceImpactPct"),
        }
        print(f"Jupiter quote (1 USDC -> SOL, read-only): {results['jupiter_quote']}")
    except Exception as exc:  # noqa: BLE001
        results["jupiter_quote"] = None
        print(f"Jupiter quote: UNAVAILABLE ({exc})")

    # 4. Halt arithmetic check (no chain data needed).
    usd_per_cad = results["usd_per_cad"] or 0.712
    start_usd = 85.0 * usd_per_cad
    halt_usd = 20.0 * usd_per_cad
    print(f"Halt arithmetic at this rate: $85 CAD = ${start_usd:.2f} USD; "
          f"$20 CAD halt = ${halt_usd:.2f} USD")
    print("DRY RUN COMPLETE — no transaction signed, sent, or funded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
