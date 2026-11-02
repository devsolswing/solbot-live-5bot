# Devnet SOL swap route — exact next action for James

Status: QUOTE data on devnet is now proven for a real
Whirlpool pool (see RULES.md §16). A TRADING route on
devnet is NOT proven yet, because no Whirlpool/Raydium
swap-transaction provider has been built or tested on
devnet. This plan creates the one thing that is still
missing on the pool side: a devnet pool for a NON-PEGGED
test token that the engine may actually trade in a proof.
After the pool exists, a swap provider still has to be
built and tested against it (with a throwaway wallet
first) before any trading gate can pass.

## Hard rules for this plan

- Devnet ONLY. No real money is involved at any step:
  devnet SOL is worthless test SOL from the faucet.
- Use a THROWAWAY devnet wallet you create for this
  plan only. NEVER use the trading wallet, NEVER paste
  the trading key anywhere, and NEVER enter any seed
  phrase into any website or script.
- Do NOT set `LIVE_ENABLED=1`, do NOT add
  `--tx-builder solana`, and do NOT trade with the
  engine until the swap provider is built/tested and a
  deployment smoke test shows a proven route.
- If any step asks for payment in real money, STOP.
- If a program ID shown to you does not match the
  verified devnet IDs in this plan, do NOT sign.

## Verified devnet program IDs (checked on devnet RPC)

- Orca Whirlpool program (same ID on mainnet + devnet):
  `whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc`
- Orca devnet WhirlpoolsConfig:
  `FcrweFY1G9HJAHG5inkGB6pKg1HZ6x9UC2WioAfWrGkR`
- Raydium devnet CPMM program:
  `CPMDWBwJDtYax9qW7AyRuVC19Cc4L4Vcy4n2BHAbHkCW`
  (CPMM config: `9zSzfkYy6awexsHvmggeH36pfVUdDGyCcwmjT3AQPBj6`)
- Raydium devnet CLMM program:
  `devi51mZmdwUJGU9hjN27vEz64Gps7uUefqxg27EAtH`
  (CLMM config: `CQYbhr6amxUER4p5SC44C63R4qw4NFc9Z4Db9vF4tZwG`)
- Raydium devnet AMM v4 program:
  `DRaya7Kj3aMWQSy19kSjvmuwq9docCHofyP9kanQGaav`
  (needs an OpenBook market first — do NOT use AMM v4
  for this plan; CPMM or Whirlpool is simpler)
- Jupiter aggregator program: NO account on devnet.
  Jupiter is NOT a devnet route. Do not use it here.

## Step 1 — Throwaway wallet + faucet SOL (desktop)

1. On your desktop (not your phone), create a fresh
   throwaway keypair for devnet testing only:
   `solana-keygen new --no-bip39-passphrase -o ~/throwaway-devnet.json`
   Write down its public address:
   `solana-keygen pubkey ~/throwaway-devnet.json`
2. Point the CLI at devnet:
   `solana config set --url https://api.devnet.solana.com`
3. Get faucet SOL (free, devnet only; the faucet
   rate-limits, so retry later if it says so):
   `solana airdrop 2 <THROWAWAY_ADDRESS> --url devnet`
   Confirm: `solana balance <THROWAWAY_ADDRESS> --url devnet`
   You want at least ~2 devnet SOL for mint + pool rent
   and fees. If the faucet is rate-limited, wait and
   retry — do NOT fund this wallet from any real wallet.

## Step 2 — Create a non-pegged test token (desktop)

Use the SPL Token CLI (devnet only). Decimals 6 is
fine for a test token; supply 1,000,000 tokens is
plenty for a proof pool:

```
spl-token create-token --decimals 6 --url devnet
# -> note the MINT address it prints; call it <TEST_MINT>
spl-token create-account <TEST_MINT> --url devnet
spl-token mint <TEST_MINT> 1000000 --url devnet
spl-token supply <TEST_MINT> --url devnet
```

Do NOT revoke or burn anything yet — you need the
mint authority later only if you want more supply.
Keep the mint address safe; it is public, not secret.

## Step 3 — Create a pool (desktop, official SDK only)

Pick ONE path. Both need Node + the official SDK on
your desktop, pointed at devnet, using ONLY the
throwaway keypair above. Use the SDK's own example
for pool creation (do not hand-roll pool instructions
and do not copy a script from a random site):

### Path A (recommended first try): Raydium CPMM

- Package: `@raydium-io/raydium-sdk-v2`
- Program: devnet CPMM above; config: devnet CPMM
  config above. CPMM needs NO OpenBook market.
- Create the pool with <TEST_MINT> + wrapped SOL,
  seeding e.g. 100,000 test tokens against 1 devnet
  SOL (that ratio sets the starting price: 1 token =
  0.00001 SOL — any reasonable ratio works for a
  proof; it is not a real price).
- Before confirming, check the transaction preview:
  program ID must be the devnet CPMM program above,
  the two mints must be <TEST_MINT> and wrapped SOL
  (`So11111111111111111111111111111111111111112`),
  and the amounts must be the ones you entered.
  If anything else appears, do NOT sign.

### Path B: Orca Whirlpool (full-range position)

- Package: `@orca-so/whirlpools`
- Program: devnet Whirlpool program above; config:
  devnet WhirlpoolsConfig above.
- Create a Whirlpool pool for <TEST_MINT> + wrapped
  SOL, then open a FULL-RANGE position (the whole
  price range) with e.g. 100,000 test tokens + 1
  devnet SOL. A full-range position is what makes
  the pool tradeable at any price in the proof.
- Same preview check as Path A: program ID must be
  the Whirlpool program above, mints must be
  <TEST_MINT> + wrapped SOL.

Whichever path you take, note the POOL ADDRESS it
prints. That address is public, not secret.

## Step 4 — Verify the pool (read-only, no signing)

From the engine folder, with no key needed:

```
python3 verify_devnet_pool.py --pool <POOL_ADDRESS>
```

It must show, all at once:
- `program_exists: true`, `program_executable: true`
- `pool_owner` = the program ID from Step 3
- `token_mint_a` / `token_mint_b` = wrapped SOL +
  <TEST_MINT> (in either order)
- Both vault balances present and positive
- `is_sol_pair: true`, `quote_verified: true`
- A `spot_token_b_per_token_a` number, a
  `price_sol_per_token` number, and a
  `devnet_pools_json_entry` block

It will also say `execution_verified: false` —
that is CORRECT at this stage. This step proves
the pool and its price only.

Extra read-only proof (optional, same result):
```
curl https://api.devnet.solana.com -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"getAccountInfo","params":["<POOL_ADDRESS>",{"encoding":"base64"}]}'
```
The returned `value` must not be null, its `owner`
must be the program ID from Step 3, and
`executable` must be false (pool accounts are data,
not programs — only the program itself is
executable).

If any check fails, STOP and report the pool
address + the verifier output — do not inject
unverified data into the engine.

## Step 5 — Inject the verified pool (deployment only)

Only after Step 4 passes, in the Railway devnet
service (names only, never values in chat):

1. Set `WATCH_MINTS` to exactly <TEST_MINT>
   (the pool's non-SOL mint, nothing else).
2. Set `DEVNET_POOLS_JSON` to exactly the
   `devnet_pools_json_entry` block printed by the
   verifier (a JSON object keyed by <TEST_MINT>).
   Do NOT type a price by hand, do NOT copy a
   mainnet price, and do NOT reuse the documented
   test pool's entry — that pool's second mint is a
   pegged test mint and must NOT be a trading mint
   under the SOL-only rule.
3. Do NOT change any other variable. Do NOT set
   `LIVE_ENABLED=1`. Do NOT add `--tx-builder solana`
   yet.

## Step 6 — What is still missing after the pool exists

Creating the pool does NOT unlock trading by itself.
The remaining build, in order, is:

1. Build a Whirlpool (or Raydium CPMM) swap-transaction
   provider in the engine, and test it FIRST with a
   throwaway wallet on devnet against the new pool
   (a real SOL -> test-token swap and a real
   test-token -> SOL swap, both confirmed on devnet).
2. Push + deploy that provider (code only, no
   enabling).
3. Run the deployment smoke test in Railway: it must
   show a proven route for <TEST_MINT> (not
   `route_unverified`) AND the feed OK.
4. Only then, add `--tx-builder solana` to the start
   command, re-run the smoke test, and — under the
   already-banked devnet go-ahead — set
   `LIVE_ENABLED=1`.
5. Mainnet is a SEPARATE go-ahead afterwards, with
   its own funding, key check, and smoke test. This
   devnet plan authorises nothing on mainnet.

## If the SDK pool creation is too heavy

If the official SDK route stalls, the fallback is
NOT to fake a route or inject a mainnet price. The
fallback is to stop at Step 4's verification and
report exactly where it stalled (which SDK, which
error, which step) so the pool-creation step can be
done differently — e.g. via a trusted no-code devnet
pool tool, after checking its program ID against the
verified devnet IDs above and re-running Step 4.
Never enter a seed phrase into such a tool, and
never use the trading wallet with it.
