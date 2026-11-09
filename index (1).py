import os, psycopg
from psycopg.rows import dict_row
from flask import Flask, request, jsonify, abort, g
from werkzeug.local import LocalProxy
from itsdangerous import URLSafeTimedSerializer, BadSignature
from werkzeug.security import check_password_hash, generate_password_hash

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


EXP = f"SELECT product_id,product_name,category,stock_quantity AS stock,expiration_date,warehouse FROM {V} WHERE expiration_date < CURRENT_DATE AND stock_quantity>0 ORDER BY expiration_date DESC LIMIT 15"
MOV = "SELECT m.created_at::date AS day, m.product_id, p.product_name, m.qty_change, m.reason, u.username AS user_name FROM stock_movements m JOIN products p USING(product_id) LEFT JOIN users u ON u.user_id=m.user_id ORDER BY m.movement_id DESC LIMIT 15"
WH = "SELECT w.warehouse_id id, w.name, w.location, count(i.product_id) products, coalesce(sum(i.stock_quantity),0) units FROM warehouses w LEFT JOIN inventory i USING(warehouse_id) GROUP BY w.warehouse_id ORDER BY w.warehouse_id"
SUP = "SELECT s.supplier_id id, s.name, s.contact_email, s.phone, count(p.product_id) products FROM suppliers s LEFT JOIN products p USING(supplier_id) GROUP BY s.supplier_id ORDER BY products DESC, s.name LIMIT 15"
CUS = "SELECT customer_id id, name, email, phone FROM customers ORDER BY customer_id DESC LIMIT 15"
def ext(role, kpis, tables): D[role]["kpis"] = D[role]["kpis"] + kpis; D[role]["tables"] = D[role]["tables"] + tables
OUT = ("Out of stock", "SELECT count(*) FROM inventory WHERE stock_quantity=0")
EXK = ("Expired in stock", f"SELECT count(*) FROM {V} WHERE expiration_date < CURRENT_DATE AND stock_quantity>0")
WR = [("/stock/writeoff", "Write off")]
ext("admin", [OUT, EXK, ("Warehouses", "SELECT count(*) FROM warehouses"), ("Suppliers", "SELECT count(*) FROM suppliers")],
    [("Warehouses", WH, []), ("Suppliers", SUP, []), ("Customers", CUS, []), ("Expired stock (past expiration date)", EXP, WR), ("Recent stock movements (in / out)", MOV, [])])
ext("inventory_manager", [OUT, EXK], [("Expired stock (past expiration date)", EXP, WR), ("Recent stock movements (in / out)", MOV, [])])
ext("warehouse_manager", [OUT], [("Warehouses", WH, []), ("Expired stock (past expiration date)", EXP, WR), ("Recent stock movements (in / out)", MOV, [])])
ext("purchase_manager", [OUT], [("Suppliers", SUP, [])]); ext("sales_manager", [], [("Customers", CUS, [])]); ext("sales_staff", [], [("Customers", CUS, [])])
ENT = {"supplier": dict(t="suppliers", pk="supplier_id", cols=[("name", "Name"), ("contact_email", "Email"), ("phone", "Phone")], roles=("admin", "purchase_manager")),
       "warehouse": dict(t="warehouses", pk="warehouse_id", cols=[("name", "Name"), ("location", "Location")], roles=("admin", "warehouse_manager")),
       "customer": dict(t="customers", pk="customer_id", cols=[("name", "Name"), ("email", "Email"), ("phone", "Phone")], roles=("admin", "sales_manager", "sales_staff"))}
def fld(n, l, t="text", req=True, o=None): return dict(n=n, l=l, t=t, req=req, o=o or [])
def F(title, url, fields, button): return dict(title=title, url=url, fields=fields, button=button)
def opts(sql): return [[str(r["v"]), r["l"]] for r in q(sql)]
def forms_for(role):
    out = []
    pid = fld("product_id", "Product ID")
    if role in ("inventory_manager", "admin"):
        out += [F("Add new product", "/api/product/add", [fld("product_id", "Product ID (e.g. 99-000-0001)"), fld("product_name", "Product name"),
                  fld("category_id", "Category", "select", o=opts("SELECT category_id v, name l FROM categories ORDER BY name")),
                  fld("supplier_id", "Supplier", "select", o=opts("SELECT supplier_id v, name l FROM suppliers ORDER BY name")),
                  fld("warehouse_id", "Warehouse", "select", False, opts("SELECT warehouse_id v, name l FROM warehouses ORDER BY name")),
                  fld("unit_price", "Unit price", "number"), fld("stock_quantity", "Opening stock", "number"), fld("reorder_level", "Reorder level", "number"),
                  fld("reorder_quantity", "Reorder quantity", "number"), fld("expiration_date", "Expiry date", "date", False)], "Add product"),
                F("Update product (price / status / reorder)", "/api/product/update", [pid, fld("unit_price", "New price", "number", False),
                  fld("status", "Status", "select", False, [[x, x] for x in ("Active", "Discontinued", "Backordered")]),
                  fld("reorder_level", "Reorder level", "number", False), fld("reorder_quantity", "Reorder quantity", "number", False)], "Update"),
                F("Delete product", "/api/product/delete", [pid], "Delete")]
    if role in ("inventory_manager", "warehouse_manager", "admin"):
        out += [F("Stock in / out (adjust stock)", "/api/stock/adjust", [pid, fld("change", "Quantity (+ stock in, - stock out)", "number"), fld("note", "Reason", "text", False)], "Apply")]
    if role in ("warehouse_manager", "admin"):
        out += [F("Move product to another warehouse", "/api/stock/move", [pid, fld("warehouse_id", "Warehouse", "select", o=opts("SELECT warehouse_id v, name l FROM warehouses ORDER BY name"))], "Move")]
    for ent, e in ENT.items():
        if role in e["roles"]:
            idf = fld("id", f"{ent.title()} ID (see the table)", "number")
            out += [F(f"Add {ent}", f"/api/e/{ent}/add", [fld(c, l, req=(i == 0)) for i, (c, l) in enumerate(e["cols"])], "Add"),
                    F(f"Update {ent}", f"/api/e/{ent}/update", [idf] + [fld(c, l, req=False) for c, l in e["cols"]], "Update"),
                    F(f"Delete {ent}", f"/api/e/{ent}/delete", [idf], "Delete")]
    if role == "admin":
        out += [F("Add user (login account)", "/api/user/add", [fld("username", "Username"), fld("full_name", "Full name"), fld("role", "Role", "select", o=[[r, r] for r in D]),
                  fld("password", "Password (min 8 characters)", "password"), fld("customer_id", "Customer ID (customer role)", "number", False), fld("supplier_id", "Supplier ID (supplier role)", "number", False)], "Add user"),
                F("Enable / disable user", "/api/user/toggle", [fld("username", "Username"), fld("state", "Action", "select", o=[["disable", "Disable"], ["enable", "Enable"]])], "Apply")]
    return out

def demo_mode(): return os.environ.get("DEMO_MODE", "").strip().lower()    # "" (off) | "on" | "readonly"
def nav_for(role, demo):
    roles = list(D) if demo else (list(STAFF) if role == "admin" else [role])
    return [dict(role=r, title=D[r]["title"], group="internal" if r in STAFF else "portal") for r in roles]
def issue(u, demo=False):
    tok = ser.dumps(dict(uid=u["user_id"], role=u["role"], name=u["full_name"], cid=u["customer_id"], sid=u["supplier_id"], demo=demo))
    return jsonify(ok=True, token=tok, role=u["role"], name=u["full_name"], demo=demo)

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
    if not r or not r[0].get("active", True) or not check_password_hash(r[0]["password_hash"], f.get("password", "")):
        return jsonify(ok=False, message="Invalid username or password"), 401
    return issue(r[0])

@app.post("/api/logout")
def logout(): return jsonify(ok=True)   # client discards its token

@app.get("/api/me")
def me():
    if "role" not in session: abort(401, "Please sign in")
    return jsonify(role=session["role"], name=session["name"], demo=bool(session.get("demo")))

@app.get("/api/demo/roles")
def demo_roles():
    on = demo_mode() in ("on", "readonly")
    return jsonify(enabled=on, readonly=demo_mode() == "readonly", roles=nav_for(None, True) if on else [])

@app.post("/api/demo/login")
def demo_login():      # passwordless demo sessions; only when DEMO_MODE is on/readonly
    if demo_mode() not in ("on", "readonly"): abort(403, "Demo mode is off")
    role = (request.get_json(silent=True) or {}).get("role")
    if role not in D: abort(404)
    r = q("SELECT * FROM users WHERE role=%s AND active ORDER BY user_id LIMIT 1", (role,))
    if not r: abort(404, "No user exists for this role")
    return issue(r[0], demo=True)

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
        nav=nav_for(mine, bool(session.get("demo"))), demo=bool(session.get("demo")), readonly=bool(session.get("demo")) and demo_mode() == "readonly",
        kpis=[dict(label=l, value=list(q(s, p)[0].values())[0]) for l, s in d["kpis"]],
        chart=dict(title=d["chart"][0], rows=q(d["chart"][1], p)) if d["chart"] else None, tables=tables,
        forms=forms_for(role), search=role in ("sales_staff", "customer"), can_order=role in ("sales_staff", "customer", "sales_manager"),
        customers=q("SELECT customer_id,name FROM customers ORDER BY name") if role in ("sales_staff", "sales_manager") else [])

def guard(*roles):
    def deco(f):
        def w(*a, **k):
            if "role" not in session: abort(401, "Please sign in")
            if session["role"] not in roles: abort(403, "Not allowed for your role")
            if session.get("demo") and demo_mode() == "readonly": abort(403, "Demo is read-only: actions are disabled")
            try: return jsonify(ok=True, message=f(*a, **k))
            except (ValueError, KeyError, TypeError) as e: return jsonify(ok=False, message=f"Not done: {e}"), 400
            except psycopg.Error: return jsonify(ok=False, message="Not done: the database rejected that input (check the values)"), 400
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
        con.execute("UPDATE inventory SET stock_quantity=stock_quantity-%s WHERE product_id=%s", (qty, pid)); log(con, pid, -qty, "Sale", f"Order #{oid}")
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
            con.execute("UPDATE inventory i SET stock_quantity=i.stock_quantity+s.quantity FROM sales_order_items s WHERE s.sales_order_id=%s AND s.product_id=i.product_id", (oid,)); con.execute("INSERT INTO stock_movements(product_id,qty_change,reason,note,user_id) SELECT product_id, quantity, 'Order cancelled', %s, %s FROM sales_order_items WHERE sales_order_id=%s", (f"Order #{oid}", session.get("uid"), oid))
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
            con.execute("UPDATE inventory i SET stock_quantity=i.stock_quantity+x.quantity FROM purchase_order_items x WHERE x.purchase_order_id=%s AND x.product_id=i.product_id", (oid,)); con.execute("INSERT INTO stock_movements(product_id,qty_change,reason,note,user_id) SELECT product_id, quantity, 'Purchase received', %s, %s FROM purchase_order_items WHERE purchase_order_id=%s", (f"PO #{oid}", session.get("uid"), oid))
    return f"Purchase order #{oid} {need[1].lower()}."

def log(con, pid, change, reason, note=None):
    con.execute("INSERT INTO stock_movements(product_id,qty_change,reason,note,user_id) VALUES (%s,%s,%s,%s,%s)", (pid, change, reason, note, session.get("uid")))
def num(f, k, cast=int, req=True):
    v = str(f.get(k, "")).strip()
    if v == "":
        if req: raise ValueError(f"{k.replace('_', ' ')} is required")
        return None
    return cast(v)
def db(): return psycopg.connect(URL(), row_factory=dict_row)

@app.post("/api/product/add")
@guard("inventory_manager", "admin")
def product_add():
    f = body(); pid = str(f.get("product_id", "")).strip(); name = str(f.get("product_name", "")).strip()
    if not pid or not name: raise ValueError("product id and name are required")
    price, stock, rl, rq = num(f, "unit_price", float), num(f, "stock_quantity"), num(f, "reorder_level"), num(f, "reorder_quantity")
    if min(price, stock, rl, rq) < 0: raise ValueError("numbers cannot be negative")
    exp = str(f.get("expiration_date", "")).strip() or None
    with db() as con:
        try:
            con.execute("INSERT INTO products(product_id,product_name,category_id,supplier_id,unit_price,status) VALUES (%s,%s,%s,%s,%s,'Active')", (pid, name, num(f, "category_id"), num(f, "supplier_id"), price))
            con.execute("INSERT INTO inventory(product_id,stock_quantity,reorder_level,reorder_quantity,date_received,expiration_date,warehouse_id) VALUES (%s,%s,%s,%s,LEAST(CURRENT_DATE, %s::date),%s,%s)", (pid, stock, rl, rq, exp, exp, num(f, "warehouse_id", int, False)))
            if stock: log(con, pid, stock, "Opening stock")
        except psycopg.errors.UniqueViolation: raise ValueError("that product id already exists")
        except psycopg.errors.ForeignKeyViolation: raise ValueError("category, supplier or warehouse does not exist")
    return f"Product {pid} added with {stock} units."

@app.post("/api/product/update")
@guard("inventory_manager", "admin")
def product_update():
    f = body(); pid = str(f.get("product_id", "")).strip()
    price, st, rl, rq = num(f, "unit_price", float, False), (f.get("status") or None), num(f, "reorder_level", int, False), num(f, "reorder_quantity", int, False)
    if any(v is not None and v < 0 for v in (price, rl, rq)): raise ValueError("numbers cannot be negative")
    with db() as con:
        if not con.execute("UPDATE products SET unit_price=COALESCE(%s::numeric, unit_price), status=COALESCE(%s::text, status) WHERE product_id=%s RETURNING 1", (price, st, pid)).fetchone(): raise ValueError("unknown product id")
        con.execute("UPDATE inventory SET reorder_level=COALESCE(%s::int, reorder_level), reorder_quantity=COALESCE(%s::int, reorder_quantity) WHERE product_id=%s", (rl, rq, pid))
    return f"Product {pid} updated."

@app.post("/api/product/delete")
@guard("inventory_manager", "admin")
def product_delete():
    pid = str(body().get("product_id", "")).strip()
    with db() as con:
        try:
            if not con.execute("DELETE FROM products WHERE product_id=%s RETURNING 1", (pid,)).fetchone(): raise ValueError("unknown product id")
        except psycopg.errors.ForeignKeyViolation: raise ValueError("it has order history, so mark it Discontinued instead")
    return f"Product {pid} deleted."

@app.post("/api/stock/adjust")
@guard("inventory_manager", "warehouse_manager", "admin")
def stock_adjust():
    f = body(); pid = str(f.get("product_id", "")).strip(); ch = num(f, "change")
    if ch == 0: raise ValueError("quantity cannot be 0")
    with db() as con:
        r = con.execute("UPDATE inventory SET stock_quantity=stock_quantity+%s WHERE product_id=%s AND stock_quantity+%s>=0 RETURNING stock_quantity", (ch, pid, ch)).fetchone()
        if not r: raise ValueError("unknown product, or not enough stock to remove that quantity")
        log(con, pid, ch, "Stock in" if ch > 0 else "Stock out", (f.get("note") or "").strip() or None)
    return f"Stock of {pid} is now {r['stock_quantity']}."

@app.post("/api/stock/writeoff")
@guard("inventory_manager", "warehouse_manager", "admin")
def stock_writeoff():
    pid = str(body().get("id", "")).strip()
    with db() as con:
        r = con.execute("SELECT stock_quantity FROM inventory WHERE product_id=%s FOR UPDATE", (pid,)).fetchone()
        if not r or r["stock_quantity"] <= 0: raise ValueError("nothing to write off")
        con.execute("UPDATE inventory SET stock_quantity=0 WHERE product_id=%s", (pid,)); log(con, pid, -r["stock_quantity"], "Expired write-off")
    return f"{r['stock_quantity']} expired units of {pid} removed from stock."

@app.post("/api/stock/move")
@guard("warehouse_manager", "admin")
def stock_move():
    f = body()
    with db() as con:
        try:
            if not con.execute("UPDATE inventory SET warehouse_id=%s WHERE product_id=%s RETURNING 1", (num(f, "warehouse_id"), str(f.get("product_id", "")).strip())).fetchone(): raise ValueError("unknown product id")
        except psycopg.errors.ForeignKeyViolation: raise ValueError("warehouse does not exist")
    return "Product moved."

@app.post("/api/e/<ent>/<act>")
@guard("admin", "purchase_manager", "warehouse_manager", "sales_manager", "sales_staff")
def ent_act(ent, act):
    e = ENT.get(ent) or abort(404)
    if session["role"] not in e["roles"]: abort(403, "Not allowed for your role")
    f = body(); cols = [c for c, _ in e["cols"]]
    with db() as con:
        try:
            if act == "add":
                v = [str(f.get(c) or "").strip() or None for c in cols]
                if not v[0]: raise ValueError("name is required")
                n = con.execute(f"INSERT INTO {e['t']}({','.join(cols)}) VALUES ({','.join(['%s'] * len(cols))}) RETURNING {e['pk']}", v).fetchone()[e["pk"]]
                return f"{ent.title()} #{n} added."
            i = int(f["id"])
            if act == "update":
                sets = [(c, str(f[c]).strip()) for c in cols if str(f.get(c) or "").strip()]
                if not sets: raise ValueError("enter at least one field to change")
                if not con.execute(f"UPDATE {e['t']} SET {','.join(c + '=%s' for c, _ in sets)} WHERE {e['pk']}=%s RETURNING 1", [v for _, v in sets] + [i]).fetchone(): raise ValueError("id not found")
                return f"{ent.title()} #{i} updated."
            if act == "delete":
                if not con.execute(f"DELETE FROM {e['t']} WHERE {e['pk']}=%s RETURNING 1", (i,)).fetchone(): raise ValueError("id not found")
                return f"{ent.title()} #{i} deleted."
        except psycopg.errors.UniqueViolation: raise ValueError("that name already exists")
        except psycopg.errors.ForeignKeyViolation: raise ValueError("it is still used by other records, so it cannot be deleted")
    abort(404)

@app.post("/api/user/add")
@guard("admin")
def user_add():
    f = body(); u, n, r, pw = (str(f.get(k, "")).strip() for k in ("username", "full_name", "role")) + (str(f.get("password", "")),)
    if not u or not n: raise ValueError("username and full name are required")
    if r not in D: raise ValueError("choose a role")
    if len(pw) < 8: raise ValueError("password must have at least 8 characters")
    cid, sid = num(f, "customer_id", int, False), num(f, "supplier_id", int, False)
    if (r == "customer" and cid is None) or (r == "supplier" and sid is None): raise ValueError("customer and supplier logins need their Customer / Supplier ID")
    with db() as con:
        try: con.execute("INSERT INTO users(username,full_name,password_hash,role,customer_id,supplier_id) VALUES (%s,%s,%s,%s,%s,%s)", (u, n, generate_password_hash(pw), r, cid, sid))
        except psycopg.errors.UniqueViolation: raise ValueError("that username already exists")
        except psycopg.errors.ForeignKeyViolation: raise ValueError("that Customer / Supplier ID does not exist")
    return f"User {u} created."

@app.post("/api/user/toggle")
@guard("admin")
def user_toggle():
    f = body(); u = str(f.get("username", "")).strip(); on = f.get("state") == "enable"
    with db() as con:
        r = con.execute("SELECT user_id FROM users WHERE username=%s", (u,)).fetchone()
        if not r: raise ValueError("unknown username")
        if r["user_id"] == session["uid"] and not on: raise ValueError("you cannot disable your own account")
        con.execute("UPDATE users SET active=%s WHERE user_id=%s", (on, r["user_id"]))
    return f"User {u} {'enabled' if on else 'disabled'}."

if __name__ == "__main__": app.run(debug=True)
