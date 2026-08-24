# Pro-Ed Naviga tracking proxy

A tiny, deterministic HTTP service that collapses the Naviga chain
(`token → orderlist → booksuborder`) into **one call** the LiveX voice agent makes.
The proxy holds the Naviga creds, caches + refreshes the token, and **retries once on
a 401 with a fresh token** — the resilience the LLM-driven flow couldn't do.

## Endpoints
Both accept EITHER a PO number OR customer id + invoice id. Auth: `X-Api-Key` header.

```
POST /order-tracking
POST /invoice-tracking
  { "ponumber": "10446576" }
  # or
  { "customerid": "00829707", "invoiceid": "3132701" }
```

Response (always HTTP 200 — never a raw 401 to the agent):
```json
{
  "found": true,
  "status": "shipped",         // shipped | partial | preparing | proforma | cancelled | not_found | error
  "items": ["Tongue Thrust Book-Oral Myofunc Ther-2E"],
  "order_date": "2026-06-24T00:00:00-05:00",
  "carrier": "FedEx",          // UPS | USPS | FedEx | null
  "ship_date": "…",
  "tracking_present": true,
  "tracking_url": "https://www.fedex.com/fedextrack/?trknbr=…",
  "balance": 64.0,             // present on PO lookups (invoice-relevant)
  "order_status": "Posted",
  "preferred_email": "j.smith@…",  // on-file email (customerservice/account)
  "emails": ["j.smith@…"],         // all on-file addresses
  "message": null              // set to a ready-to-speak line on the error/edge statuses
}
```
One call covers all three needs: **PO lookup** (orderlist), **order lookup** (booksuborder),
and **email account details** (customerservice/account). Send `{"include_email": false}` to
skip the account call and save ~one Naviga round-trip when you don't need the address.

## Run locally (free, for testing — Cloudflare tunnel)
No cloud account, no cold starts. Runs on your machine; a Cloudflare quick tunnel gives
it a public HTTPS URL for the voice agent to call.
```bash
brew install cloudflared               # one-time
cp .env.example .env                    # then set NAVIGA_CLIENT_SECRET + PROXY_API_KEY
bash run-local.sh                       # Terminal 1 — serves on :8080
cloudflared tunnel --url http://localhost:8080   # Terminal 2 — prints an https://<...>.trycloudflare.com URL
```
Keep both terminals up while testing (laptop on). The tunnel URL changes each time you
restart `cloudflared` — paste the current one to the SE to wire the flow.

## Deploy (Cloud Run, no Dockerfile — buildpacks)
From this directory:
```bash
gcloud run deploy proed-naviga-proxy \
  --source . --region us-central1 --allow-unauthenticated \
  --project <your-gcp-project> \
  --set-env-vars NAVIGA_CLIENT_ID=<your-naviga-client-id>,NAVIGA_WEBSITE_ID=187 \
  --set-secrets NAVIGA_CLIENT_SECRET=naviga-client-secret:latest,PROXY_API_KEY=proed-proxy-key:latest
```
- Put the Naviga secret and your chosen proxy key in Secret Manager first
  (`gcloud secrets create naviga-client-secret --data-file=-` etc.), or use
  `--set-env-vars` for a quick test (less safe). **Never commit the secret.**
- `--allow-unauthenticated` is fine because the `X-Api-Key` header gates it; if you
  prefer, drop it and put an API Gateway / IAM in front.
- The service listens on `$PORT` (Cloud Run sets it) via gunicorn (`Procfile`).

## Smoke test
```bash
URL=https://proed-naviga-proxy-xxxx.us-central1.run.app
curl -s -X POST "$URL/order-tracking" -H "X-Api-Key: <key>" \
  -H "Content-Type: application/json" -d '{"ponumber":"10446576"}' | jq
curl -s -X POST "$URL/order-tracking" -H "X-Api-Key: <key>" \
  -H "Content-Type: application/json" -d '{"customerid":"00829707","invoiceid":"3132701"}' | jq
```

## Then wire the LiveX flow
Hand the SE the deployed URL + the `X-Api-Key` value. The single-call flow
(`bench-Vdet.json`) points its one `external_api_call_tool` at `POST <URL>/order-tracking`
with header `X-Api-Key: <key>` and body `{"ponumber":"$po_number$"}` (order path) or
`{"customerid":"$customer_id$","invoiceid":"$invoice_number$"}` (invoice path).

## Notes / refinements
- The `partial` status is a heuristic (picklists < line items) — refine against a real
  partial-shipment payload if you have one.
- `invoice-tracking` currently shares the order logic; if invoice tracking needs
  different fields (balance/payment focus), branch `handle()` per route.
- Token is cached per warm instance (≤1h). Cold starts fetch once. The 401-retry means
  a mid-life token rejection self-heals without the agent ever seeing it.
