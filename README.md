# extended-mm-bot

Compliance-first market making MVP for Extended perpetuals (Starknet). The bot streams order books, computes simplified Avellaneda-Stoikov quotes with inventory skew, and manages orders via the X10 Python SDK.

## Features
- Async REST and WebSocket connectivity with retry/backoff and required headers
- Local order book reconstruction with rolling volatility estimate
- Simplified quoting engine supporting funding and inventory skew
- Execution module aware of replace thresholds and post-only placement
- Risk manager with net position caps and circuit breaker hooks
- PnL tracking scaffold for spread, inventory, fees, and funding
- Skeleton tests for order book replay and risk logic

## Getting Started
1. `cp .env.example .env` and fill in `EXTENDED_API_KEY`, Stark keys, and vault id from Extended testnet.
2. Create a virtual environment and install dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Run the bot (testnet by default):
   ```bash
   python -m src.bot
   ```

## Docker
A minimal runtime image is provided under `docker/Dockerfile`.

## Testing
Run unit tests with:
```bash
pytest
```

## Notes
- Start on Extended testnet and validate reconnect, cancel, and risk flows before deploying to mainnet.
- Store API keys securely (environment variables or vault). Avoid committing secrets.
- Self-trade prevention and more granular inventory tracking should be completed before production deployment.
