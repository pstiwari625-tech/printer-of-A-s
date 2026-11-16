"""
termux_listener.py - Production-grade Android SMS & Push Notification Listener
Listens to BOTH:
1. Standard Cellular/Bank SMS (via termux-sms-list)
2. App Push Notifications from Paytm, PhonePe, GPay, Bank Apps (via termux-notification-list)
"""

import subprocess
import json
import time
import os
import hashlib
from typing import Set, Dict, Any
from bridge import SupabasePaymentBridge

POLL_INTERVAL_SECONDS = 3
SEEN_HASHES_FILE = "seen_sms_hashes.txt"

# Target payment apps for push notification filtering
PAYMENT_PACKAGES = [
    "net.one97.paytm",                                 # Paytm & Paytm for Business
    "com.phonepe.app",                                 # PhonePe
    "com.phonepe.app.business",                        # PhonePe Business
    "com.google.android.apps.nbu.paisa.user",          # Google Pay
    "in.org.npci.upiapp",                              # BHIM
    "com.kotak.mbusiness",                             # Kotak Bank
    "com.msf.kbank.mobile"                             # Kotak 811
]

def get_message_hash(raw_text: str) -> str:
    """Creates a unique 64-char SHA-256 hash for deduplication."""
    return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

def load_seen_hashes() -> Set[str]:
    seen = set()
    if os.path.exists(SEEN_HASHES_FILE):
        with open(SEEN_HASHES_FILE, "r") as f:
            for line in f:
                h = line.strip()
                if h:
                    seen.add(h)
    return seen

def save_seen_hash(msg_hash: str):
    with open(SEEN_HASHES_FILE, "a") as f:
        f.write(f"{msg_hash}\n")

def get_recent_sms(limit: int = 5):
    """Fetches cellular inbox SMS."""
    try:
        result = subprocess.run(
            ["termux-sms-list", "-l", str(limit), "-t", "inbox"],
            capture_output=True,
            text=True,
            check=True
        )
        data = json.loads(result.stdout)
        return data if isinstance(data, list) else []
    except Exception:
        return []

def get_recent_notifications():
    """Fetches Android status bar push notifications (Paytm, GPay, PhonePe)."""
    try:
        result = subprocess.run(
            ["termux-notification-list"],
            capture_output=True,
            text=True,
            check=True
        )
        data = json.loads(result.stdout)
        return data if isinstance(data, list) else []
    except Exception:
        return []

def main():
    print("=" * 60)
    print("🚀 Termux Dual-Mode Listener Started (SMS + Push Notifications)")
    print(f"Monitoring Paytm, PhonePe, GPay & SMS every {POLL_INTERVAL_SECONDS}s...")
    print("=" * 60)

    bridge = SupabasePaymentBridge()
    seen_hashes = load_seen_hashes()

    # Index existing messages & notifications on startup to avoid processing old ones
    print("🔍 Indexing existing SMS and active notifications...")
    for s in get_recent_sms(limit=10):
        h = get_message_hash(f"sms_{s.get('number','')}_{s.get('received','')}_{s.get('body','')}")
        seen_hashes.add(h)
        save_seen_hash(h)

    for n in get_recent_notifications():
        h = get_message_hash(f"notif_{n.get('packageName','')}_{n.get('title','')}_{n.get('content','')}")
        seen_hashes.add(h)
        save_seen_hash(h)

    print("✅ Startup indexing complete. Actively listening for payments...\n")

    while True:
        try:
            # 1. Check for Cellular SMS
            for msg in get_recent_sms(limit=5):
                body = msg.get("body", "")
                h = get_message_hash(f"sms_{msg.get('number','')}_{msg.get('received','')}_{body}")
                if h in seen_hashes:
                    continue

                seen_hashes.add(h)
                save_seen_hash(h)

                sender = msg.get("number", "UNKNOWN")
                print(f"\n[🔔 NEW SMS from {sender}]: {body[:70]}...")
                bridge.process_sms(body)

            # 2. Check for App Push Notifications (Paytm / PhonePe / GPay)
            for notif in get_recent_notifications():
                pkg = notif.get("packageName", "")
                title = notif.get("title", "")
                content = notif.get("content", "")
                combined_text = f"{title} {content}".strip()

                h = get_message_hash(f"notif_{pkg}_{title}_{content}")
                if h in seen_hashes:
                    continue

                # Filter: check if notification is from a payment app or contains money keywords
                is_payment_app = any(p in pkg for p in PAYMENT_PACKAGES)
                has_money_keywords = any(k in combined_text.lower() for k in ["received", "credited", "rs.", "inr", "₹"])

                if is_payment_app or has_money_keywords:
                    seen_hashes.add(h)
                    save_seen_hash(h)

                    app_name = pkg.split(".")[-1].capitalize()
                    print(f"\n[📱 PUSH NOTIFICATION from {app_name}]: {combined_text}...")
                    bridge.process_sms(combined_text)

        except KeyboardInterrupt:
            print("\nStopping Termux listener.")
            break
        except Exception as e:
            print(f"[LOOP ERROR]: {e}")

        time.sleep(POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
