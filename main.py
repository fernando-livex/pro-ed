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
import os, re, time, threading
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

class NavigaMismatch(Exception):
    """The customer ID is real but isn't the account on that order (often bill-to vs ship-to)."""

class NavigaNoSuchRecord(Exception):
    """No order/invoice exists with that ID."""

def naviga_get(path, params):
    """GET with one automatic fresh-token retry on 401 (the intermittent-401 killer)."""
    for attempt in (0, 1):
        token = get_token(force=(attempt == 1))
        r = requests.get(f"{BASE}{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params=params, timeout=TIMEOUT)
        if r.status_code == 401 and attempt == 0:
            continue  # stale/rejected token -> refetch fresh and retry once
        # Naviga answers a BAD IDENTIFIER with HTTP 500 + a validation message, not a 404.
        # Left as an HTTPError these read to the caller as "the system is down", when really
        # they just mistyped a number -- so classify them and let the agent offer a retry.
        if r.status_code == 500:
            body = (r.text or "")
            if "does not have access to order" in body:
                raise NavigaMismatch()
            if "Cannot read data with ID" in body:
                raise NavigaNoSuchRecord()
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


def zip_from_address(addr):
    """Naviga has no zip field; the address string ends with it, e.g.
    "...|PORTLAND, OR 97217". Take the last 5-digit group."""
    if not addr:
        return ""
    if isinstance(addr, (list, tuple)):          # Naviga returns the address as lines
        addr = " ".join(str(x) for x in addr if x)
    m = re.findall(r"\b(\d{5})(?:-\d{4})?\b", str(addr))
    return m[-1] if m else ""


def po_variants(raw):
    """Naviga stores POs EXACTLY as the customer wrote them, including hyphens
    (e.g. "142-71361" matches; "14271361" does not). But callers also read digits
    aloud with separators. So try the PO as given first, then de-hyphenated.
    Spaces are never part of a PO. Uppercase: Naviga PONumber is case-sensitive."""
    s = re.sub(r"\s+", "", (raw or "").strip().upper())
    out, seen = [], set()
    for cand in (s, s.replace("-", "")):
        if cand and cand not in seen:
            seen.add(cand)
            out.append(cand)
    return out


def pick_by_zip(cands, zip_in):
    """Choose among orders that share a PO, using the caller's ship-to zip.

    Cascade: a full 5-digit match wins outright; otherwise fall back to the first
    three digits, because regional accounts span many zip codes and the caller may
    give a local zip while the account is registered elsewhere. Callers are asked
    for the WHOLE zip - the 3-digit tolerance is deliberately invisible to them.
    `cands` is already sorted most-recent-first, so [0] is the latest match.
    """
    if len(zip_in) >= 5:
        exact = [c for c in cands if c[2] == zip_in[:5]]
        if exact:
            return exact[0]
    z3 = zip_in[:3]
    if z3:
        pref = [c for c in cands if c[2].startswith(z3)]
        if pref:
            return pref[0]
    return None


def search_customer(name):
    """Resolve a caller-spoken last name (or company) to their BILL-TO account.

    /api/customerservice/search?Name= is a case-insensitive PREFIX match on last name or
    company name. It returns ONE account or the sentinel BillToCustomerID 0 -- never a list,
    and never any hint that the match was ambiguous ("Queen Anne" -> "QUEEN ANNE'S COUNTY").
    So the id it hands back is a CANDIDATE only. The caller is not verified by this call and
    the account is not confirmed: booksuborder does that, by rejecting an account that does
    not own the invoice. Returns (customer_id, zips) with zips from the account addresses.
    """
    if not name:
        return "", []
    try:
        data = naviga_get("/api/customerservice/search", {"WebsiteID": WEBSITE_ID, "Name": name})
    except Exception:
        return "", []
    cid = data.get("BillToCustomerID") or data.get("CustomerID") or 0
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        cid = 0
    if cid <= 0:                       # 0 is Naviga's "no match" sentinel
        return "", []
    zips = [str(a.get("PostCode") or "") for a in (data.get("Addresses") or []) if a.get("PostCode")]
    return str(cid), zips

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
    ship_addr = (picklists[0].get("ShipToCustomerAddress") if picklists else "") or detail.get("SoldToCustomerAddress") or ""
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
    # Naviga marks online/download licences fulfilled by QUANTITY but never produces a tracking
    # number for them -- e.g. "EDMARK 2E ONLINE" orders 3141630 / 3140117 / 3139882.
    # We cannot prove an item is digital (the same product books under both "Customer pick up" and
    # "BEST WAY", and every Download* field reads 0 even on the digital order), but we CAN say we
    # have no shipment to describe -- so don't let the flow claim one, or read a null carrier.
    # NB: a ship date is NOT a usable signal here -- order 3141630's picklist carries
    # ShipDate 2026-09-15 with PicklistTracking []. Absence of tracking is the discriminator.
    if status == "shipped" and not trackings:
        status = "fulfilled_no_tracking"

    out = {"found": True, "status": status,
           "order_id": detail.get("OrderID"),                # this is the invoice / order id
           "po_number": detail.get("PONumber"),
           "customer_name": detail.get("SoldToCustomerName") or detail.get("BillToCustomerName"),
           "items": items,
           "order_date": (order or {}).get("OrderDate") or detail.get("OrderDate"),
           "ship_date": ship_date,
           "carrier": carrier,
           "ship_to_zip": zip_from_address(ship_addr),
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
    po_raw = payload.get("ponumber") or ""
    cust = (payload.get("customerid") or "").strip()
    inv  = (payload.get("invoiceid")  or "").strip()
    order = None
    # Resolve the PO -> candidate orders (a PO is NOT unique; it can match several).
    # Caller is asked for the full zip; we match on 5 digits first, then the first 3.
    zip_in = re.sub(r"\D", "", str(payload.get("zip") or payload.get("zip3") or ""))
    po, orders = "", []
    for cand in po_variants(po_raw):
        orders = orderlist_by_po(cand)
        if orders:
            po = cand
            break
    if po_raw:
        if not orders:
            return {"found": False, "status": "not_found",
                    "message": "I'm not finding an order under that P O number."}
        if len(orders) > 1:
            # Several orders share this PO. The caller's ship-to zip is what tells them apart.
            if not zip_in:
                return {"found": False, "status": "multiple", "match_count": len(orders),
                        "message": (f"I found {len(orders)} orders under that P O number. "
                                    "What is the ship-to zip code?")}
            # Most recent first, so ties resolve to the latest order.
            orders = sorted(orders, key=lambda o: str(o.get("OrderDate") or ""), reverse=True)
            cands = []
            for o in orders:
                det = booksuborder(o.get("OrderID"), o.get("BillToCustomerID"))
                pls = det.get("PickLists") or []
                addr = (pls[0].get("ShipToCustomerAddress") if pls else "") or det.get("SoldToCustomerAddress") or ""
                cands.append((o, det, zip_from_address(addr)))
            picked = pick_by_zip(cands, zip_in)
            if not picked:
                return {"found": False, "status": "not_found", "match_count": len(orders),
                        "message": ("I'm not finding an order under that P O number "
                                    "with that ship-to zip code.")}
            order, detail, _z = picked
            result = consolidate(detail, order)
            if result.get("found") and payload.get("include_email", True):
                preferred, _ = account_email(order.get("BillToCustomerID"))
                result["preferred_email"] = preferred or ""
            result["match_count"] = len(orders)
            result["zip_match"] = "exact" if (len(zip_in) >= 5 and _z == zip_in[:5]) else "prefix3"
            return result
        order = sorted(orders, key=lambda o: str(o.get("OrderDate") or ""), reverse=True)[0]
        order_id, customer_id = order.get("OrderID"), order.get("BillToCustomerID")
    elif inv and (cust or payload.get("lastname")):
        customer_id = cust
        if not customer_id:
            # No customer number -- resolve the account from the name the caller gave.
            # Deliberately NOT gated on the zip: a name match plus an invoice that Naviga
            # agrees belongs to that account is the proof. The zip is the caller-verification
            # factor and is reported below, exactly as on the P O path.
            customer_id, acct_zips = search_customer(payload.get("lastname"))
            if not customer_id:
                return {"found": False, "status": "not_found", "reason": "name_no_match",
                        "message": ("I'm not finding an account under that name. "
                                    "I can try a different spelling, or the account number "
                                    "from your invoice.")}
        order_id = inv
    else:
        # NOT status "error": the flow speaks an outage line for that, and conversation
        # 05b566c7 showed a caller told "we're having trouble reaching the system" when the
        # real problem was that customer_id never made it into memory. not_found routes to
        # the branch that re-asks and offers alternatives, which is what should happen.
        return {"found": False, "status": "not_found", "reason": "missing_identifier",
                "message": ("I didn't catch that last number. I can take your purchase order "
                            "number, or your order number along with the name on the account.")}
    result = consolidate(booksuborder(order_id, customer_id), order)
    # Include the on-file email so the flow can offer to send it without a second call.
    # Set {"include_email": false} in the request to skip (saves ~one Naviga call).
    if result.get("found") and payload.get("include_email", True):
        preferred, addrs = account_email(customer_id)
        result["preferred_email"] = preferred
        result["emails"] = addrs
    # Only ONE order matched, so the zip isn't needed to disambiguate. Report how well it
    # lined up anyway (never blocks the answer) so we can see, from real calls, whether
    # requiring a zip match on unique lookups would be safe to turn on later.
    if zip_in and result.get("found"):
        z = result.get("ship_to_zip") or ""
        result["zip_match"] = ("exact"   if len(zip_in) >= 5 and z == zip_in[:5]
                               else "prefix3" if z and z.startswith(zip_in[:3])
                               else "mismatch")
    return result

def handle():
    if request.headers.get("X-Api-Key") != PROXY_API_KEY:
        # `message` is read aloud by the agent, so keep it speakable; `detail` is for devs.
        return jsonify({"found": False, "status": "error",
                        "message": "I'm sorry, I can't reach the order system right now.",
                        "detail": "unauthorized"}), 200
    try:
        return jsonify(track(request.get_json(force=True, silent=True) or {})), 200
    except NavigaMismatch:
        # NOT a system failure -- the agent should offer another number, not apologise for an outage.
        return jsonify({"found": False, "status": "not_found", "reason": "customer_mismatch",
                        "message": ("That customer number isn't the account on that order. "
                                    "If the order was placed by another location, the billing "
                                    "account number is the one to use.")}), 200
    except NavigaNoSuchRecord:
        return jsonify({"found": False, "status": "not_found", "reason": "no_such_record",
                        "message": "I'm not finding an order or invoice with that number."}), 200
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
