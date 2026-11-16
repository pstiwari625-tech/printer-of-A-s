"""
termux_listener.py - Production-grade Android SMS Listener
Prevents loops, deduplicates via SHA-256 hashing, and ignores old past SMS.
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

def get_message_hash(msg: Dict[str, Any]) -> str:
    """
    Creates a unique 64-char SHA-256 hash for each SMS.
    Guaranteed single-line, collision-free, handles multi-line bodies cleanly.
    """
    raw_str = f"{msg.get('number', '')}_{msg.get('received', '')}_{msg.get('date', '')}_{msg.get('body', '')}"
    return hashlib.sha256(raw_str.encode("utf-8")).hexdigest()

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
    try:
        result = subprocess.run(
            ["termux-sms-list", "-l", str(limit), "-t", "inbox"],
            capture_output=True,
            text=True,
            check=True
        )
        data = json.loads(result.stdout)
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        print("[ERROR] 'termux-sms-list' not found. Ensure termux-api is installed.")
        return []
    except Exception as e:
        print(f"[ERROR] Failed to read SMS via Termux: {e}")
        return []

def main():
    print("=" * 60)
    print("🚀 Termux Autonomous UPI/Bank Payment Listener Started")
    print(f"Polling inbox every {POLL_INTERVAL_SECONDS} seconds...")
    print("=" * 60)

    bridge = SupabasePaymentBridge()
    seen_hashes = load_seen_hashes()

    # On initial boot, mark existing inbox messages as already seen
    # so we don't re-trigger past payments on script restart!
    print("🔍 Indexing existing inbox messages...")
    initial_messages = get_recent_sms(limit=10)
    new_indexed = 0
    for m in initial_messages:
        h = get_message_hash(m)
        if h not in seen_hashes:
            seen_hashes.add(h)
            save_seen_hash(h)
            new_indexed += 1
    print(f"✅ Indexed {len(initial_messages)} existing messages. Now waiting for NEW incoming SMS...\n")

    while True:
        try:
            messages = get_recent_sms(limit=5)
            for msg in messages:
                h = get_message_hash(msg)
                
                # Deduplication check: ignore if already processed
                if h in seen_hashes:
                    continue

                # Mark as seen IMMEDIATELY to prevent duplicate loops
                seen_hashes.add(h)
                save_seen_hash(h)

                body = msg.get("body", "")
                sender = msg.get("number", "UNKNOWN")

                print(f"\n[🔔 NEW SMS from {sender}]: {body[:70]}...")
                result = bridge.process_sms(body)

        except KeyboardInterrupt:
            print("\nStopping Termux listener.")
            break
        except Exception as e:
            print(f"[LOOP ERROR]: {e}")

        time.sleep(POLL_INTERVAL_SECONDS)

if __name__ == "__main__":
    main()
