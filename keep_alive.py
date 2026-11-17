#!/usr/bin/env python3
"""
Printer of A's - Intelligent Anti-Sleep Random Keep-Alive
---------------------------------------------------------
Pings Render web service at randomized intervals between 10 and 14 minutes
(with random minutes and seconds) using rotating browser User-Agents.
Prevents Render free tier from sleeping without triggering robotic cron / bot filters.

Usage:
    python3 keep_alive.py
Or in background:
    nohup python3 keep_alive.py > keep_alive.log 2>&1 &
"""

import os
import sys
import time
import random
import urllib.request
from datetime import datetime

TARGET_URL = os.environ.get("RENDER_URL", "https://printer-of-a-s.onrender.com/health")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.6312.40 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0"
]

def ping_now():
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ua = random.choice(USER_AGENTS)
    req = urllib.request.Request(
        TARGET_URL,
        headers={
            "User-Agent": ua,
            "Accept": "text/html,application/json,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache"
        }
    )
    try:
        start = time.time()
        with urllib.request.urlopen(req, timeout=25) as resp:
            elapsed = time.time() - start
            print(f"[{now_str}] SUCCESS: Pinged {TARGET_URL} | Status: {resp.status} | Time: {elapsed:.2f}s")
            return True
    except Exception as e:
        print(f"[{now_str}] WARNING: Ping failed: {e}")
        return False

def main():
    print("=" * 65)
    print("  Printer of A's - Anti-Sleep Random Keep-Alive Monitor")
    print(f"  Target: {TARGET_URL}")
    print("  Interval: Random between 10m 00s and 14m 00s (Random jitter)")
    print("=" * 65)

    # Initial ping upon start
    ping_now()

    while True:
        # Pick completely random seconds between 600s (10 min) and 840s (14 min)
        sleep_secs = random.randint(600, 840)
        mins = sleep_secs // 60
        secs = sleep_secs % 60
        next_wake = datetime.fromtimestamp(time.time() + sleep_secs).strftime("%H:%M:%S")

        print(f">> Waiting {mins}m {secs}s (randomized). Next ping scheduled at {next_wake}...")
        time.sleep(sleep_secs)
        ping_now()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nKeep-alive monitor stopped.")
        sys.exit(0)
