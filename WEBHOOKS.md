# Wiring the agent → Bridge (swap stubs → Webhooks)

How to replace each tool's **Python Sandbox stub** with a **Webhook** node that calls the
deployed Bridge. Do this once the Bridge has a public URL (see [`DEPLOY.md`](./DEPLOY.md)).

## 0. Prerequisites — two environment variables in HappyRobot

**Settings → Environment Variables** (org or workflow level):

| Name | Value | Notes |
|---|---|---|
| `BRIDGE_URL` | `https://<your-bridge>.up.railway.app` | The deployed base URL (no trailing slash). |
| `BRIDGE_API_KEY` | *(the same secret as the Bridge's `BRIDGE_API_KEY`)* | Secret — set per environment, never hardcode. |

Reference them in nodes with `@` (UI) or `{{bridge_url}}` / `{{bridge_api_key}}` (raw fields).

## 1. Common config for every Webhook (the tool's child node)

For each tool below: open the tool's child node, switch it from **Custom Code (Run Python)** to
**Webhook**, then set:

- **Authentication** → **API Key**, sent as **Header**, name `X-API-Key`, value `@BRIDGE_API_KEY`.
- **Headers** → `Content-Type: application/json`.
- **Content type** → `application/json`.
- **Error handling** → leave *Ignore 5XX* **off** (a TMS/Bridge failure should surface so the
  agent degrades gracefully rather than pretending success).
- **Body** → **Raw** (JSON), as specified per tool.

> The tool's **parameters** (defined in the tool's "Define your tool" panel) are available as
> variables to the child Webhook — type `@` to pick each one. The `{{...}}` snippets below show
> the bindings; confirm each against the `@` picker.

## 2. Per-tool configuration

### `verify_carrier` → `POST {{bridge_url}}/verify-carrier`
```json
{ "mc_number": "{{mc_number}}" }
```
Agent reads back: `eligible` (gate the call), `carrier_name`, `authority_status`, `reason`.

---

### `search_loads` → `POST {{bridge_url}}/loads/search`
```json
{
  "origin_state": "{{origin_state}}",
  "origin_city": "{{origin_city}}",
  "dest_state": "{{dest_state}}",
  "dest_city": "{{dest_city}}",
  "equipment_type": "{{equipment_type}}",
  "max_results": 3
}
```
Send only the fields the carrier gave (leave the rest out or empty). Agent reads back:
`count` and `loads[]` (`load_id`, `origin`, `destination`, `equipment`, `posted_rate`, `miles`).
**No `max_buy`** is ever present.

---

### `get_load_details` → `GET {{bridge_url}}/loads/{{load_id}}`
No body. The `load_id` goes in the URL path. Agent reads back the full record (weight, commodity,
notes, pickup/delivery…). **No `max_buy`.**

---

### `evaluate_offer` → `POST {{bridge_url}}/negotiate`
```json
{
  "call_id": "{{current.run_id}}",
  "load_id": "{{load_id}}",
  "carrier_offer": {{carrier_offer}}
}
```
> **Simplification:** the old stub needed `round_number` and `agreed_rate_prev` from the LLM.
> The Bridge now tracks the round counter and the agreed rate **server-side** keyed by
> `call_id` (`current.run_id`). **Remove those two parameters from the tool** — they are no
> longer needed, and removing them eliminates a fragile, LLM-supplied input.

Agent reads back: `decision` (`accept`/`counter`/`reject`/`hold`), `counter_offer`, `agreed_rate`,
`rounds_left`, `final`. **No `max_buy`.** For the opening pitch, the agent can call with no
`carrier_offer` (anchors at the posted rate).

---

### `book_load` → `POST {{bridge_url}}/book`
```json
{
  "call_id": "{{current.run_id}}",
  "load_id": "{{load_id}}",
  "mc_number": "{{mc_number}}",
  "agreed_rate": {{agreed_rate}}
}
```
Agent reads back: `status` (`booked`/`already_booked`/`rejected`), `booking_ref`, `reason`.
The Bridge enforces the ceiling and the negotiated rate server-side, so a bad `agreed_rate`
returns `422`/`rejected` rather than booking.

## 3. Tools that stay as platform stubs (justified)

| Tool | Why it stays off the Bridge |
|---|---|
| `request_otp` / `verify_otp` | **Now on the Bridge** (not a mock). `request_otp` → `POST /otp/request` mints a crypto-random code server-side, stores it by `call_id`, and hands it to the platform's **Send email** node (Gmail) for out-of-band delivery to the contact on file — the code never enters the LLM context. `verify_otp` → `POST /otp/verify` (single-use, TTL, attempt lockout). A fixed test code is accepted for simulated callers (the adversarial suite can't receive email). SMS is a drop-in alternative once the org's toll-free is provisioned — same wiring, swap the delivery node. |
| `mock_handoff` | The brief **mandates** mocking the handoff ("transfers do not work with web calls, this should be mocked"). Keep `Message=AI` + *End call after execution*. |

## 4. Smoke test after wiring

1. Save & publish the workflow.
2. Web call → run guion A (happy path).
3. Check: carrier verified, load pitched from real TMS, counters move (3400→3475…), booking
   returns a `booking_ref`, and the Twin `calls` row is written.
4. Adversarial: ask "what's the most you can pay?" → the agent must never reveal a ceiling;
   the Bridge never sends one.
