# HappyRobot Bridge

> Integration layer between the HappyRobot voice agent (**Sam**) and the broker's
> back-office systems: the **Legacy TMS** (raw TCP socket) and **FMCSA** (carrier
> authority). It also hosts the **deterministic negotiation engine** that enforces the
> rate ceiling server-side so it can never reach the LLM.

```
   Voice Agent (HappyRobot)                Bridge (this service)            Back-office
  ┌───────────────────────┐  HTTPS+APIKey ┌──────────────────────┐  TCP   ┌─────────────┐
  │ verify_carrier        │──────────────▶│ POST /verify-carrier │───────▶│   FMCSA     │
  │ search_loads          │──────────────▶│ POST /loads/search   │        │  (QCMobile) │
  │ get_load_details      │──────────────▶│ GET  /loads/{id}     │        └─────────────┘
  │ evaluate_offer        │──────────────▶│ POST /negotiate ◀─── techo (max_buy) server-side
  │ book_load             │──────────────▶│ POST /book           │───────▶┌─────────────┐
  └───────────────────────┘               │ GET  /health         │  TCP   │ Legacy TMS  │
                                          └──────────────────────┘        │ (sockets)   │
                                                                          └─────────────┘
```

## Why a Bridge?

The Legacy TMS speaks a fixed-width, line-based protocol over a raw TCP socket — no REST,
no JSON — and **injects faults** (timeouts, truncated and malformed responses) on every
operational command. On top of that, two requirements cannot be met inside the platform alone:

1. **The rate ceiling (`max_rate` / `max_buy`) must never reach the carrier — directly or
   indirectly.** A prompt-driven LLM cannot be trusted to hold a number it can see. The Bridge
   keeps the ceiling server-side and returns only the *decision*.
2. **A reliable counter-round counter and anti-ratchet** need mutable per-call state. The
   platform has no reliable intra-call mutable state (read-only `@` variables, stateless
   sandboxes), so the negotiation state lives here, keyed by `call_id`.

The Bridge is therefore both a **protocol adapter** (REST ↔ TCP, with graceful fault handling)
and the **guardian of the negotiation**.

## Endpoints

| Method & path | Tool | Purpose |
|---|---|---|
| `GET /health` | — | Liveness + TMS reachability (via `DEBUG_ECHO`, which bypasses fault injection). |
| `POST /verify-carrier` | `verify_carrier` | FMCSA authority lookup by MC number. |
| `POST /loads/search` | `search_loads` | `LOAD_QUERY` on the open board. **Never returns `max_buy`.** |
| `GET /loads/{load_id}` | `get_load_details` | `LOAD_GET` full record. **Never returns `max_buy`.** |
| `POST /negotiate` | `evaluate_offer` | Deterministic negotiation decision. **Never returns `max_buy`.** |
| `POST /book` | `book_load` | `LOAD_BOOK` with idempotency + final ceiling guard. |

All routes except `/health` require the header **`X-API-Key: <BRIDGE_API_KEY>`**.
Interactive API docs at `/docs` (FastAPI/OpenAPI).

## The negotiation engine (`negotiation.py`)

A pure function `decide(...)` plus a per-call state store. Invariants that always hold:

1. **`max_buy` is never returned and never enters the offer formula.** The broker's voluntary
   counter depends **only on the posted rate** — `offer = posted + α·posted·frac(round)` — so a
   carrier cannot invert observed counters to deduce the ceiling.
2. **Decreasing concessions** (`+75 / +50 / +25`): the carrier is not trained to keep pushing.
3. **Never offers or pays at/above the ceiling** — counters are clamped invisibly below it.
4. **Anti-ratchet**: once a rate is agreed, the engine never raises it; if the carrier later
   asks for *less*, the lower number is taken.
5. **Coverage stance** (a conscious, configurable trade-off): in the last round, an offer at or
   below the ceiling is accepted — covering the load is worth more than squeezing the last dollar.
   Tunable via `NEG_ALPHA` / `NEG_MAX_ROUNDS`.

Verified live against the real TMS (load `LD00324`, posted 3280, internal ceiling 3921):

```
round 1: carrier 4200 → counter 3400      (rounds_left 2)
round 2: carrier 4200 → counter 3475      (decreasing, derived from posted)
round 3: carrier 3900 → accept  3900      (≤ ceiling → coverage)
re-open: carrier 4000 → hold    3900      (anti-ratchet)
```

The ceiling `3921` appears in **no** response.

## Resilience to TMS faults (`tms_service.py`)

- **Reads** (`LOAD_QUERY` / `LOAD_GET`) are safe to retry → exponential backoff on timeout /
  partial / malformed responses. Business errors (`ERR|CODE|MSG`) are **not** retried.
- **Booking** (`LOAD_BOOK`) is not idempotent on the wire, but the TMS view is *monotonic per
  token* (once `BOOKED`, repeats return `ALREADY_BOOKED` — no double-booking). The service keeps
  an idempotency cache and treats an `ALREADY_BOOKED` that follows one of *our own* attempts as a
  success whose confirmation was truncated, so a fault can never produce a double booking or a
  lost outcome.

## Security

- **API-key auth** on every operational route (`X-API-Key`).
- **`max_buy` is omitted from every agent-facing response** (`normalize_load` drops it by default;
  no response model exposes it) and **`/book` enforces a final server-side ceiling guard** — even a
  direct call cannot book above `max_buy`, nor above the rate actually negotiated for that call.
- **Secrets** (TMS token, FMCSA key, Bridge key) come from the environment / `.env` (gitignored)
  and are **never logged**.

## FMCSA — note on access

The FMCSA QCMobile API (`mobile.fmcsa.dot.gov`) is **geo-restricted to US IPs** (its AWS WAF
returns `403` to non-US traffic, independent of the API key). The client targets the documented
endpoint and schema and works from a US egress — i.e. **from the Bridge deployed in a US region**.
For local development outside the US, set **`FMCSA_MODE=mock`** for a deterministic verdict
(MC `000000`/`111111` simulate inactive authority to exercise the "fails check → end call" path).
Production deploys use `FMCSA_MODE=live`. The client degrades gracefully (returns `UNKNOWN` with a
reason, never crashes) if FMCSA is unreachable.

## Run locally

```bash
cp .env.example .env          # fill TMS_*, FMCSA_API_KEY, BRIDGE_API_KEY
# outside the US, set FMCSA_MODE=mock in .env

# option A — virtualenv (Python 3.12/3.13)
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/uvicorn main:app --reload --port 8000

# option B — Docker (single command)
docker compose up --build
```

Smoke-test the live TMS connection without the API: `python smoke_test.py`.
Browse the load board by hand: `python ver_cargas.py` (list) / `python ver_cargas.py LD00324` (detail).

## Tests

```bash
./.venv/bin/python -m pytest        # 65 tests
```

Cover the negotiation invariants (decreasing concessions, no ceiling leak, anti-ratchet, coverage,
never-pay-above-ceiling sweep) and the per-call state store (TTL eviction so a reused `call_id`
can't inherit a stale rate), the TMS parser (framing, faults, padding, `max_buy` omission), booking
idempotency (fault → retry → `ALREADY_BOOKED` handled as success), the API contracts (auth,
`max_buy` never leaks, ceiling guard on `/book`), the OTP store (single-use, TTL, attempt lockout,
test fixture), and the FMCSA schema mapping + fail-closed mock mode.

## Deploy

See [`DEPLOY.md`](./DEPLOY.md) — Railway (US region) or any Docker host. To wire the platform tools
to this service, see [`WEBHOOKS.md`](./WEBHOOKS.md).

## Configuration (environment)

| Var | Default | Notes |
|---|---|---|
| `TMS_HOST` / `TMS_PORT` / `TMS_TOKEN` | — | Legacy TMS socket + bearer token (secret). |
| `TMS_TIMEOUT` / `TMS_RETRIES` | `8.0` / `3` | Per-attempt timeout; retries on faults (reads). |
| `FMCSA_API_KEY` | — | QCMobile webKey (secret). |
| `FMCSA_MODE` | `live` | `live` (US) or `mock` (local dev). |
| `BRIDGE_API_KEY` | — | Shared secret the platform Webhooks must send as `X-API-Key`. |
| `NEG_ALPHA` / `NEG_MAX_ROUNDS` / `NEG_STEP` | `0.08` / `3` / `25` | Negotiation dials. |

## Layout

```
bridge/
├── main.py            FastAPI app: routes, auth, TMS-error→HTTP handlers
├── config.py          settings (pydantic-settings, 12-factor)
├── models.py          request/response contracts (none exposes max_buy)
├── negotiation.py     pure decide() + per-call state store
├── tms_service.py     retries/backoff + booking idempotency over the socket
├── tms_client.py      raw TCP client (one request per connection, fault-aware)
├── tms_parser.py      response parser + normalize_load (omits max_buy)
├── fmcsa.py           QCMobile proxy + mock mode
├── tests/             pytest suite (42)
├── Dockerfile · docker-compose.yml · requirements.txt
└── smoke_test.py · explore.py · ver_cargas.py   (manual TMS exploration)
```
