"""
cashfree_helper.py - Lightweight Cashfree Payment Gateway Client
Zero external dependencies: uses Python standard library urllib, json, hmac, hashlib, base64.
Compatible with Cashfree PG API version 2023-08-01.
"""

import os
import json
import hmac
import hashlib
import base64
import urllib.request
import urllib.error
from typing import Optional, Dict, Any, List

def load_env():
    script_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(script_dir, ".env"),
        os.path.join(script_dir, "..", ".env"),
        ".env",
        "../.env"
    ]
    for env_path in candidates:
        if os.path.exists(env_path):
            try:
                with open(env_path, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            if k.strip() not in os.environ:
                                os.environ[k.strip()] = v.strip()
            except Exception:
                pass

load_env()

API_VERSION = "2023-08-01"


def get_config():
    app_id = (os.getenv("CASHFREE_APP_ID") or "").strip()
    secret_key = (os.getenv("CASHFREE_SECRET_KEY") or "").strip()
    env = (os.getenv("CASHFREE_ENV") or "").strip().lower()

    if not env:
        # Auto-detect sandbox if app_id starts with TEST
        env = "sandbox" if app_id.startswith("TEST") else "production"

    base_url = (
        "https://sandbox.cashfree.com/pg"
        if env == "sandbox"
        else "https://api.cashfree.com/pg"
    )

    return {
        "app_id": app_id,
        "secret_key": secret_key,
        "env": env,
        "base_url": base_url
    }


def is_cashfree_configured() -> bool:
    cfg = get_config()
    return bool(cfg["app_id"] and cfg["secret_key"])


def _headers() -> Dict[str, str]:
    cfg = get_config()
    return {
        "x-client-id": cfg["app_id"],
        "x-client-secret": cfg["secret_key"],
        "x-api-version": API_VERSION,
        "Content-Type": "application/json"
    }


def _request(endpoint: str, method: str = "GET", data: Optional[Dict] = None) -> Any:
    cfg = get_config()
    url = f"{cfg['base_url']}/{endpoint.lstrip('/')}"
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=_headers(), method=method)

    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            resp_text = response.read().decode("utf-8")
            return json.loads(resp_text) if resp_text else None
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8")
        try:
            parsed = json.loads(err_msg)
            message = parsed.get("message", err_msg)
        except Exception:
            message = err_msg
        raise RuntimeError(f"Cashfree API Error ({e.code}): {message}")
    except Exception as e:
        raise RuntimeError(f"Cashfree connection failed: {str(e)}")


def create_order(
    order_id: str,
    amount: float,
    phone: str = "9999999999",
    customer_name: str = "Guest",
    customer_id: Optional[str] = None,
    notify_url: Optional[str] = None
) -> Dict[str, Any]:
    """
    Creates an order on Cashfree to obtain payment_session_id.
    """
    clean_amount = round(float(amount), 2)
    payload = {
        "order_id": order_id,
        "order_amount": clean_amount,
        "order_currency": "INR",
        "customer_details": {
            "customer_id": customer_id or f"cust_{order_id}",
            "customer_phone": phone,
            "customer_name": customer_name
        }
    }
    if notify_url:
        payload["order_meta"] = {"notify_url": notify_url}

    return _request("orders", method="POST", data=payload)


def create_upi_qr(payment_session_id: str) -> Optional[Dict[str, Any]]:
    """
    Generates dynamic UPI QR code (base64 image or EMV string) for the session.
    """
    payload = {
        "payment_session_id": payment_session_id,
        "payment_method": {
            "upi": {
                "channel": "qrcode"
            }
        }
    }
    return _request("orders/sessions", method="POST", data=payload)


def create_upi_link(payment_session_id: str) -> Optional[Dict[str, Any]]:
    """
    Generates UPI intent links / simulator links for the session.
    """
    payload = {
        "payment_session_id": payment_session_id,
        "payment_method": {
            "upi": {
                "channel": "link"
            }
        }
    }
    return _request("orders/sessions", method="POST", data=payload)


def get_order_status(order_id: str) -> Optional[Dict[str, Any]]:
    """
    Retrieves latest order status from Cashfree (ACTIVE, PAID, EXPIRED).
    """
    try:
        return _request(f"orders/{order_id}", method="GET")
    except Exception as e:
        print(f"[WARN] Failed to fetch Cashfree order status for {order_id}: {e}")
        return None


def get_order_payments(order_id: str) -> List[Dict[str, Any]]:
    """
    Retrieves list of payments for an order, including UTR / bank_reference.
    """
    try:
        res = _request(f"orders/{order_id}/payments", method="GET")
        return res if isinstance(res, list) else []
    except Exception as e:
        print(f"[WARN] Failed to fetch payments for {order_id}: {e}")
        return []


def verify_webhook_signature(timestamp: str, raw_body: str, signature: str) -> bool:
    """
    Verifies Cashfree webhook signature using HMAC-SHA256.
    """
    cfg = get_config()
    secret = cfg["secret_key"]
    if not secret:
        return False

    try:
        payload = f"{timestamp}{raw_body}".encode("utf-8")
        computed = base64.b64encode(
            hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
        ).decode("utf-8")
        return hmac.compare_digest(computed, signature)
    except Exception as e:
        print(f"[WARN] Webhook signature verification error: {e}")
        return False


_SESSION_CACHE: Dict[str, Dict[str, Any]] = {}


def get_or_create_cashfree_session(job_id: str, amount: float, notify_url: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Creates or retrieves an existing Cashfree session for a job and returns QR code and links.
    """
    if not is_cashfree_configured():
        return None
    try:
        clean_amount = round(float(amount), 2)
        amount_cents = int(round(clean_amount * 100))
        order_id = f"job_{job_id}_{amount_cents}"

        # 1. Return from fast in-memory cache if available
        cache_key = f"{job_id}_{amount_cents}"
        if cache_key in _SESSION_CACHE:
            cached = _SESSION_CACHE[cache_key]
            if cached.get("qr_data"):
                return cached

        session_id = None
        try:
            order_res = create_order(order_id, clean_amount, notify_url=notify_url)
            session_id = order_res.get("payment_session_id")
        except RuntimeError as e:
            err_str = str(e).lower()
            if "already present" in err_str or "already exists" in err_str or "409" in err_str:
                existing = get_order_status(order_id)
                session_id = existing.get("payment_session_id") if existing else None
            else:
                print(f"[WARN] Cashfree create order failed: {e}")
                return None

        if not session_id:
            return None

        qr_data = None
        try:
            qr_res = create_upi_qr(session_id)
            if qr_res and "data" in qr_res:
                payload = qr_res["data"].get("payload", {})
                qr_data = payload.get("qrcode") or qr_res["data"].get("url")
        except Exception as e:
            print(f"[WARN] Could not fetch Cashfree QR: {e}")

        links = {}
        try:
            link_res = create_upi_link(session_id)
            if link_res and "data" in link_res:
                links = link_res["data"].get("payload", {})
        except Exception as e:
            print(f"[WARN] Could not fetch Cashfree links: {e}")

        if not links.get("web") and session_id:
            hosted_link = f"https://payments.cashfree.com/order/#{session_id}"
            links["web"] = hosted_link
            if not links.get("default"):
                links["default"] = hosted_link

        cfg = get_config()
        session_info = {
            "order_id": order_id,
            "session_id": session_id,
            "qr_data": qr_data,
            "links": links,
            "env": cfg["env"]
        }
        _SESSION_CACHE[cache_key] = session_info
        return session_info
    except Exception as e:
        print(f"[WARN] Cashfree session creation error: {e}")
        return None
