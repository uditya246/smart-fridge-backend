"""
Smart Fridge Backend — FastAPI
Handles inventory, expiry checks, Blinkit search, and push notifications via Firebase.
"""
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import json, os, datetime, schedule, threading, time
import firebase_admin
from firebase_admin import credentials, messaging

app = FastAPI(title="Smart Fridge API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

INVENTORY_FILE = "inventory.json"

# ── Firebase setup ────────────────────────────────────────────────────────────
# Download your serviceAccountKey.json from Firebase Console →
# Project Settings → Service Accounts → Generate new private key
FIREBASE_CRED = "serviceAccountKey.json"
if os.path.exists(FIREBASE_CRED):
    cred = credentials.Certificate(FIREBASE_CRED)
    firebase_admin.initialize_app(cred)
    FIREBASE_READY = True
else:
    FIREBASE_READY = False
    print("WARNING: serviceAccountKey.json not found. Notifications disabled.")

# FCM tokens of registered devices (stored in a simple file)
TOKENS_FILE = "fcm_tokens.json"

def load_tokens():
    if os.path.exists(TOKENS_FILE):
        with open(TOKENS_FILE) as f:
            return json.load(f)
    return []

def save_tokens(tokens):
    with open(TOKENS_FILE, "w") as f:
        json.dump(tokens, f)

# ── Inventory helpers ─────────────────────────────────────────────────────────
def load_inv():
    if os.path.exists(INVENTORY_FILE):
        with open(INVENTORY_FILE) as f:
            return json.load(f)
    return {}

def save_inv(data):
    with open(INVENTORY_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ── Blinkit catalog (simulated — replace with Playwright scraping) ────────────
CATALOG = {
    "Milk":    ["Amul Milk 1L", "Mother Dairy Milk 1L", "Nandini Milk 500ml"],
    "Eggs":    ["Farm Fresh Eggs 6pk", "Organic Eggs 12pk"],
    "Butter":  ["Amul Butter 100g", "Britannia Butter 100g"],
    "Cheese":  ["Amul Cheese Slices 200g", "Britannia Cheese Slices"],
    "Yogurt":  ["Amul Dahi 400g", "Mother Dairy Dahi 400g"],
    "Bread":   ["Britannia Bread 400g", "Modern Bread 500g"],
    "Spinach": [],  # simulate unavailable
}

# ── Pydantic models ────────────────────────────────────────────────────────────
class Item(BaseModel):
    name: str
    quantity: int
    min_quantity: int
    unit: str = "units"
    expiry: Optional[str] = ""   # YYYY-MM-DD

class ConsumeRequest(BaseModel):
    name: str
    amount: int

class TokenRequest(BaseModel):
    token: str

class OrderRequest(BaseModel):
    items: list[str]

# ── Push notification helper ──────────────────────────────────────────────────
def send_push(title: str, body: str):
    if not FIREBASE_READY:
        print(f"[NOTIF] {title}: {body}")
        return
    tokens = load_tokens()
    if not tokens:
        return
    for token in tokens:
        try:
            message = messaging.Message(
                notification=messaging.Notification(title=title, body=body),
                token=token,
                android=messaging.AndroidConfig(priority="high"),
                apns=messaging.APNSConfig(
                    payload=messaging.APNSPayload(
                        aps=messaging.Aps(sound="default")
                    )
                ),
            )
            messaging.send(message)
        except Exception as e:
            print(f"Push error: {e}")

# ── Scheduler ─────────────────────────────────────────────────────────────────
def midnight_check():
    inv      = load_inv()
    today    = datetime.date.today()
    low      = [k for k, v in inv.items() if v["quantity"] < v["min_quantity"]]
    expiring = []

    for name, data in inv.items():
        exp = data.get("expiry", "")
        if exp:
            try:
                delta = (datetime.date.fromisoformat(exp) - today).days
                if delta <= 2:
                    expiring.append((name, delta))
            except ValueError:
                pass

    if low:
        send_push("🛒 Low Stock", f"Restock needed: {', '.join(low)}")
    for name, days in expiring:
        if days < 0:
            send_push("⚠️ Expired", f"{name} expired {abs(days)} day(s) ago!")
        elif days == 0:
            send_push("⚠️ Expires Today", f"{name} expires today!")
        else:
            send_push("⏰ Expiry Soon", f"{name} expires in {days} day(s)")

def start_scheduler():
    schedule.every().day.at("00:00").do(midnight_check)
    while True:
        schedule.run_pending()
        time.sleep(60)

threading.Thread(target=start_scheduler, daemon=True).start()

# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "Smart Fridge API running"}

# Register device for push notifications
@app.post("/register-token")
def register_token(req: TokenRequest):
    tokens = load_tokens()
    if req.token not in tokens:
        tokens.append(req.token)
        save_tokens(tokens)
    return {"success": True}

# Get full inventory
@app.get("/inventory")
def get_inventory():
    inv   = load_inv()
    today = datetime.date.today()
    result = []
    for name, data in inv.items():
        item = {"name": name, **data}
        exp  = data.get("expiry", "")
        if exp:
            try:
                item["days_to_expiry"] = (datetime.date.fromisoformat(exp) - today).days
            except ValueError:
                item["days_to_expiry"] = None
        else:
            item["days_to_expiry"] = None
        result.append(item)
    return result

# Add or update item
@app.post("/inventory")
def add_item(item: Item):
    inv = load_inv()
    inv[item.name] = {
        "quantity":     item.quantity,
        "min_quantity": item.min_quantity,
        "unit":         item.unit,
        "expiry":       item.expiry or "",
    }
    save_inv(inv)
    return {"success": True, "item": item.name}

# Delete item
@app.delete("/inventory/{name}")
def delete_item(name: str):
    inv = load_inv()
    if name not in inv:
        raise HTTPException(status_code=404, detail="Item not found")
    del inv[name]
    save_inv(inv)
    return {"success": True}

# Consume (use) item
@app.post("/inventory/consume")
def consume_item(req: ConsumeRequest):
    inv = load_inv()
    if req.name not in inv:
        raise HTTPException(status_code=404, detail="Item not found")
    inv[req.name]["quantity"] = max(0, inv[req.name]["quantity"] - req.amount)
    save_inv(inv)
    qty = inv[req.name]["quantity"]
    mn  = inv[req.name]["min_quantity"]
    if qty < mn:
        send_push("🛒 Low Stock", f"{req.name} is running low ({qty} left, min {mn})")
    return {"success": True, "remaining": qty}

# Get low stock items
@app.get("/low-stock")
def low_stock():
    inv = load_inv()
    return [
        {"name": k, **v}
        for k, v in inv.items()
        if v["quantity"] < v["min_quantity"]
    ]

# Get expiry alerts
@app.get("/expiry-alerts")
def expiry_alerts():
    inv   = load_inv()
    today = datetime.date.today()
    alerts = []
    for name, data in inv.items():
        exp = data.get("expiry", "")
        if exp:
            try:
                delta = (datetime.date.fromisoformat(exp) - today).days
                if delta <= 2:
                    alerts.append({"name": name, "days_to_expiry": delta, **data})
            except ValueError:
                pass
    return alerts

# Search Blinkit for an item
@app.get("/blinkit/search/{item_name}")
def blinkit_search(item_name: str):
    options = CATALOG.get(item_name.strip().title(), [])
    return {"item": item_name, "available": len(options) > 0, "options": options}

# Place order (opens Blinkit — user pays manually)
@app.post("/blinkit/order")
def blinkit_order(req: OrderRequest):
    # In production: use Playwright to add items to Blinkit cart
    # For now returns the deep-link URL for the app to open
    query   = "%20".join(req.items[0].split()) if req.items else "groceries"
    url     = f"https://blinkit.com/s/?q={query}"
    return {"success": True, "blinkit_url": url, "items": req.items}

# Manually trigger midnight check (for testing)
@app.post("/check-now")
def check_now():
    threading.Thread(target=midnight_check, daemon=True).start()
    return {"success": True, "message": "Check triggered"}

# Send test notification
@app.post("/test-notification")
def test_notification():
    send_push("Smart Fridge Test", "Your notifications are working!")
    return {"success": True}
