"""
Casse Cluub backend: Flask + SQLite REST API.

Serves the storefront (public/index.html) and a JSON API under /api.
Run:  python app.py   ->  http://127.0.0.1:5000
"""
import json
import os
import re
import secrets
import sqlite3
import time
from functools import wraps

from flask import Flask, g, jsonify, request, send_from_directory
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DATABASE", os.path.join(BASE_DIR, "casse.db"))
SECRET_KEY = os.environ.get("SECRET_KEY") or secrets.token_hex(32)  # random per run if unset
TOKEN_MAX_AGE = 60 * 60 * 12  # admin session lasts 12 hours

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024  # product images are base64 inside JSON
signer = URLSafeTimedSerializer(SECRET_KEY, salt="casse-admin")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS products (
  id TEXT PRIMARY KEY,
  position INTEGER NOT NULL,
  data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL,
  status TEXT NOT NULL,
  total INTEGER NOT NULL,
  data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscribers (
  email TEXT PRIMARY KEY,
  source TEXT,
  at INTEGER
);
CREATE TABLE IF NOT EXISTS kv (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PHONE_RE = re.compile(r"^(\+?91)?[6-9]\d{9}$")
PIN_RE = re.compile(r"^[1-9]\d{5}$")
ORDER_STATES = {"awaiting_payment", "paid", "packed", "shipped", "cancelled"}


# ---------------------------------------------------------------- database
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    user = os.environ.get("ADMIN_USER", "admin")
    password = os.environ.get("ADMIN_PASSWORD", "admin123")
    if not conn.execute("SELECT 1 FROM users WHERE username=?", (user,)).fetchone():
        conn.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            (user, generate_password_hash(password)),
        )
        if password == "admin123":
            print("WARNING: using default admin password 'admin123'. Set ADMIN_PASSWORD in .env.")
    conn.commit()
    conn.close()


def kv_get(key, default):
    row = db().execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def kv_set(key, value):
    db().execute(
        "INSERT INTO kv (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )


def load_products():
    rows = db().execute("SELECT data FROM products ORDER BY position").fetchall()
    return [json.loads(r["data"]) for r in rows]


# -------------------------------------------------------------------- auth
def current_admin():
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    try:
        return signer.loads(header[7:], max_age=TOKEN_MAX_AGE).get("u")
    except (BadSignature, SignatureExpired):
        return None


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_admin():
            return jsonify(error="Admin login required."), 401
        return fn(*args, **kwargs)

    return wrapper


def bad(msg, code=400):
    return jsonify(error=msg), code


# ------------------------------------------------------------------ routes
@app.get("/api/health")
def health():
    return jsonify(ok=True, time=int(time.time()))


@app.post("/api/login")
def login():
    body = request.get_json(silent=True) or {}
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    row = db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    if not row or not check_password_hash(row["password_hash"], password):
        return bad("Wrong username or password.", 401)
    return jsonify(token=signer.dumps({"u": row["username"]}), user=row["username"])


@app.get("/api/me")
@admin_required
def me():
    return jsonify(user=current_admin())


@app.get("/api/state")
def get_state():
    """Everything the storefront needs. Orders and subscribers only for the admin."""
    products = load_products()
    if not products:
        return jsonify(empty=True)  # fresh database: frontend falls back to its demo data
    state = {
        "products": products,
        "collections": kv_get("collections", []),
        "reviews": kv_get("reviews", []),
        "settings": kv_get("settings", {}),
        "orders": [],
        "subscribers": [],
        "seedV": kv_get("seedV", 2),
    }
    if current_admin():
        rows = db().execute("SELECT data FROM orders ORDER BY created_at DESC").fetchall()
        state["orders"] = [json.loads(r["data"]) for r in rows]
        subs = db().execute("SELECT email, source, at FROM subscribers ORDER BY at DESC").fetchall()
        state["subscribers"] = [dict(s) for s in subs]
    return jsonify(state)


@app.put("/api/state")
@admin_required
def put_state():
    """Admin saves catalogue, collections, reviews and settings. Order statuses are merged."""
    body = request.get_json(silent=True) or {}
    products = body.get("products")
    if not isinstance(products, list) or not products:
        return bad("products must be a non-empty list.")
    if any(not isinstance(p, dict) or not p.get("id") for p in products):
        return bad("Every product needs an id.")
    conn = db()
    with conn:
        conn.execute("DELETE FROM products")
        conn.executemany(
            "INSERT OR REPLACE INTO products (id, position, data) VALUES (?, ?, ?)",
            [(p["id"], i, json.dumps(p)) for i, p in enumerate(products)],
        )
        for key in ("collections", "reviews", "settings", "seedV"):
            if key in body:
                kv_set(key, body[key])
        for o in body.get("orders", []) or []:
            row = conn.execute("SELECT data FROM orders WHERE id=?", (o.get("id"),)).fetchone()
            if not row or o.get("status") not in ORDER_STATES:
                continue
            data = json.loads(row["data"])
            data["status"] = o["status"]
            data["payment"] = o.get("payment", data.get("payment"))
            data["stockApplied"] = bool(o.get("stockApplied"))
            conn.execute(
                "UPDATE orders SET status=?, data=? WHERE id=?",
                (data["status"], json.dumps(data), data["id"]),
            )
    return jsonify(ok=True, products=len(products))


@app.get("/api/orders")
@admin_required
def list_orders():
    rows = db().execute("SELECT data FROM orders ORDER BY created_at DESC").fetchall()
    return jsonify([json.loads(r["data"]) for r in rows])


@app.patch("/api/orders/<order_id>")
@admin_required
def update_order(order_id):
    status = (request.get_json(silent=True) or {}).get("status")
    if status not in ORDER_STATES:
        return bad("Unknown status.")
    row = db().execute("SELECT data FROM orders WHERE id=?", (order_id,)).fetchone()
    if not row:
        return bad("Order not found.", 404)
    data = json.loads(row["data"])
    data["status"] = status
    if status in ("paid", "packed", "shipped"):
        data["payment"]["status"] = "paid"
    with db():
        db().execute("UPDATE orders SET status=?, data=? WHERE id=?", (status, json.dumps(data), order_id))
    return jsonify(data)


@app.post("/api/orders")
def create_order():
    """Public checkout. Prices are recomputed here: the browser's totals are never trusted."""
    body = request.get_json(silent=True) or {}
    customer = body.get("customer") or {}
    address = body.get("address") or {}
    items_in = body.get("items") or []
    shipping_kind = "express" if body.get("shipping") == "express" else "standard"
    provider = body.get("provider") if body.get("provider") in ("cod", "razorpay", "stripe") else "cod"

    errors = {}
    if len(str(customer.get("name", "")).strip()) < 2:
        errors["name"] = "Enter your full name."
    if not EMAIL_RE.match(str(customer.get("email", "")).strip()):
        errors["email"] = "Enter a valid email address."
    if not PHONE_RE.match(re.sub(r"[\s-]", "", str(customer.get("phone", "")))):
        errors["phone"] = "Enter a 10-digit mobile number."
    if len(str(address.get("line", "")).strip()) < 5:
        errors["address"] = "Enter your street address."
    if len(str(address.get("city", "")).strip()) < 2:
        errors["city"] = "Enter your city."
    if not address.get("state"):
        errors["state"] = "Choose your state."
    if not PIN_RE.match(str(address.get("pincode", "")).strip()):
        errors["pincode"] = "Enter a 6-digit pincode."
    if errors:
        return jsonify(error="Please check your details.", fields=errors), 400
    if not isinstance(items_in, list) or not items_in:
        return bad("Your bag is empty.")

    products = {p["id"]: p for p in load_products()}
    settings = kv_get("settings", {})
    models = settings.get("models") or []
    items, subtotal, qty_total = [], 0, 0
    for it in items_in[:50]:
        p = products.get(str(it.get("id")))
        try:
            qty = int(it.get("qty", 1))
        except (TypeError, ValueError):
            qty = 0
        if not p or p.get("status") != "active":
            return bad("An item in your bag is no longer available.")
        if not 1 <= qty <= 10:
            return bad("Quantity must be between 1 and 10.")
        if qty > int(p.get("stock", 0) or 0):
            return bad(f"Sorry, only {p.get('stock', 0)} left of {p['name']}.")
        model = str(it.get("model", ""))
        if models and model not in models:
            return bad(f"Choose a phone model for {p['name']}.")
        unit = p["salePrice"] if p.get("salePrice") and p["salePrice"] < p["price"] else p["price"]
        variant = next((v for v in p.get("variants") or [] if v.get("name") == it.get("variant")), None)
        unit += int(variant.get("priceDelta", 0)) if variant else 0
        subtotal += unit * qty
        qty_total += qty
        items.append({
            "id": p["id"], "name": p["name"], "sku": p.get("sku", ""),
            "model": model, "variant": variant["name"] if variant else "",
            "qty": qty, "unit": unit,
        })

    discount, promo_code = 0, ""
    code = str(body.get("promo") or "").upper()
    promo = next((x for x in settings.get("promos", []) if str(x.get("code", "")).upper() == code), None)
    if promo and subtotal >= float(promo.get("min") or 0):
        promo_code = promo["code"]
        value = float(promo.get("value") or 0)
        discount = round(subtotal * value / 100) if promo.get("type") == "percent" else min(subtotal, int(value))

    if shipping_kind == "express":
        shipping = int(settings.get("expressRate", 199))
    else:
        free = qty_total >= int(settings.get("freeShipQty", 2))
        shipping = 0 if free else int(settings.get("standardRate", 79))
    total = max(0, subtotal - discount) + shipping

    order = {
        "id": "CC-" + secrets.token_hex(3).upper(),
        "createdAt": int(time.time() * 1000),
        "customer": {k: str(customer.get(k, "")).strip() for k in ("name", "email", "phone")},
        "address": {
            "line": str(address.get("line", "")).strip(), "city": str(address.get("city", "")).strip(),
            "state": str(address.get("state", "")), "pincode": str(address.get("pincode", "")).strip(),
        },
        "shipping": shipping_kind, "promo": promo_code, "items": items,
        "totals": {"sub": subtotal, "disc": discount, "shipping": shipping, "total": total},
        "payment": {"provider": provider, "status": "unpaid"},
        "status": "awaiting_payment",
    }
    with db():
        db().execute(
            "INSERT INTO orders (id, created_at, status, total, data) VALUES (?, ?, ?, ?, ?)",
            (order["id"], order["createdAt"], order["status"], total, json.dumps(order)),
        )
    return jsonify(order=order), 201


@app.post("/api/subscribers")
def add_subscriber():
    body = request.get_json(silent=True) or {}
    email = str(body.get("email", "")).strip().lower()
    if not EMAIL_RE.match(email):
        return bad("Enter a valid email address.")
    with db():
        db().execute(
            "INSERT OR IGNORE INTO subscribers (email, source, at) VALUES (?, ?, ?)",
            (email, str(body.get("source", ""))[:40], int(time.time() * 1000)),
        )
    return jsonify(ok=True), 201


# --------------------------------------------------------------- frontend
@app.get("/")
def index():
    return send_from_directory(os.path.join(BASE_DIR, "public"), "index.html")


@app.errorhandler(413)
def too_big(_e):
    return bad("That upload is too large.", 413)


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5000)), debug=os.environ.get("FLASK_DEBUG") == "1")
