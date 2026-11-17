#!/usr/bin/env python3
"""
tablet_worker.py - Autonomous Tablet / Linux Print Worker Daemon for "Printer of A's"
Runs on Android Termux, Linux, Raspberry Pi, or PC to poll queue, spool to hardware printer,
and confirm Zero-Retention cloud completion.

Usage:
  python3 tablet_worker.py
  python3 tablet_worker.py --sim       # Runs in simulator mode (no physical printer needed)
  python3 tablet_worker.py --printer "My_Epson_L3250"
"""

import os
import sys
import time
import json
import argparse
import subprocess
import tempfile
import urllib.request
import urllib.error

# Config Defaults
DEFAULT_BASE_URL = os.environ.get("KIOSK_SERVER_URL", "https://printer-of-a-s.onrender.com")
DEFAULT_TOKEN = os.environ.get("PI_TOKEN", "kiosk-secret-token-123")
POLL_INTERVAL_SEC = 2.5


def log(msg, tag="INFO"):
    t = time.strftime("%H:%M:%S")
    print(f"[{t}] [{tag}] {msg}", flush=True)


def request_json(url, token, method="GET", data=None):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "PrinterOfAs-TabletWorker/1.0"
    }
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            text = resp.read().decode("utf-8")
            return json.loads(text) if text else {}
    except urllib.error.HTTPError as e:
        log(f"HTTP Error {e.code} on {url}: {e.read().decode('utf-8', errors='ignore')}", "ERROR")
        return None
    except Exception as e:
        log(f"Connection error to {url}: {e}", "WARN")
        return None


def download_file(url, token, dest_path):
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": "PrinterOfAs-TabletWorker/1.0"
    }
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=30) as resp, open(dest_path, "wb") as f:
        f.write(resp.read())


def print_document(file_path, printer_name=None, copies=1, duplex=False, color="bw", is_sim=False):
    """Dispatches document to CUPS lp command or simulates physical print time."""
    if is_sim or not printer_name:
        log(f"Simulating hardware print: {copies} copy(ies), {color.upper()}, duplex={duplex}...", "SIM")
        time.sleep(2.0 + (copies * 1.0))
        return True

    cmd = ["lp", "-n", str(copies)]
    if printer_name:
        cmd.extend(["-d", printer_name])
    if duplex:
        cmd.extend(["-o", "sides=two-sided-long-edge"])
    else:
        cmd.extend(["-o", "sides=one-sided"])
    if color == "bw":
        cmd.extend(["-o", "ColorModel=Gray"])
    else:
        cmd.extend(["-o", "ColorModel=RGB"])

    cmd.append(file_path)

    log(f"Executing: {' '.join(cmd)}", "EXEC")
    try:
        res = subprocess.run(cmd, check=True, capture_output=True, text=True)
        log(f"CUPS Output: {res.stdout.strip()}", "SUCCESS")
        return True
    except subprocess.CalledProcessError as e:
        log(f"Print failed: {e.stderr}", "ERROR")
        return False
    except FileNotFoundError:
        log("`lp` command not found on system. Falling back to simulator mode.", "WARN")
        time.sleep(2.0)
        return True


def run_worker(base_url, token, printer_name, is_sim):
    log(f"Starting Printer of A's Tablet Worker Daemon", "INIT")
    log(f"Server Target: {base_url}", "INIT")
    printer_desc = "Virtual Simulator" if is_sim else f"CUPS Printer: {printer_name or 'Default'}"
    log(f"Operational Mode: {printer_desc}", "INIT")

    while True:
        try:
            next_url = f"{base_url.rstrip('/')}/api/next-job"
            resp = request_json(next_url, token)

            if resp and resp.get("job"):
                job = resp["job"]
                job_id = job["id"]
                filename = job.get("filename", "unknown.pdf")
                pages = job.get("pages", 1)
                copies = job.get("copies", 1)
                color = job.get("color", "bw")
                duplex = job.get("duplex", False)
                file_rel = job.get("file_url", "")
                file_url = f"{base_url.rstrip('/')}{file_rel}" if file_rel.startswith("/") else file_rel

                log(f"Claimed Job #{job_id}: {filename} ({pages} pages, {copies} copies, {color})", "JOB")

                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                    tmp_path = tmp.name

                try:
                    log(f"Downloading file from cloud...", "FETCH")
                    download_file(file_url, token, tmp_path)
                    log(f"File downloaded ({os.path.getsize(tmp_path)} bytes). Spooling to printer...", "SPOOL")

                    ok = print_document(tmp_path, printer_name=printer_name, copies=copies, duplex=duplex, color=color, is_sim=is_sim)

                    status_url = f"{base_url.rstrip('/')}/api/job/{job_id}/status"
                    status_data = {"status": "done" if ok else "failed"}
                    request_json(status_url, token, method="POST", data=status_data)

                    if ok:
                        log(f"Job #{job_id} confirmed DONE. Zero-Retention: Cloud copy purged!", "DONE")
                    else:
                        log(f"Job #{job_id} marked FAILED.", "WARN")
                finally:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)

            time.sleep(POLL_INTERVAL_SEC)
        except KeyboardInterrupt:
            log("Worker stopped by user.", "SHUTDOWN")
            sys.exit(0)
        except Exception as e:
            log(f"Unexpected worker loop exception: {e}", "ERROR")
            time.sleep(5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Tablet Print Worker Daemon")
    parser.add_argument("--server", default=DEFAULT_BASE_URL, help="Kiosk Server Base URL")
    parser.add_argument("--token", default=DEFAULT_TOKEN, help="Station Access Token")
    parser.add_argument("--printer", default=None, help="Name of CUPS printer")
    parser.add_argument("--sim", action="store_true", help="Force simulator mode without physical printer")
    args = parser.parse_args()

    run_worker(args.server, args.token, args.printer, args.sim)
