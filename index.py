import os, psycopg
from psycopg.rows import dict_row
from flask import Flask, request, jsonify, abort, g
from werkzeug.local import LocalProxy
from itsdangerous import URLSafeTimedSerializer, BadSignature
from werkzeug.security import check_password_hash

app = Flask(__name__)
if os.environ.get("VERCEL") and not os.environ.get("SECRET_KEY"):
    raise RuntimeError("Set SECRET_KEY in Vercel environment variables")
SECRET = os.environ.get("SECRET_KEY", "dev-only-secret")
ser = URLSafeTimedSerializer(SECRET)
session = LocalProxy(lambda: g.s)          # per-request user data, loaded from the bearer token

@app.before_request
def load_user():
    g.s = {}
    h = request.headers.get("Authorization", "")
    if h.startswith("Bearer "):
        try: g.s = ser.loads(h[7:], max_age=12 * 3600)   # tokens expire after 12 hours
        except BadSignature: pass

@app.after_request
def cors(r):   # set FRONTEND_ORIGIN to your frontend URL once deployed (default * is fine for bearer tokens)
    r.headers["Access-Control-Allow-Origin"] = os.environ.get("FRONTEND_ORIGIN", "*")
    r.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return r
URL = lambda: os.environ["DATABASE_URL"]

def q(sql, args=None):
    with psycopg.connect(URL(), row_factory=dict_row) as con:
        return con.execute(sql, args).fetchall()

def P(): return dict(cid=session.get("cid"), sid=session.get("sid"), uid=session.get("uid"))

V = "v_inventory"
LOW = f"SELECT product_id,product_name,category,supplier,stock_quantity AS stock,reorder_level AS reorder_at,reorder_quantity AS suggested_order FROM {V} WHERE is_low_stock AND status<>'Discontinued' ORDER BY stock_quantity-reorder_level LIMIT 25"
BYCAT = ("Stock units by category", f"SELECT category label, sum(stock_quantity) value FROM {V} GROUP BY 1 ORDER BY 2 DESC")
def SO(where): return f"SELECT o.sales_order_id id, c.name customer, o.order_date, o.status, sum(i.quantity*i.unit_price) total FROM sales_orders o JOIN customers c USING(customer_id) LEFT JOIN sales_order_items i USING(sales_order_id) WHERE {where} GROUP BY o.sales_order_id,c.name ORDER BY o.sales_order_id DESC LIMIT 20"
def PO(where): return f"SELECT o.purchase_order_id id, s.name supplier, o.order_date, o.status, sum(i.quantity*i.unit_cost) total, string_agg(p.product_name||' x'||i.quantity, ', ') items FROM purchase_orders o JOIN suppliers s USING(supplier_id) JOIN purchase_order_items i USING(purchase_order_id) JOIN products p USING(product_id) WHERE {where} GROUP BY o.purchase_order_id,s.name ORDER BY o.purchase_order_id DESC LIMIT 20"
CAT = f"SELECT product_id,product_name,category,unit_price,stock_quantity,status FROM {V} WHERE (product_name ILIKE %(t)s OR category ILIKE %(t)s) AND status='Active' ORDER BY product_name LIMIT 40"

# Each stakeholder dashboard: KPI cards, one chart, tables [(title, sql, row_actions)]
D = {
 "admin": dict(title="Owner / Admin", kpis=[("Products", f"SELECT count(*) FROM {V}"), ("Stock units", f"SELECT sum(stock_quantity) FROM {V}"),
    ("Inventory value", f"SELECT round(sum(stock_value),2) FROM {V}"), ("Low stock", f"SELECT count(*) FROM {V} WHERE is_low_stock"),
    ("Sales (non-cancelled)", "SELECT round(coalesce(sum(i.quantity*i.unit_price),0),2) FROM sales_orders o JOIN sales_order_items i USING(sales_order_id) WHERE o.status<>'Cancelled'"),
    ("Open purchase orders", "SELECT count(*) FROM purchase_orders WHERE status IN ('Draft','Sent')"),
    ("Date-quality issues", f"SELECT count(*) FROM {V} WHERE date_issue")],
    chart=("Inventory value by category", f"SELECT category label, round(sum(stock_value),2) value FROM {V} GROUP BY 1 ORDER BY 2 DESC"),
    tables=[("Users & roles", "SELECT username, full_name, role FROM users ORDER BY role", []),
            ("Recent sales orders", SO("true"), []), ("Recent purchase orders", PO("true"), [])]),
 "inventory_manager": dict(title="Inventory Manager", kpis=[
    ("Low-stock (active)", f"SELECT count(*) FROM {V} WHERE is_low_stock AND status<>'Discontinued'"),
    ("Low-stock (all)", f"SELECT count(*) FROM {V} WHERE is_low_stock"),
    ("Backordered", f"SELECT count(*) FROM {V} WHERE status='Backordered'"), ("Avg turnover rate", f"SELECT round(avg(turnover_rate),1) FROM {V}")],
    chart=BYCAT, tables=[("Reorder alerts (most critical first)", LOW, [])]),
 "warehouse_manager": dict(title="Warehouse Manager", kpis=[("Stock units held", f"SELECT sum(stock_quantity) FROM {V}"),
    ("Incoming POs", "SELECT count(*) FROM purchase_orders WHERE status='Sent'"),
    ("Orders to dispatch", "SELECT count(*) FROM sales_orders WHERE status='Confirmed'"), ("Bad expiry dates", f"SELECT count(*) FROM {V} WHERE date_issue")],
    chart=("Units by status", f"SELECT status label, sum(stock_quantity) value FROM {V} GROUP BY 1"),
    tables=[("Incoming purchase orders (receive to add stock)", PO("o.status='Sent'"), [("/po/receive", "Receive")]),
            ("Confirmed orders ready to dispatch", SO("o.status='Confirmed'"), [("/so/dispatch", "Dispatch")]),
            ("Records needing date check", f"SELECT product_id, product_name, date_received, expiration_date, storage_location FROM {V} WHERE date_issue ORDER BY date_received DESC LIMIT 15", [])]),
 "sales_manager": dict(title="Sales Manager", kpis=[("Orders", "SELECT count(*) FROM sales_orders"), ("Pending", "SELECT count(*) FROM sales_orders WHERE status='Pending'"),
    ("Revenue (dispatched)", "SELECT round(coalesce(sum(i.quantity*i.unit_price),0),2) FROM sales_orders o JOIN sales_order_items i USING(sales_order_id) WHERE o.status='Dispatched'"),
    ("Customers", "SELECT count(*) FROM customers")],
    chart=("Order value by status", "SELECT o.status label, round(sum(i.quantity*i.unit_price),2) value FROM sales_orders o JOIN sales_order_items i USING(sales_order_id) GROUP BY 1"),
    tables=[("Pending orders", SO("o.status='Pending'"), [("/so/confirm", "Confirm"), ("/so/cancel", "Cancel")]),
            ("Confirmed orders", SO("o.status='Confirmed'"), [("/so/dispatch", "Dispatch"), ("/so/cancel", "Cancel")]),
            ("Top 10 sellers (dataset)", f"SELECT product_name, category, sales_volume, unit_price FROM {V} ORDER BY sales_volume DESC LIMIT 10", [])]),
 "purchase_manager": dict(title="Purchase Manager", kpis=[("To reorder (active)", f"SELECT count(*) FROM {V} WHERE is_low_stock AND status<>'Discontinued'"),
    ("Draft POs", "SELECT count(*) FROM purchase_orders WHERE status='Draft'"), ("Sent POs", "SELECT count(*) FROM purchase_orders WHERE status='Sent'"),
    ("Est. reorder cost", f"SELECT round(sum(reorder_quantity*unit_price),2) FROM {V} WHERE is_low_stock AND status<>'Discontinued'")],
    chart=("Items to reorder by supplier (top 10)", f"SELECT supplier label, count(*) value FROM {V} WHERE is_low_stock AND status<>'Discontinued' GROUP BY 1 ORDER BY 2 DESC LIMIT 10"),
    tables=[("Draft purchase orders", PO("o.status='Draft'"), [("/po/send", "Send to supplier"), ("/po/cancel", "Cancel")]),
            ("Reorder list (click to create PO)", LOW, [("/po/quick", "Create PO")]), ("Recent purchase orders", PO("true"), [])]),
 "sales_staff": dict(title="Sales Staff", kpis=[("Active products", f"SELECT count(*) FROM {V} WHERE status='Active'"),
    ("My orders", "SELECT count(*) FROM sales_orders WHERE created_by=%(uid)s")], chart=None,
    tables=[("Product lookup", CAT, []), ("My recent orders", SO("o.created_by=%(uid)s"), [])]),
 "customer": dict(title="My Account", kpis=[("My orders", "SELECT count(*) FROM sales_orders WHERE customer_id=%(cid)s"),
    ("Pending", "SELECT count(*) FROM sales_orders WHERE customer_id=%(cid)s AND status='Pending'"),
    ("Total spent", "SELECT round(coalesce(sum(i.quantity*i.unit_price),0),2) FROM sales_orders o JOIN sales_order_items i USING(sales_order_id) WHERE o.customer_id=%(cid)s AND o.status<>'Cancelled'")],
    chart=None, tables=[("Shop products", CAT, []), ("My orders", SO("o.customer_id=%(cid)s"), [])]),
 "supplier": dict(title="Supplier Portal", kpis=[("Products I supply", "SELECT count(*) FROM products WHERE supplier_id=%(sid)s"),
    ("Open orders (Sent)", "SELECT count(*) FROM purchase_orders WHERE supplier_id=%(sid)s AND status='Sent'"),
    ("Order value (all)", "SELECT round(coalesce(sum(i.quantity*i.unit_cost),0),2) FROM purchase_orders o JOIN purchase_order_items i USING(purchase_order_id) WHERE o.supplier_id=%(sid)s")],
    chart=None, tables=[("Purchase orders for me", PO("o.supplier_id=%(sid)s AND o.status<>'Draft'"), []),
        ("My products & stock", "SELECT p.product_id,p.product_name,i.stock_quantity,i.reorder_level,p.status FROM products p JOIN inventory i USING(product_id) WHERE p.supplier_id=%(sid)s ORDER BY i.stock_quantity-i.reorder_level", [])]),
}
STAFF = ("admin", "inventory_manager", "warehouse_manager", "sales_manager", "purchase_manager", "sales_staff")


from werkzeug.exceptions import HTTPException
@app.errorhandler(HTTPException)
def _err(e): return jsonify(ok=False, message=e.description if e.code != 404 else "Not found"), e.code

@app.get("/")
@app.get("/api/health")
def health():
    try: q("SELECT 1"); db = True
    except Exception: db = False
    return jsonify(service="smartbiz-api", status="ok", database=db)

@app.post("/api/login")
def login():
    f = request.get_json(silent=True) or {}
    r = q("SELECT * FROM users WHERE username=%s", (f.get("username", ""),))
    if not r or not check_password_hash(r[0]["password_hash"], f.get("password", "")):
        return jsonify(ok=False, message="Invalid username or password"), 401
    u = r[0]
    tok = ser.dumps(dict(uid=u["user_id"], role=u["role"], name=u["full_name"], cid=u["customer_id"], sid=u["supplier_id"]))
    return jsonify(ok=True, token=tok, role=u["role"], name=u["full_name"])

@app.post("/api/logout")
def logout(): return jsonify(ok=True)   # client discards its token

@app.get("/api/me")
def me():
    if "role" not in session: abort(401, "Please sign in")
    return jsonify(role=session["role"], name=session["name"])

@app.get("/api/dashboard/<role>")
def dash(role):
    if "role" not in session: abort(401, "Please sign in")
    if role not in D: abort(404)
    mine = session["role"]
    if mine != role and not (mine == "admin" and role in STAFF): abort(403, "Not allowed for your role")  # enforced server-side
    d, p = D[role], P(); p["t"] = f"%{request.args.get('q', '')}%"
    tables = []
    for t, s, a in d["tables"]:
        rows = q(s, p)
        tables.append(dict(title=t, columns=list(rows[0].keys()) if rows else [], rows=[list(r.values()) for r in rows],
                           actions=[dict(url="/api" + u, label=l) for u, l in a]))
    return jsonify(role=role, title=d["title"], name=session["name"],
        nav=[dict(role=r, title=x["title"]) for r, x in D.items() if r in STAFF] if mine == "admin" else [],
        kpis=[dict(label=l, value=list(q(s, p)[0].values())[0]) for l, s in d["kpis"]],
        chart=dict(title=d["chart"][0], rows=q(d["chart"][1], p)) if d["chart"] else None, tables=tables,
        search=role in ("sales_staff", "customer"), can_order=role in ("sales_staff", "customer", "sales_manager"),
        customers=q("SELECT customer_id,name FROM customers ORDER BY name") if role in ("sales_staff", "sales_manager") else [])

def guard(*roles):
    def deco(f):
        def w(*a, **k):
            if "role" not in session: abort(401, "Please sign in")
            if session["role"] not in roles: abort(403, "Not allowed for your role")
            try: return jsonify(ok=True, message=f(*a, **k))
            except (ValueError, KeyError, TypeError) as e: return jsonify(ok=False, message=f"Not done: {e}"), 400
        w.__name__ = f.__name__; return w
    return deco

def add_so(con, cid, lines):
    oid = con.execute("INSERT INTO sales_orders(customer_id,created_by) VALUES (%s,%s) RETURNING sales_order_id", (cid, session["uid"])).fetchone()["sales_order_id"]
    for pid, qty in lines:
        r = con.execute("SELECT p.unit_price,p.status,i.stock_quantity FROM products p JOIN inventory i USING(product_id) WHERE p.product_id=%s FOR UPDATE OF i", (pid,)).fetchone()
        if not r: raise ValueError(f"unknown product {pid}")
        if r["status"] == "Discontinued": raise ValueError(f"{pid} is discontinued")
        if r["stock_quantity"] < qty: raise ValueError(f"only {r['stock_quantity']} in stock for {pid}")
        con.execute("INSERT INTO sales_order_items(sales_order_id,product_id,quantity,unit_price) VALUES (%s,%s,%s,%s)", (oid, pid, qty, r["unit_price"]))
        con.execute("UPDATE inventory SET stock_quantity=stock_quantity-%s WHERE product_id=%s", (qty, pid))
    return oid

def body(): return request.get_json(silent=True) or {}

@app.post("/api/so/new")
@guard("sales_staff", "sales_manager", "customer")
def so_new():
    f = body(); cid = session["cid"] if session["role"] == "customer" else int(f["customer_id"])
    lines = [(str(l["product_id"]).strip(), int(l["quantity"])) for l in f.get("lines", []) if str(l.get("product_id", "")).strip()]
    if not lines or any(n <= 0 for _, n in lines): raise ValueError("add at least one product with a positive quantity")
    with psycopg.connect(URL(), row_factory=dict_row) as con:   # one transaction: all lines or nothing
        return f"Order #{add_so(con, cid, lines)} placed and stock updated."

@app.post("/api/so/<act>")
@guard("sales_manager", "warehouse_manager", "admin")
def so_act(act):
    frm, to = {"confirm": ("Pending", "Confirmed"), "dispatch": ("Confirmed", "Dispatched"), "cancel": ("Pending','Confirmed", "Cancelled")}[act]
    oid = int(body()["id"])
    with psycopg.connect(URL(), row_factory=dict_row) as con:
        if not con.execute(f"UPDATE sales_orders SET status=%s WHERE sales_order_id=%s AND status IN ('{frm}') RETURNING 1", (to, oid)).fetchone():
            raise ValueError("order is not in a state that allows this")
        if act == "cancel":
            con.execute("UPDATE inventory i SET stock_quantity=i.stock_quantity+s.quantity FROM sales_order_items s WHERE s.sales_order_id=%s AND s.product_id=i.product_id", (oid,))
    return f"Order #{oid} {to.lower()}."

@app.post("/api/po/quick")
@guard("purchase_manager", "admin")
def po_quick():
    pid = body()["id"]
    with psycopg.connect(URL(), row_factory=dict_row) as con:
        r = con.execute("SELECT p.supplier_id,p.unit_price,i.reorder_quantity FROM products p JOIN inventory i USING(product_id) WHERE p.product_id=%s", (pid,)).fetchone()
        if not r: raise ValueError("unknown product")
        n = con.execute("INSERT INTO purchase_orders(supplier_id,created_by) VALUES (%s,%s) RETURNING purchase_order_id", (r["supplier_id"], session["uid"])).fetchone()["purchase_order_id"]
        con.execute("INSERT INTO purchase_order_items(purchase_order_id,product_id,quantity,unit_cost) VALUES (%s,%s,%s,%s)", (n, pid, max(r["reorder_quantity"], 1), r["unit_price"]))
    return f"Draft purchase order #{n} created."

@app.post("/api/po/<act>")
@guard("purchase_manager", "warehouse_manager", "admin")
def po_act(act):
    need = {"send": ("Draft", "Sent", "purchase_manager"), "cancel": ("Draft", "Cancelled", "purchase_manager"), "receive": ("Sent", "Received", "warehouse_manager")}[act]
    if session["role"] not in (need[2], "admin"): abort(403, "Not allowed for your role")
    oid = int(body()["id"])
    with psycopg.connect(URL(), row_factory=dict_row) as con:
        if not con.execute("UPDATE purchase_orders SET status=%s WHERE purchase_order_id=%s AND status=%s RETURNING 1", (need[1], oid, need[0])).fetchone():
            raise ValueError("purchase order is not in a state that allows this")
        if act == "receive":
            con.execute("UPDATE inventory i SET stock_quantity=i.stock_quantity+x.quantity FROM purchase_order_items x WHERE x.purchase_order_id=%s AND x.product_id=i.product_id", (oid,))
    return f"Purchase order #{oid} {need[1].lower()}."

if __name__ == "__main__": app.run(debug=True)
