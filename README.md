# Solana Multi-Wallet Confirmation Bot (Telegram)

Monitors multiple Solana wallets and sends a Telegram alert when two or more of them **buy the same SPL token** within a time window.

> Heuristic: any **net increase** in an SPL token balance for a given wallet is treated as a "buy".

## Quick Start

1) Create a Telegram Bot with **@BotFather** and copy the token.
2) Find your chat id (DM the bot once, then visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` or use @RawDataBot).
3) Fill `config.json`:
```json
{
  "RPC_URL": "https://api.mainnet-beta.solana.com",
  "TELEGRAM_BOT_TOKEN": "YOUR_TELEGRAM_BOT_TOKEN",
  "TELEGRAM_CHAT_ID": "YOUR_TELEGRAM_CHAT_ID",
  "WALLETS": ["WALLET1","WALLET2"],
  "CONFIRMATION_THRESHOLD": 2,
  "POLL_INTERVAL_SEC": 10,
  "LOOKBACK_SIGNATURES": 20,
  "WINDOW_SEC": 120,
  "BOOTSTRAP_SKIP_HISTORY": true,
  "COOLDOWN_SEC": 300,
  "DRY_RUN": false
}
```
> Tip: Use a private RPC (Helius/QuickNode/Alchemy/etc.) to avoid rate limits.

4) Install deps and run:
```bash
pip install -r requirements.txt
python bot.py
```

## How it works

- Polls `getSignaturesForAddress` for each wallet.
- Fetches fresh transactions via `getTransaction` (jsonParsed).
- Compares pre/post token balances; positive deltas for the wallet = "buy".
- Keeps a sliding window (`WINDOW_SEC`) per mint; if at least `CONFIRMATION_THRESHOLD` unique wallets bought the same mint in that window, sends a Telegram alert.
- Cooldown per mint to avoid spam.

## Notes

- "Buy" vs "transfer" is heuristic-based. For precision on DEX-specific buys, integrate with a DEX/indexer (Helius webhooks, Raydium tx decoders).
- Increase `LOOKBACK_SIGNATURES` if wallets are very active.
- Set `DRY_RUN: true` to print alerts locally without sending to Telegram.

## Run as a service (systemd – optional)

Create `/etc/systemd/system/sol-multiwallet.service` (Linux):
```
[Unit]
Description=Solana Multi-Wallet Confirmation Bot
After=network.target

[Service]
WorkingDirectory=/path/to/sol-multiwallet
ExecStart=/usr/bin/python3 bot.py
Restart=always
User=youruser

[Install]
WantedBy=multi-user.target
```
Then:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sol-multiwallet
```