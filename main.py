"""
Pro-Ed Naviga tracking proxy — deterministic, single-call order/invoice tracking.

Collapses the Naviga chain (token -> orderlist -> booksuborder) into ONE call the
LiveX voice agent makes. The proxy holds the Naviga credentials, caches + refreshes
the token itself, and RETRIES once on a 401 with a fresh token — which is exactly
the resilience the LLM-driven flow could not do reliably.

Two endpoints, each accepts EITHER a PO number OR customer id + invoice id:
  POST /order-tracking     {"ponumber":"10446576"}  |  {"customerid":"00829707","invoiceid":"3132701"}
  POST /invoice-tracking   (same input contract)
Auth: header  X-Api-Key: <PROXY_API_KEY>
Always returns HTTP 200 with a slim, speakable JSON (never leaks a 401 to the agent).

Env (set at deploy — NEVER hardcode secrets):
  NAVIGA_CLIENT_ID, NAVIGA_CLIENT_SECRET, PROXY_API_KEY, [NAVIGA_WEBSITE_ID=187]
"""
import os, time, threading
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

BASE = "https://pei.navigahub.com/ElanRESTservice/PEI"
CLIENT_ID     = os.environ["NAVIGA_CLIENT_ID"]
CLIENT_SECRET = os.environ["NAVIGA_CLIENT_SECRET"]
PROXY_API_KEY = os.environ["PROXY_API_KEY"]
WEBSITE_ID    = os.environ.get("NAVIGA_WEBSITE_ID", "187")
TIMEOUT       = float(os.environ.get("HTTP_TIMEOUT", "8"))  # keep voice-fast

# ---- token cache (per warm instance) with refresh + 401 retry -------------------
_tok = {"v": None, "exp": 0.0}
_lock = threading.Lock()

def get_token(force=False):
    now = time.time()
    with _lock:                       # fast path: cache read only
        if not force and _tok["v"] and now < _tok["exp"]:
            return _tok["v"]
    # Fetch OUTSIDE the lock so concurrent requests don't serialize on the HTTP call
    # (worst case a couple of parallel fetches on cold start — Naviga tokens are stateless).
    r = requests.post(f"{BASE}/Token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials",
              "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
        timeout=TIMEOUT)
    r.raise_for_status()
    j = r.json()
    with _lock:                       # brief write only
        _tok["v"] = j["access_token"]
        # cap the cache at 1h, keep a 60s safety buffer under whatever Naviga returns
        _tok["exp"] = now + min(int(j.get("expires_in", 3600)), 3600) - 60
        return _tok["v"]

def naviga_get(path, params):
    """GET with one automatic fresh-token retry on 401 (the intermittent-401 killer)."""
    for attempt in (0, 1):
        token = get_token(force=(attempt == 1))
        r = requests.get(f"{BASE}{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params=params, timeout=TIMEOUT)
        if r.status_code == 401 and attempt == 0:
            continue  # stale/rejected token -> refetch fresh and retry once
        r.raise_for_status()
        return r.json()

# ---- Naviga calls ---------------------------------------------------------------
def orderlist_by_po(po):
    data = naviga_get("/api/book/orderlist", {
        "PONumber": po, "OrderReturnOption": "2", "OpenOrderStatusOption": "2",
        "OrderOpenStatusSelection": "2", "IncludeDeletedOrders": "false"})
    return data.get("Orders") or []

def booksuborder(order_id, customer_id):
    return naviga_get("/api/book/booksuborder",
                      {"WebsiteID": WEBSITE_ID, "ID": order_id, "CustomerID": customer_id})

def account_email(customer_id):
    """On-file email(s) for a customer. Resilient: never breaks the tracking response."""
    try:
        data = naviga_get("/api/customerservice/account", {"id": customer_id, "WebsiteID": WEBSITE_ID})
    except Exception:
        return None, []
    emails = data.get("Emails") or []
    addrs = [e.get("Address") for e in emails if e.get("Address")]
    preferred = next((e.get("Address") for e in emails if e.get("IsPreferred")), None) or (addrs[0] if addrs else None)
    return preferred, addrs

# ---- shape the slim, speakable response -----------------------------------------
def carrier_of(ref):
    if not ref: return None
    if ref.startswith("1Z"): return "UPS"
    if ref.startswith("9"):  return "USPS"
    return "FedEx"

def tracking_url(carrier, ref):
    if not (carrier and ref): return None
    return {"UPS":  f"https://www.ups.com/track?tracknum={ref}",
            "USPS": f"https://tools.usps.com/go/TrackConfirmAction?tLabels={ref}",
            "FedEx":f"https://www.fedex.com/fedextrack/?trknbr={ref}"}.get(carrier)

def consolidate(detail, order=None):
    if not detail or not detail.get("OrderID"):
        return {"found": False, "status": "not_found", "message": "No order found for that request."}
    if (detail.get("OrderTypeID") == "P"
            or "PRO FORMA" in (detail.get("OrderTypeDescription") or "").upper()):
        return {"found": True, "status": "proforma",
                "message": "That order is still a proforma and hasn't shipped yet."}
    lineitems = detail.get("LineItems") or []
    if any("Cancel" in (li.get("StatusDescription") or li.get("LineStatus") or "") for li in lineitems):
        return {"found": True, "status": "cancelled",
                "message": "There's a cancellation noted on that order."}

    items = [(li.get("ProductTitle") or li.get("Title")) for li in lineitems
             if (li.get("ProductTitle") or li.get("Title"))]
    # Collect ALL trackings across ALL picklists — an order can ship in several packages,
    # and one picklist can carry multiple tracking numbers. Prefer Naviga's own Link.
    picklists = detail.get("PickLists") or []
    trackings, ship_date = [], None
    for pl in picklists:
        ship_date = ship_date or pl.get("ShipDate")
        for t in (pl.get("PicklistTracking") or []):
            ref = t.get("Reference")
            if not ref:
                continue
            car = carrier_of(ref)
            # Build the URL ourselves — Naviga's FedEx Link is malformed
            # (http://www.fedex.com/Tracking<num>); our tracking_url() is correct per carrier.
            trackings.append({"carrier": car, "reference": ref,
                              "url": tracking_url(car, ref)})
    ship_date = ship_date or detail.get("ShipDate")
    carrier = trackings[0]["carrier"] if trackings else None

    def _num(x):
        try: return float(x or 0)
        except (TypeError, ValueError): return 0.0
    ordered = sum(_num(li.get("QuantityOrdered")) for li in lineitems)
    shipped = sum(_num(li.get("QuantityShipped")) for li in lineitems)
    if ordered > 0:                       # quantity data present -> exact
        status = "preparing" if shipped <= 0 else ("partial" if shipped < ordered else "shipped")
    else:                                 # fallback: infer from trackings
        status = "shipped" if trackings else "preparing"

    out = {"found": True, "status": status,
           "order_id": detail.get("OrderID"),                # this is the invoice / order id
           "po_number": detail.get("PONumber"),
           "customer_name": detail.get("SoldToCustomerName") or detail.get("BillToCustomerName"),
           "items": items,
           "order_date": (order or {}).get("OrderDate") or detail.get("OrderDate"),
           "ship_date": ship_date,
           "carrier": carrier,
           "balance": detail.get("BalanceAmount", (order or {}).get("BalanceAmount")),
           "order_status": (order or {}).get("OrderStatusDescription") or detail.get("OrderStatusDescription"),
           # tracking_present reflects REAL tracking references, not just a picklist.
           "tracking_present": bool(trackings),
           "tracking_count": len(trackings),
           "tracking_url": trackings[0]["url"] if trackings else None,  # first (single-link / back-compat)
           "trackings": trackings,        # ALL tracking numbers + links
           "message": None}
    return out

# ---- core: resolve identifiers, then look up ------------------------------------
def track(payload):
    # Naviga PONumber lookups are CASE-SENSITIVE: "P202702136" matches, "p202702136" returns
    # nothing. POs can contain letters, so normalize to uppercase (digits are unaffected).
    po   = (payload.get("ponumber")   or "").strip().replace(" ", "").replace("-", "").upper()
    cust = (payload.get("customerid") or "").strip()
    inv  = (payload.get("invoiceid")  or "").strip()
    order = None
    if po:
        orders = orderlist_by_po(po)
        if not orders:
            return {"found": False, "status": "not_found",
                    "message": "I'm not finding an order under that P O number."}
        order = orders[0]
        order_id, customer_id = order.get("OrderID"), order.get("BillToCustomerID")
    elif cust and inv:
        order_id, customer_id = inv, cust
    else:
        return {"found": False, "status": "error",
                "message": "Please provide a P O number, or a customer ID and invoice number."}
    result = consolidate(booksuborder(order_id, customer_id), order)
    # Include the on-file email so the flow can offer to send it without a second call.
    # Set {"include_email": false} in the request to skip (saves ~one Naviga call).
    if result.get("found") and payload.get("include_email", True):
        preferred, addrs = account_email(customer_id)
        result["preferred_email"] = preferred
        result["emails"] = addrs
    return result

def handle():
    if request.headers.get("X-Api-Key") != PROXY_API_KEY:
        # `message` is read aloud by the agent, so keep it speakable; `detail` is for devs.
        return jsonify({"found": False, "status": "error",
                        "message": "I'm sorry, I can't reach the order system right now.",
                        "detail": "unauthorized"}), 200
    try:
        return jsonify(track(request.get_json(force=True, silent=True) or {})), 200
    except requests.HTTPError:
        return jsonify({"found": False, "status": "error", "message": "The order system is temporarily unavailable."}), 200
    except Exception:
        return jsonify({"found": False, "status": "error", "message": "Something went wrong looking that up."}), 200

@app.route("/order-tracking", methods=["POST"])
def order_tracking():   return handle()

@app.route("/invoice-tracking", methods=["POST"])
def invoice_tracking(): return handle()

@app.route("/health")
def health(): return "ok", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
