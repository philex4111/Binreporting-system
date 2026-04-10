# mpesa_payments.py
# M-Pesa STK Push module for Umoja Estate Bin Reporting System
# Adapted from mpesa_api.py — no pos_engine dependency

import requests  # type: ignore[import-untyped]
import base64
from datetime import datetime
from requests.auth import HTTPBasicAuth  # type: ignore[import-untyped]

# ─────────────────────────────────────────
# DARAJA SANDBOX CREDENTIALS (same as your duka_pos project)
# ─────────────────────────────────────────
CONSUMER_KEY    = "vdGmXrEieuguC0A1pE0XhTpLOdXWhNms8lgQDWvqDX28ANYl".strip()
CONSUMER_SECRET = "KxP6YjGbI9AvJGKK7sUrAjwsTNjeHXSR5uEFnUbtbLlA1bsDXnl4vnGFEUFZ9zXn".strip()
SHORTCODE       = "174379"
PASSKEY         = "bfb279f9aa9bdbcf158e97dd71a467cd2e0c893059b10f78e6b72ada1ed2c919"

# ─────────────────────────────────────────
# NGROK CALLBACK URL — update when Ngrok restarts
# ─────────────────────────────────────────
NGROK_BASE_URL = "https://subconjunctively-brainy-norine.ngrok-free.dev"
CALLBACK_URL   = f"{NGROK_BASE_URL}/mpesa/callback"

COLLECTION_FEE = 100  # KES per collection


def get_access_token() -> str | None:
    """Fetch a fresh OAuth token from Safaricom."""
    url = "https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials"
    try:
        resp = requests.get(
            url,
            auth=HTTPBasicAuth(CONSUMER_KEY, CONSUMER_SECRET),
            timeout=15
        )
        if resp.status_code == 200:
            return resp.json().get("access_token")
        print(f"❌ Auth failed: {resp.status_code} — {resp.text}")
        return None
    except Exception as e:
        print(f"❌ Auth exception: {e}")
        return None


def format_phone(phone: str) -> str:
    """Normalize any Kenyan phone format to 2547XXXXXXXX."""
    phone = str(phone).strip().replace(" ", "").replace("-", "")
    if phone.startswith("+"):
        phone = phone[1:]
    if phone.startswith("0"):
        phone = "254" + phone[1:]
    if phone.startswith("7") or phone.startswith("1"):
        phone = "254" + phone
    return phone


def trigger_stk_push(phone: str, amount: float, resident_id: int,
                     bill_ids: list, db_conn) -> dict:
    """
    Send an M-Pesa STK push and save a Pending payment record.

    Args:
        phone       : resident's phone number (any Kenyan format)
        amount      : KES amount to charge
        resident_id : users.id of the resident
        bill_ids    : list of bill IDs this payment covers
        db_conn     : active MySQL connection (autocommit=True)

    Returns dict with keys: ok, checkout_id, message
    """
    token = get_access_token()
    if not token:
        return {
            "ok": False,
            "message": "❌ Failed to connect to Safaricom. Check Consumer Key & Secret.",
            "checkout_id": None,
        }

    formatted = format_phone(phone)
    timestamp  = datetime.now().strftime("%Y%m%d%H%M%S")
    password   = base64.b64encode(
        (SHORTCODE + PASSKEY + timestamp).encode()
    ).decode()

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }
    payload = {
        "BusinessShortCode": SHORTCODE,
        "Password":          password,
        "Timestamp":         timestamp,
        "TransactionType":   "CustomerPayBillOnline",
        "Amount":            int(amount),
        "PartyA":            formatted,
        "PartyB":            SHORTCODE,
        "PhoneNumber":       formatted,
        "CallBackURL":       CALLBACK_URL,
        "AccountReference":  "Umoja Estate",
        "TransactionDesc":   f"Bin Collection Fee KES {int(amount)}",
    }

    try:
        print(f"🚀 STK Push → {formatted} | KES {int(amount)}")
        resp   = requests.post(
            "https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest",
            json=payload, headers=headers, timeout=60
        )
        result = resp.json() if resp.content else {}

        if "CheckoutRequestID" in result:
            checkout_id = result["CheckoutRequestID"]
            bill_ids_str = ",".join(str(b) for b in bill_ids)

            cur = db_conn.cursor()
            cur.execute(
                """
                INSERT INTO payments
                    (resident_id, amount, phone, checkout_request_id, bill_ids, status)
                VALUES (%s, %s, %s, %s, %s, 'Pending')
                """,
                (resident_id, amount, formatted, checkout_id, bill_ids_str)
            )
            cur.close()

            print(f"✅ STK accepted. Checkout ID: {checkout_id}")
            return {
                "ok":          True,
                "checkout_id": checkout_id,
                "phone":       formatted,
                "amount":      int(amount),
                "message":     (
                    f"STK Push sent to {formatted}. "
                    f"Please enter your M-Pesa PIN on your phone."
                ),
            }

        else:
            err = result.get("errorMessage", result.get("ResultDesc", "Unknown error"))
            print(f"❌ Safaricom rejected: {err}")
            return {"ok": False, "message": f"M-Pesa Error: {err}", "checkout_id": None}

    except requests.exceptions.ReadTimeout:
        # Sandbox is slow but the push may still arrive on the phone
        return {
            "ok":          True,
            "checkout_id": None,
            "phone":       formatted,
            "amount":      int(amount),
            "message": (
                "Safaricom sandbox is slow — the prompt may still appear on "
                f"{formatted}. Ask the resident to check their phone."
            ),
        }
    except Exception as e:
        return {"ok": False, "message": f"System Error: {e}", "checkout_id": None}