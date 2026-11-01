# SolBot LIVE 5-bot engine — DISABLED by default.
#
# This image packages the engine only. It does NOT start a trading loop
# (no loop exists in this build), sends no transaction, signs nothing,
# and sets no signing key. LIVE_ENABLED is explicitly 0 in the image;
# unset is also off. A key, if ever set, is set by James himself in
# Railway Variables — never in this file, never in the repo.
#
# Build with the context set to this folder (Railway: set the service's
# Root Directory to this folder, builder = Dockerfile).

FROM python:3.12-slim

WORKDIR /app

# Stdlib only — no dependencies to install, no network fetch at build
# time beyond the base image.
COPY engine.py dry_run.py RULES.md README.md ./
COPY tests ./tests

# Fail the build if any unit test fails.
RUN python3 -m unittest discover -s tests

# Explicitly disabled. No TRADING_PRIVATE_KEY / TRADING_KEYPAIR here.
# NETWORK is NOT set in this image (unset = mainnet default). A devnet
# service sets NETWORK=devnet in Railway Variables; a mainnet service
# sets NETWORK=mainnet or leaves it unset.
ENV PYTHONUNBUFFERED=1
ENV LIVE_ENABLED=0

# Disabled-mode guard: proves the live gate is off and no signer is
# available, then exits cleanly. It performs no network access, no
# signing, and no trading.
CMD ["python3", "-c", "from engine import is_live_enabled, load_signer; assert not is_live_enabled(); assert load_signer() is None; print('solbot-live-5bot: DISABLED - no trading, no signing')"]
