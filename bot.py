import os
import sys
import json
import time
import logging
from collections import defaultdict
from datetime import datetime, timezone

import requests
from solders.pubkey import Pubkey
from solana.rpc.api import Client

# ---------------------------
# Helpers
# ---------------------------
def now_ts():
    return int(datetime.now(tz=timezone.utc).timestamp())

def short(s, n=4):
    if not s:
        return s
    return f"{s[:n]}…{s[-n:]}"

def load_config():
    """
    EXE-friendly config loader.
    If running as .exe (PyInstaller), sys._MEIPASS exists and config.json will be bundled.
    Otherwise, load from script folder.
    """
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    cfg_path = os.path.join(base_path, "config.json")
    with open(cfg_path, "r", encoding="utf-8") as f:
        return json.load(f)

def send_tg(bot_token, chat_id, text, dry_run=False):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if dry_run:
        print("[DRY_RUN TG]", text)
        return True
    try:
        r = requests.post(url, data=payload, timeout=10)
        return r.ok
    except Exception as e:
        print("Telegram error:", type(e).__name__, "-", e)
        return False

def new_client(rpc_url):
    # create a Solana JSON-RPC client
    return Client(rpc_url)

def rpc_is_healthy(client):
    try:
        res = client.get_slot()
        return res and res.value is not None
    except Exception:
        return False

def get_healthy_client(rpc_urls):
    for url in rpc_urls:
        c = new_client(url)
        if rpc_is_healthy(c):
            return c, url
    return None, None

# Parse SPL token balance deltas for a specific owner (wallet)
def extract_token_changes_for_wallet(tx_obj, wallet_str):
    """
    Returns list of events for this wallet from a parsed transaction:
    [
      {"mint": "<mint>", "pre": float, "post": float, "delta": float, "dir": "BUY"/"SELL"}
    ]
    """
    events = []
    try:
        val = tx_obj.value
        if not val:
            return events
        meta = val.get("meta") if isinstance(val, dict) else None
        if not meta:
            return events

        pre = meta.get("preTokenBalances") or []
        post = meta.get("postTokenBalances") or []

        # Build map: (owner, mint) -> amount
        def to_map(arr):
            d = {}
            for it in arr:
                owner = it.get("owner")
                mint = it.get("mint")
                ui = it.get("uiTokenAmount") or {}
                amt_str = ui.get("amount") or "0"
                dec = ui.get("decimals") or 0
                # ui 'uiAmountString' may exist; but safe compute:
                try:
                    # some RPCs send integer string; divide by 10**dec
                    amt = float(amt_str) / (10 ** int(dec))
                except Exception:
                    amt = float(ui.get("uiAmount", 0.0) or 0.0)
                d[(owner, mint)] = amt
            return d

        pre_map = to_map(pre)
        post_map = to_map(post)

        # For all mints owned by this wallet
        keys = set([k for k in pre_map.keys() if k[0] == wallet_str]) | set([k for k in post_map.keys() if k[0] == wallet_str])

        for (owner, mint) in keys:
            pre_amt = pre_map.get((owner, mint), 0.0)
            post_amt = post_map.get((owner, mint), 0.0)
            delta = round(post_amt - pre_amt, 12)
            if abs(delta) > 0:
                direction = "BUY" if delta > 0 else "SELL"
                events.append({
                    "mint": mint,
                    "pre": pre_amt,
                    "post": post_amt,
                    "delta": delta,
                    "dir": direction,
                })
    except Exception as e:
        print("extract_token_changes_for_wallet error:", type(e).__name__, e)
    return events

def solscan_token_link(mint):
    return f"https://solscan.io/token/{mint}"

def solana_tx_link(sig):
    return f"https://explorer.solana.com/tx/{sig}"

# ---------------------------
# Main
# ---------------------------
def main():
    config = load_config()

    RPC_URLS = config.get("RPC_URLS", ["https://api.mainnet-beta.solana.com"])
    BOT_TOKEN = config["TELEGRAM_BOT_TOKEN"]
    CHAT_ID = config["TELEGRAM_CHAT_ID"]
    WALLETS = [str(w) for w in config.get("WALLETS", [])]
    assert len(WALLETS) == 2, "Exactly 2 wallets required for 'both buy same coin' signal."

    POLL_INTERVAL_SEC = int(config.get("POLL_INTERVAL_SEC", 10))
    WINDOW_SEC = int(config.get("WINDOW_SEC", 120))
    COOLDOWN_SEC = int(config.get("COOLDOWN_SEC", 300))
    LOOKBACK_SIGNATURES = int(config.get("LOOKBACK_SIGNATURES", 20))
    DRY_RUN = bool(config.get("DRY_RUN", False))
    SIGNAL_ON_BOTH_BUY = bool(config.get("SIGNAL_ON_BOTH_BUY", True))
    SIGNAL_ON_BOTH_SELL = bool(config.get("SIGNAL_ON_BOTH_SELL", True))

    # Logging (file + console)
    logging.basicConfig(
        filename="bot.log",
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    client, active_rpc = get_healthy_client(RPC_URLS)
    if not client:
        print("No healthy RPC available! Check your RPC URLs or network.")
        return

    print("🚀 Bot started... Watching wallets:", WALLETS)
    print("🟢 Active RPC:", active_rpc)

    # Track processed signatures to avoid reprocessing
    processed_sigs = set()

    # Track last event times per wallet & mint & direction
    # last_event[wallet][(dir, mint)] = timestamp
    last_event = defaultdict(dict)

    # Cooldown to avoid duplicate alerts
    # cooldown[(dir, mint)] = timestamp when last sent
    cooldown = {}

    # Bootstrap: optional skip past history
    if config.get("BOOTSTRAP_SKIP_HISTORY", True):
        # mark recent signatures as processed so we start "fresh"
        for w in WALLETS:
            try:
                sigs = client.get_signatures_for_address(Pubkey.from_string(w), limit=LOOKBACK_SIGNATURES).value
                for s in sigs:
                    processed_sigs.add(str(s.signature))
            except Exception as e:
                print("Bootstrap error for", w, "-", type(e).__name__, e)

    while True:
        try:
            # rotate RPC if current becomes unhealthy
            if not rpc_is_healthy(client):
                client, active_rpc = get_healthy_client(RPC_URLS)
                if not client:
                    print("No healthy RPC available right now… retrying soon.")
                    time.sleep(POLL_INTERVAL_SEC)
                    continue
                print("🔄 Switched RPC to:", active_rpc)

            # poll each wallet
            for idx, w in enumerate(WALLETS):
                other = WALLETS[1 - idx]

                # fetch recent signatures
                try:
                    sigs = client.get_signatures_for_address(Pubkey.from_string(w), limit=LOOKBACK_SIGNATURES).value
                except Exception as e:
                    print("RPC error get_signatures_for_address:", type(e).__name__, e)
                    continue

                for entry in sigs:
                    sig = str(entry.signature)
                    if sig in processed_sigs:
                        continue

                    # fetch parsed transaction
                    tx = None
                    try:
                        # new clients accept max_supported_transaction_version
                        tx = client.get_transaction(
                            sig,
                            encoding="jsonParsed",
                            max_supported_transaction_version=0
                        )
                    except Exception:
                        # older RPCs without the param
                        tx = client.get_transaction(sig, encoding="jsonParsed")

                    processed_sigs.add(sig)

                    if not tx or not tx.value:
                        continue

                    # blockTime for windowing
                    ts = tx.value.get("blockTime", None)
                    evt_ts = int(ts) if ts else now_ts()

                    # find wallet-specific SPL token changes
                    changes = extract_token_changes_for_wallet(tx, w)
                    if not changes:
                        continue

                    # for each mint change, update last_event and check intersection
                    for ch in changes:
                        mint = ch["mint"]
                        direction = ch["dir"]   # BUY or SELL

                        # ignore 0 or dust changes if needed (optional)
                        # if abs(ch["delta"]) < 1e-9: continue

                        last_event[w][(direction, mint)] = evt_ts

                        # Check counterpart wallet within WINDOW_SEC
                        # BUY x BUY => notify if SIGNAL_ON_BOTH_BUY
                        # SELL x SELL => notify if SIGNAL_ON_BOTH_SELL
                        for pair_dir, enabled in (("BUY", SIGNAL_ON_BOTH_BUY), ("SELL", SIGNAL_ON_BOTH_SELL)):
                            if not enabled:
                                continue
                            if direction != pair_dir:
                                continue

                            t_other = last_event[other].get((pair_dir, mint))
                            if not t_other:
                                continue

                            if abs(evt_ts - t_other) <= WINDOW_SEC:
                                # cooldown check
                                key = (pair_dir, mint)
                                last_sent = cooldown.get(key, 0)
                                if now_ts() - last_sent < COOLDOWN_SEC:
                                    continue  # still cooling

                                # Build alert
                                token_url = solscan_token_link(mint)
                                msg = (
                                    f"🔔 *Signal!* Both wallets **{pair_dir}** the same token\n\n"
                                    f"🪪 Wallet A: `{short(WALLETS[0])}`\n"
                                    f"🪪 Wallet B: `{short(WALLETS[1])}`\n"
                                    f"🪙 Mint: `{mint}`\n"
                                    f"🔗 [View Token]({token_url})\n"
                                    f"⏱ Window: {WINDOW_SEC}s\n"
                                )

                                print(msg.replace("*", ""))
                                ok = send_tg(BOT_TOKEN, CHAT_ID, msg, DRY_RUN)
                                if ok:
                                    logging.info(f"Sent {pair_dir} signal for mint {mint}")
                                    cooldown[key] = now_ts()

            time.sleep(POLL_INTERVAL_SEC)

        except KeyboardInterrupt:
            print("Exiting by user…")
            break
        except Exception as e:
            print("Unexpected error:", type(e).__name__, "-", e)
            time.sleep(3)

if __name__ == "__main__":
    main()
