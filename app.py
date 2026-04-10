# pyright: reportGeneralTypeIssues=false
# pyright: reportArgumentType=false
# pyright: reportOptionalSubscript=false
# pyright: reportAttributeAccessIssue=false
import os
import json
import traceback
from datetime import datetime, date
from functools import wraps

try:
    from dotenv import load_dotenv  # type: ignore[import-untyped]
    load_dotenv()
except ImportError:
    pass

import mysql.connector  # type: ignore[import-untyped]
from flask import (  # type: ignore[import-untyped]
    Flask, render_template, request, redirect,
    url_for, session, jsonify,
)
from werkzeug.security import generate_password_hash, check_password_hash  # type: ignore[import-untyped]
import mpesa_payments

app = Flask(__name__)
app.secret_key = os.getenv(
    "FLASK_SECRET_KEY", "dev-only-change-FLASK_SECRET_KEY-in-production"
)

DB_CONFIG = {
    "host":     os.getenv("DB_HOST",     "localhost"),
    "user":     os.getenv("DB_USER",     "root"),
    "password": os.getenv("DB_PASSWORD", "@Lakika2003"),
    "database": os.getenv("DB_NAME",     "binreportingsystem"),
}

COLLECTION_FEE = 100  # KES per collection


# ─── DB HELPERS ────────────────────────────────────────────────

def _cursor(conn):
    return conn.cursor(dictionary=True)

def get_server_connection():
    return mysql.connector.connect(
        host=DB_CONFIG["host"],
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
    )

def get_db():
    return mysql.connector.connect(**DB_CONFIG, autocommit=True)


# ─── AUTH HELPERS ───────────────────────────────────────────────

def _redirect_for_role(role):
    if role == "resident":
        return redirect(url_for("report", user=session["username"]))
    if role == "manager":
        return redirect(url_for("dashboard"))
    if role == "admin":
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("home"))


def login_required(*allowed_roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if "username" not in session or "role" not in session:
                return redirect(url_for("login"))
            if allowed_roles and session["role"] not in allowed_roles:
                return _redirect_for_role(session["role"])
            return view(*args, **kwargs)
        return wrapped
    return decorator


# ─── DB INIT ────────────────────────────────────────────────────

def init_db():
    db_name = DB_CONFIG["database"]
    srv = get_server_connection()
    srv.autocommit = True
    cur = srv.cursor()
    cur.execute(
        f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
        "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
    )
    cur.close(); srv.close()

    conn = get_db()
    cur  = _cursor(conn)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
          id INT AUTO_INCREMENT PRIMARY KEY,
          username VARCHAR(50) NOT NULL UNIQUE,
          password_hash VARCHAR(255) NOT NULL,
          role ENUM('resident','manager','admin') NOT NULL,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS reports (
          id INT AUTO_INCREMENT PRIMARY KEY,
          resident_id INT NOT NULL,
          status ENUM('Full','Empty') NOT NULL,
          concerns TEXT,
          created_at DATETIME NOT NULL,
          FOREIGN KEY (resident_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
          id INT AUTO_INCREMENT PRIMARY KEY,
          sender_id INT NULL,
          receiver_id INT NOT NULL,
          body TEXT NOT NULL,
          created_at DATETIME NOT NULL,
          FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE SET NULL,
          FOREIGN KEY (receiver_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS assignments (
          id INT AUTO_INCREMENT PRIMARY KEY,
          resident_id INT NOT NULL,
          collection_date DATE NOT NULL,
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY (resident_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB
    """)

    # ── NEW: bills table ────────────────────────────────────────
    # One row per collection assignment = KES 100 owed
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bills (
          id INT AUTO_INCREMENT PRIMARY KEY,
          resident_id INT NOT NULL,
          assignment_id INT NULL,
          amount DECIMAL(10,2) DEFAULT 100.00,
          description VARCHAR(255),
          bill_month VARCHAR(7),
          status ENUM('Unpaid','Paid') DEFAULT 'Unpaid',
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY (resident_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB
    """)

    # ── NEW: payments table ─────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
          id INT AUTO_INCREMENT PRIMARY KEY,
          resident_id INT NOT NULL,
          amount DECIMAL(10,2) NOT NULL,
          phone VARCHAR(20),
          mpesa_code VARCHAR(50),
          checkout_request_id VARCHAR(100),
          bill_ids TEXT,
          status ENUM('Pending','Completed','Failed') DEFAULT 'Pending',
          fail_reason VARCHAR(255),
          created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
          FOREIGN KEY (resident_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB
    """)

    cur.close(); conn.close()


# ─── FORMATTERS ─────────────────────────────────────────────────

def _fmt_dt(value):
    if value is None: return ""
    if isinstance(value, datetime): return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)

def _fmt_date(value):
    if value is None: return None
    if isinstance(value, date): return value.isoformat()
    return str(value)


# ─── USER QUERIES ───────────────────────────────────────────────

def get_user_by_username(username):
    conn = get_db(); cur = _cursor(conn)
    cur.execute("SELECT id, username, password_hash, role FROM users WHERE username = %s", (username,))
    row = cur.fetchone(); cur.close(); conn.close()
    return row

def get_users_admin_dict():
    conn = get_db(); cur = _cursor(conn)
    cur.execute("SELECT username, role FROM users ORDER BY username")
    rows = cur.fetchall(); cur.close(); conn.close()
    return {r["username"]: {"role": r["role"]} for r in rows}

def resident_usernames():
    conn = get_db(); cur = _cursor(conn)
    cur.execute("SELECT username FROM users WHERE role = 'resident' ORDER BY username")
    rows = cur.fetchall(); cur.close(); conn.close()
    return [r["username"] for r in rows]

def user_id_by_username(username):
    row = get_user_by_username(username)
    return row["id"] if row else None


# ─── REPORTS ────────────────────────────────────────────────────

def fetch_reports_for_resident(username):
    conn = get_db(); cur = _cursor(conn)
    cur.execute("""
        SELECT r.status, r.concerns, r.created_at
        FROM reports r JOIN users u ON r.resident_id = u.id
        WHERE u.username = %s ORDER BY r.created_at ASC
    """, (username,))
    rows = cur.fetchall(); cur.close(); conn.close()
    return [{"resident": username, "status": r["status"],
             "concerns": r["concerns"] or "", "date": _fmt_dt(r["created_at"])} for r in rows]

def fetch_reports_all():
    conn = get_db(); cur = _cursor(conn)
    cur.execute("""
        SELECT u.username AS resident, r.status, r.concerns, r.created_at
        FROM reports r JOIN users u ON r.resident_id = u.id
        ORDER BY r.created_at ASC
    """)
    rows = cur.fetchall(); cur.close(); conn.close()
    return [{"resident": r["resident"], "status": r["status"],
             "concerns": r["concerns"] or "", "date": _fmt_dt(r["created_at"])} for r in rows]

def insert_report(username, status, concerns):
    rid = user_id_by_username(username)
    if not rid: return
    conn = get_db(); cur = _cursor(conn)
    cur.execute(
        "INSERT INTO reports (resident_id, status, concerns, created_at) VALUES (%s, %s, %s, %s)",
        (rid, status, concerns, datetime.now())
    )
    cur.close(); conn.close()

def last_report_status_for_resident(username):
    conn = get_db(); cur = _cursor(conn)
    cur.execute("""
        SELECT r.status FROM reports r JOIN users u ON r.resident_id = u.id
        WHERE u.username = %s ORDER BY r.created_at DESC LIMIT 1
    """, (username,))
    row = cur.fetchone(); cur.close(); conn.close()
    return row["status"] if row else "Empty"


# ─── ASSIGNMENTS ─────────────────────────────────────────────────

def fetch_assignments_for_resident(username):
    conn = get_db(); cur = _cursor(conn)
    cur.execute("""
        SELECT a.collection_date
        FROM assignments a JOIN users u ON a.resident_id = u.id
        WHERE u.username = %s ORDER BY a.collection_date ASC, a.id ASC
    """, (username,))
    rows = cur.fetchall(); cur.close(); conn.close()
    return [{"collection_date": _fmt_date(r["collection_date"])} for r in rows]

def fetch_assignments_all():
    conn = get_db(); cur = _cursor(conn)
    cur.execute("""
        SELECT u.username AS resident, a.collection_date
        FROM assignments a JOIN users u ON a.resident_id = u.id
        ORDER BY a.collection_date ASC
    """)
    rows = cur.fetchall(); cur.close(); conn.close()
    return [{"resident": r["resident"],
             "collection_date": _fmt_date(r["collection_date"])} for r in rows]

def last_collection_for_resident(username):
    conn = get_db(); cur = _cursor(conn)
    cur.execute("""
        SELECT a.collection_date
        FROM assignments a JOIN users u ON a.resident_id = u.id
        WHERE u.username = %s ORDER BY a.collection_date DESC LIMIT 1
    """, (username,))
    row = cur.fetchone(); cur.close(); conn.close()
    return _fmt_date(row["collection_date"]) if row else None

def insert_assignment(username, collection_date):
    """Create assignment AND automatically create a KES 100 bill."""
    rid = user_id_by_username(username)
    if not rid: return
    conn = get_db(); cur = _cursor(conn)

    cur.execute(
        "INSERT INTO assignments (resident_id, collection_date) VALUES (%s, %s)",
        (rid, collection_date)
    )
    assignment_id = cur.lastrowid
    bill_month    = collection_date[:7]  # YYYY-MM

    cur.execute(
        """
        INSERT INTO bills (resident_id, assignment_id, amount, description, bill_month, status)
        VALUES (%s, %s, %s, %s, %s, 'Unpaid')
        """,
        (rid, assignment_id, COLLECTION_FEE,
         f"Bin collection on {collection_date}", bill_month)
    )
    cur.close(); conn.close()


# ─── MESSAGES ────────────────────────────────────────────────────

def fetch_messages_for_resident(username):
    conn = get_db(); cur = _cursor(conn)
    cur.execute("""
        SELECT m.body, m.created_at,
               COALESCE(s.username, 'System') AS sender
        FROM messages m
        JOIN users r ON m.receiver_id = r.id
        LEFT JOIN users s ON m.sender_id = s.id
        WHERE r.username = %s ORDER BY m.created_at ASC
    """, (username,))
    rows = cur.fetchall(); cur.close(); conn.close()
    return [{"text": r["body"], "time": _fmt_dt(r["created_at"]),
             "sender": r["sender"]} for r in rows]

def fetch_messages_all():
    conn = get_db(); cur = _cursor(conn)
    cur.execute("""
        SELECT recv.username AS receiver, m.body, m.created_at,
               COALESCE(sndr.username, 'System') AS sender
        FROM messages m
        JOIN users recv ON m.receiver_id = recv.id
        LEFT JOIN users sndr ON m.sender_id = sndr.id
        ORDER BY m.created_at ASC
    """)
    rows = cur.fetchall(); cur.close(); conn.close()
    return [{"receiver": r["receiver"], "sender": r["sender"],
             "text": r["body"], "time": _fmt_dt(r["created_at"])} for r in rows]

def insert_message(sender_id, receiver_username, body):
    rid = user_id_by_username(receiver_username)
    if not rid: return
    conn = get_db(); cur = _cursor(conn)
    cur.execute(
        "INSERT INTO messages (sender_id, receiver_id, body, created_at) VALUES (%s,%s,%s,%s)",
        (sender_id, rid, body, datetime.now())
    )
    cur.close(); conn.close()


# ─── BILLING HELPERS ─────────────────────────────────────────────

def fetch_bills_for_resident(resident_id: int) -> list:
    """Return all bills for a resident, newest first."""
    conn = get_db(); cur = _cursor(conn)
    cur.execute("""
        SELECT id, amount, description, bill_month, status, created_at
        FROM bills WHERE resident_id = %s ORDER BY created_at DESC
    """, (resident_id,))
    rows = cur.fetchall(); cur.close(); conn.close()
    return [{"id": r["id"], "amount": float(r["amount"]),
             "description": r["description"], "bill_month": r["bill_month"],
             "status": r["status"], "date": _fmt_dt(r["created_at"])} for r in rows]

def fetch_outstanding_bills(resident_id: int) -> list:
    """Return unpaid bills ordered oldest-first (for sequential settlement)."""
    conn = get_db(); cur = _cursor(conn)
    cur.execute("""
        SELECT id, amount FROM bills
        WHERE resident_id = %s AND status = 'Unpaid'
        ORDER BY created_at ASC
    """, (resident_id,))
    rows = cur.fetchall(); cur.close(); conn.close()
    return [{"id": r["id"], "amount": float(r["amount"])} for r in rows]

def total_outstanding(resident_id: int) -> float:
    conn = get_db(); cur = _cursor(conn)
    cur.execute(
        "SELECT COALESCE(SUM(amount),0) AS total FROM bills WHERE resident_id=%s AND status='Unpaid'",
        (resident_id,)
    )
    row = cur.fetchone(); cur.close(); conn.close()
    return float(row["total"])

def monthly_outstanding(resident_id: int, year_month: str) -> float:
    """Total unpaid bills for a given YYYY-MM month."""
    conn = get_db(); cur = _cursor(conn)
    cur.execute(
        "SELECT COALESCE(SUM(amount),0) AS total FROM bills "
        "WHERE resident_id=%s AND bill_month=%s AND status='Unpaid'",
        (resident_id, year_month)
    )
    row = cur.fetchone(); cur.close(); conn.close()
    return float(row["total"])

def fetch_payments_for_resident(resident_id: int) -> list:
    """Return all payments for a resident."""
    conn = get_db(); cur = _cursor(conn)
    cur.execute("""
        SELECT id, amount, phone, mpesa_code, status, created_at
        FROM payments WHERE resident_id = %s ORDER BY created_at DESC
    """, (resident_id,))
    rows = cur.fetchall(); cur.close(); conn.close()
    return [{"id": r["id"], "amount": float(r["amount"]),
             "phone": r["phone"], "mpesa_code": r["mpesa_code"] or "—",
             "status": r["status"], "date": _fmt_dt(r["created_at"])} for r in rows]

def mark_bills_paid(bill_ids: list, conn=None):
    """Mark specific bill IDs as Paid."""
    if not bill_ids: return
    should_close = conn is None
    if conn is None: conn = get_db()
    cur = conn.cursor()
    placeholders = ",".join(["%s"] * len(bill_ids))
    cur.execute(f"UPDATE bills SET status='Paid' WHERE id IN ({placeholders})", bill_ids)
    cur.close()
    if should_close: conn.close()

def settle_payment(resident_id: int, amount_paid: float, conn=None):
    """
    Apply a payment to unpaid bills from oldest to newest.
    Returns list of bill IDs that were fully paid.
    """
    should_close = conn is None
    if conn is None: conn = get_db()

    cur = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT id, amount FROM bills
        WHERE resident_id=%s AND status='Unpaid'
        ORDER BY created_at ASC
    """, (resident_id,))
    unpaid = cur.fetchall()
    cur.close()

    settled = []
    remaining = amount_paid
    for bill in unpaid:
        if remaining <= 0: break
        if remaining >= float(bill["amount"]):
            remaining -= float(bill["amount"])
            settled.append(bill["id"])

    if settled:
        mark_bills_paid(settled, conn=conn)

    if should_close: conn.close()
    return settled


# ─── REPORT SUMMARY ──────────────────────────────────────────────

def report_summary_counts():
    conn = get_db(); cur = _cursor(conn)
    cur.execute("SELECT COUNT(*) AS total FROM reports")
    total = cur.fetchone()["total"]
    cur.execute("SELECT COUNT(*) AS fulls FROM reports WHERE status = 'Full'")
    full_bins = cur.fetchone()["fulls"]
    cur.close(); conn.close()
    return total, full_bins, total - full_bins


# ─────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"]
        password = request.form["password"]
        row = get_user_by_username(username)
        if row and check_password_hash(row["password_hash"], password):
            session.clear()
            session["username"] = username
            session["role"] = row["role"]
            role = row["role"]
            if role == "resident": return redirect(url_for("report", user=username))
            if role == "manager":  return redirect(url_for("dashboard"))
            if role == "admin":    return redirect(url_for("admin_dashboard"))
        return render_template("login.html", error="Invalid username or password!")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/report/<user>", methods=["GET", "POST"])
def report(user):
    if "username" not in session or "role" not in session:
        return redirect(url_for("login"))
    if session["role"] != "resident" or session["username"] != user:
        return _redirect_for_role(session["role"])
    u = get_user_by_username(user)
    if not u or u["role"] != "resident":
        return redirect(url_for("login"))

    if request.method == "POST":
        full_bin = "Yes" if "full_bin" in request.form else "No"
        concerns = request.form.get("concerns", "")
        insert_report(user, "Full" if full_bin == "Yes" else "Empty", concerns)

    resident_id   = u["id"]
    user_reports  = fetch_reports_for_resident(user)
    user_messages = fetch_messages_for_resident(user)
    user_assigns  = fetch_assignments_for_resident(user)
    last_collect  = last_collection_for_resident(user)

    # Billing data
    bills        = fetch_bills_for_resident(resident_id)
    payments     = fetch_payments_for_resident(resident_id)
    outstanding  = total_outstanding(resident_id)
    this_month   = datetime.now().strftime("%Y-%m")
    month_owed   = monthly_outstanding(resident_id, this_month)

    return render_template(
        "resident_report.html",
        user=user,
        reports=user_reports,
        messages=user_messages,
        assignments=user_assigns,
        last_collection=last_collect,
        bills=bills,
        payments=payments,
        outstanding=outstanding,
        month_owed=month_owed,
        this_month=this_month,
        collection_fee=COLLECTION_FEE,
    )


# ─── PAYMENT: INITIATE STK PUSH ──────────────────────────────────

@app.route("/pay", methods=["POST"])
def pay():
    if "username" not in session or session["role"] != "resident":
        return jsonify({"ok": False, "message": "Unauthorized"}), 403

    phone     = request.form.get("phone", "").strip()
    pay_type  = request.form.get("pay_type", "outstanding")  # "one" | "outstanding"

    if not phone:
        return jsonify({"ok": False, "message": "Phone number is required."})

    user = get_user_by_username(session["username"])
    if not user:
        return jsonify({"ok": False, "message": "User not found."})

    resident_id = user["id"]
    unpaid      = fetch_outstanding_bills(resident_id)

    if not unpaid:
        return jsonify({"ok": False, "message": "No outstanding bills to pay!"})

    if pay_type == "one":
        # Pay exactly one (oldest) collection: KES 100
        bills_to_pay = [unpaid[0]]
        amount       = COLLECTION_FEE
    else:
        # Pay everything outstanding
        bills_to_pay = unpaid
        amount       = sum(b["amount"] for b in unpaid)

    bill_ids = [b["id"] for b in bills_to_pay]

    conn   = get_db()
    result = mpesa_payments.trigger_stk_push(
        phone=phone,
        amount=amount,
        resident_id=resident_id,
        bill_ids=bill_ids,
        db_conn=conn
    )
    conn.close()
    return jsonify(result)


# ─── PAYMENT: POLL STATUS ────────────────────────────────────────

@app.route("/mpesa/payment_status/<checkout_id>")
def payment_status(checkout_id):
    conn = get_db(); cur = _cursor(conn)
    cur.execute(
        "SELECT status, mpesa_code, amount FROM payments WHERE checkout_request_id=%s",
        (checkout_id,)
    )
    row = cur.fetchone(); cur.close(); conn.close()
    if not row:
        return jsonify({"status": "pending"})
    return jsonify({"status": row["status"], "mpesa_code": row["mpesa_code"],
                    "amount": float(row["amount"] or 0)})


# ─── PAYMENT: SAFARICOM CALLBACK ─────────────────────────────────

@app.route("/mpesa/callback", methods=["POST"])
def mpesa_callback():
    raw = request.get_data(as_text=True)
    print(f"\n{'='*60}\n📩 M-PESA CALLBACK — {datetime.now().strftime('%H:%M:%S')}\n{raw[:400]}\n{'='*60}")

    try:
        data        = json.loads(raw) if raw else {}
        stk         = data.get("Body", {}).get("stkCallback", {})
        result_code = stk.get("ResultCode")
        checkout_id = stk.get("CheckoutRequestID", "")

        if result_code == 0:
            # ── Payment successful ───────────────────────────────
            receipt = "UNKNOWN"; amount_paid = 0.0; phone = "UNKNOWN"
            for item in stk.get("CallbackMetadata", {}).get("Item", []):
                name = item.get("Name"); val = item.get("Value")
                if name == "MpesaReceiptNumber": receipt     = str(val)
                if name == "Amount":             amount_paid = float(val)
                if name == "PhoneNumber":        phone       = str(val)

            print(f"✅ PAID | {receipt} | KES {amount_paid} | {phone}")

            conn = get_db()
            cur  = conn.cursor(dictionary=True)

            # Update payment record
            cur.execute(
                "UPDATE payments SET status='Completed', mpesa_code=%s WHERE checkout_request_id=%s",
                (receipt, checkout_id)
            )
            print(f"   Payments updated: {cur.rowcount}")

            # Fetch the payment row to know resident + bill_ids
            cur.execute(
                "SELECT resident_id, amount, bill_ids FROM payments WHERE checkout_request_id=%s",
                (checkout_id,)
            )
            prow = cur.fetchone()
            cur.close()

            if prow:
                resident_id = prow["resident_id"]
                bill_ids_str = prow.get("bill_ids") or ""
                if bill_ids_str:
                    # Mark specific bills stored at push time
                    bill_ids = [int(x) for x in bill_ids_str.split(",") if x.strip()]
                    mark_bills_paid(bill_ids, conn=conn)
                    print(f"   Bills marked paid: {bill_ids}")
                else:
                    # Fallback: settle by amount
                    settled = settle_payment(resident_id, amount_paid, conn=conn)
                    print(f"   Bills settled by amount: {settled}")

            conn.close()

        else:
            # ── Payment failed / cancelled ───────────────────────
            reason = stk.get("ResultDesc", "Cancelled or insufficient funds")
            print(f"❌ FAILED: {reason}")
            conn = get_db(); cur = conn.cursor()
            cur.execute(
                "UPDATE payments SET status='Failed', fail_reason=%s WHERE checkout_request_id=%s",
                (reason[:255], checkout_id)
            )
            cur.close(); conn.close()

    except Exception:
        print(f"🔥 CALLBACK CRASHED:\n{traceback.format_exc()}")

    # Always return 200 so Safaricom doesn't retry endlessly
    return jsonify({"ResultCode": 0, "ResultDesc": "Accepted"})


# ─── MANAGER ROUTES ──────────────────────────────────────────────

@app.route("/dashboard")
@login_required("manager")
def dashboard():
    resident_status = []
    for name in resident_usernames():
        last_status = last_report_status_for_resident(name)
        uid = user_id_by_username(name)
        resident_status.append({
            "name":            name,
            "status":          last_status,
            "color":           "red" if last_status == "Full" else "green",
            "last_collection": last_collection_for_resident(name),
            "outstanding":     total_outstanding(uid) if uid else 0,
        })

    return render_template(
        "manager_dashboard.html",
        resident_status=resident_status,
        reports=fetch_reports_all(),
        assignments=fetch_assignments_all(),
        messages=fetch_messages_all(),
    )


@app.route("/assign", methods=["POST"])
@login_required("manager")
def assign():
    resident        = request.form["resident"]
    collection_date = request.form["collection_date"]
    insert_assignment(resident, collection_date)   # also creates bill
    return redirect(url_for("dashboard"))


@app.route("/reply", methods=["POST"])
@login_required("manager")
def reply():
    resident     = request.form["resident"]
    message_text = request.form["message"]
    sender_id    = user_id_by_username(session["username"])
    insert_message(sender_id, resident, message_text)
    return redirect(url_for("dashboard"))


@app.route("/broadcast", methods=["POST"])
@login_required("manager")
def broadcast():
    message_text = request.form["message"]
    sender_id    = user_id_by_username(session["username"])
    for name in resident_usernames():
        insert_message(sender_id, name, message_text)
    return redirect(url_for("dashboard"))


@app.route("/message/<user>")
def message(user):
    if "username" not in session or "role" not in session:
        return redirect(url_for("login"))
    if session["role"] != "resident" or session["username"] != user:
        return _redirect_for_role(session["role"])
    u = get_user_by_username(user)
    if not u or u["role"] != "resident":
        return redirect(url_for("login"))
    return render_template("message.html",
                           messages=fetch_messages_for_resident(user), user=user)


@app.route("/resident/<resident>")
@login_required("manager")
def resident_profile(resident):
    u = get_user_by_username(resident)
    if not u or u["role"] != "resident":
        return redirect(url_for("dashboard"))

    resident_id = u["id"]
    return render_template(
        "resident_profile.html",
        resident=resident,
        reports=fetch_reports_for_resident(resident),
        messages=fetch_messages_for_resident(resident),
        assignments=fetch_assignments_for_resident(resident),
        last_collection=last_collection_for_resident(resident),
        bills=fetch_bills_for_resident(resident_id),
        payments=fetch_payments_for_resident(resident_id),
        outstanding=total_outstanding(resident_id),
    )


# ─── ADMIN ROUTES ────────────────────────────────────────────────

# ─── ADMIN ROUTES ────────────────────────────────────────────────

@app.route("/admin_dashboard", methods=["GET", "POST"])
@login_required("admin")
def admin_dashboard():
    error_msg = None  # 1. Variable to hold our error message

    if request.method == "POST":
        action   = request.form.get("action")
        username = request.form.get("username")
        password = request.form.get("password")
        role     = request.form.get("role")

        if action == "add" and username and role:
            plain = password or "1234"
            conn = get_db(); cur = _cursor(conn)
            try:
                cur.execute(
                    "INSERT INTO users (username, password_hash, role) VALUES (%s, %s, %s)",
                    (username, generate_password_hash(plain), role),
                )
            except mysql.connector.Error as err:
                # 2. Catch the duplicate entry error (1062)
                if err.errno == 1062:
                    error_msg = f"The username '{username}' is already taken!"
                else:
                    error_msg = "A database error occurred."
            finally:
                cur.close(); conn.close()

        elif action == "delete" and username and username != "admin":
            conn = get_db(); cur = _cursor(conn)
            cur.execute("DELETE FROM users WHERE username=%s AND username<>'admin'", (username,))
            cur.close(); conn.close()

    return render_template(
        "admin_dashboard.html",
        error=error_msg,  # 3. Pass the error to the frontend
        users=get_users_admin_dict(),
        reports=fetch_reports_all(),
        messages=fetch_messages_all(),
        assignments=fetch_assignments_all(),
    )

@app.route("/admin_view/<username>")
@login_required("admin")
def admin_view(username):
    u = get_user_by_username(username)
    if not u:
        return redirect(url_for("admin_dashboard"))
    resident_id = u["id"]
    return render_template(
        "admin_view_user.html",
        username=username,
        role=u["role"],
        reports=[r for r in fetch_reports_all() if r["resident"] == username],
        messages=[m for m in fetch_messages_all() if m["receiver"] == username],
        assignments=[a for a in fetch_assignments_all() if a["resident"] == username],
        last_collection=last_collection_for_resident(username),
        bills=fetch_bills_for_resident(resident_id),
        payments=fetch_payments_for_resident(resident_id),
        outstanding=total_outstanding(resident_id),
    )


@app.route("/generate_report")
@login_required("manager")
def generate_report():
    total_reports, full_bins, empty_bins = report_summary_counts()
    return render_template(
        "generate_report.html",
        total_reports=total_reports,
        full_bins=full_bins,
        empty_bins=empty_bins,
        timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    try:
        init_db()
    except mysql.connector.Error as exc:
        print(f"Database error: {exc}")
        raise SystemExit(1) from exc
    app.run(debug=True, port=5000)