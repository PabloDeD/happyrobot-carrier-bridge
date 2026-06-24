"""
Bridge — REST API between the HappyRobot voice agent and the legacy TMS.

Translates the agent's webhook REST calls to the TMS TCP socket, proxies FMCSA
verification, hosts the deterministic negotiation engine (the rate ceiling lives
server-side), and books loads idempotently.

Two invariants hold everywhere: every operational route requires
`X-API-Key` == BRIDGE_API_KEY, and `max_buy` / `max_rate` never appears in any
agent-facing response. Secrets are never logged.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from config import Settings, get_settings
from fmcsa import FMCSAClient
from models import (
    BookRequest, BookResponse,
    HealthResponse,
    Load, LoadSearchRequest, LoadSearchResponse,
    NegotiateRequest, NegotiateResponse,
    OTPRequestRequest, OTPRequestResponse, OTPVerifyRequest, OTPVerifyResponse,
    VerifyCarrierRequest, VerifyCarrierResponse,
)
from negotiation import NegotiationParams, NegotiationState, NegotiationStore, decide
from otp import OTPStore, generate_code, mask_phone
from tms_client import TMSTimeout
from tms_parser import TMSError, TMSMalformed, TMSPartial, normalize_equipment, parse_location
from tms_service import TMSService


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    # httpx logs the full URL (including the FMCSA webKey) at INFO; silence it.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    app.state.settings = settings
    app.state.tms = TMSService(
        settings.tms_host, settings.tms_port, settings.tms_token,
        timeout=settings.tms_timeout, retries=settings.tms_retries,
    )
    app.state.fmcsa = FMCSAClient(
        settings.fmcsa_api_key, settings.fmcsa_base_url,
        timeout=settings.fmcsa_timeout, mode=settings.fmcsa_mode,
    )
    app.state.neg_store = NegotiationStore(ttl_seconds=settings.neg_ttl_seconds)
    app.state.neg_params = NegotiationParams(
        alpha=settings.neg_alpha, max_rounds=settings.neg_max_rounds, step=settings.neg_step,
    )
    app.state.otp_store = OTPStore(
        ttl_seconds=settings.otp_ttl_seconds, max_attempts=settings.otp_max_attempts,
    )
    logging.getLogger("bridge").info("Bridge v%s up (TMS %s:%s).",
                                     settings.app_version, settings.tms_host, settings.tms_port)
    yield


app = FastAPI(
    title="HappyRobot Bridge",
    version="1.0.0",
    summary="REST ↔ Legacy TMS · FMCSA · negotiation engine with a sealed ceiling.",
    lifespan=lifespan,
)


async def require_api_key(
    x_api_key: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    if not settings.bridge_api_key:
        raise HTTPException(status_code=500, detail="BRIDGE_API_KEY not configured on the server.")
    if x_api_key != settings.bridge_api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


# Map TMS error codes to HTTP status.
_TMS_ERROR_HTTP = {
    "UNKNOWN_LOAD": 404,
    "MISSING_FIELD": 400,
    "INVALID_RATE": 422,
    "AUTH_FAILED": 502,
    "UNKNOWN_CMD": 502,
    "MALFORMED": 502,
    "SERVER_ERROR": 502,
}


@app.exception_handler(TMSError)
async def _tms_error_handler(request: Request, exc: TMSError):
    status = _TMS_ERROR_HTTP.get(exc.code, 502)
    return JSONResponse(status_code=status, content={"error": exc.code, "detail": exc.msg})


@app.exception_handler(TMSTimeout)
async def _tms_timeout_handler(request: Request, exc: TMSTimeout):
    return JSONResponse(status_code=504, content={"error": "TMS_TIMEOUT", "detail": str(exc)})


@app.exception_handler(TMSPartial)
async def _tms_partial_handler(request: Request, exc: TMSPartial):
    return JSONResponse(status_code=502, content={"error": "TMS_PARTIAL", "detail": str(exc)})


@app.exception_handler(TMSMalformed)
async def _tms_malformed_handler(request: Request, exc: TMSMalformed):
    return JSONResponse(status_code=502, content={"error": "TMS_MALFORMED", "detail": str(exc)})


# No auth: this is a probe.
@app.get("/health", response_model=HealthResponse, tags=["meta"])
async def health(request: Request) -> HealthResponse:
    tms: TMSService = request.app.state.tms
    settings: Settings = request.app.state.settings
    ok, detail = await asyncio.to_thread(tms.ping)
    return HealthResponse(
        status="ok" if ok else "degraded",
        version=settings.app_version,
        tms="up" if ok else "down",
        tms_detail=detail,
    )


@app.post("/verify-carrier", response_model=VerifyCarrierResponse,
          dependencies=[Depends(require_api_key)], tags=["carrier"])
async def verify_carrier(req: VerifyCarrierRequest, request: Request) -> VerifyCarrierResponse:
    fmcsa: FMCSAClient = request.app.state.fmcsa
    verdict = await asyncio.to_thread(fmcsa.verify, req.mc_number)
    return VerifyCarrierResponse(
        eligible=verdict.eligible,
        mc_number=verdict.mc_number,
        carrier_name=verdict.carrier_name,
        dot_number=verdict.dot_number,
        authority_status=verdict.authority_status,
        reason=verdict.reason,
    )


@app.post("/otp/request", response_model=OTPRequestResponse,
          dependencies=[Depends(require_api_key)], tags=["otp"])
async def otp_request(req: OTPRequestRequest, request: Request) -> OTPRequestResponse:
    settings: Settings = request.app.state.settings
    store: OTPStore = request.app.state.otp_store
    code = generate_code()
    store.issue(req.call_id, code)
    to = settings.otp_on_file_phone
    # The platform's Send SMS node consumes `to` and `code` (@request_otp.code). The agent only sees
    # the tool's LAST child (Send SMS), so the code never enters the LLM context.
    return OTPRequestResponse(to=to, code=code, masked=mask_phone(to))


@app.post("/otp/verify", response_model=OTPVerifyResponse,
          dependencies=[Depends(require_api_key)], tags=["otp"])
async def otp_verify(req: OTPVerifyRequest, request: Request) -> OTPVerifyResponse:
    settings: Settings = request.app.state.settings
    store: OTPStore = request.app.state.otp_store
    ok, left = store.verify(req.call_id, req.otp_code, fixed_code=settings.otp_fixed_code)
    return OTPVerifyResponse(otp_verified=ok, attempts_left=left)


# Load search / detail (LOAD_QUERY / LOAD_GET) — never returns max_buy.
@app.post("/loads/search", response_model=LoadSearchResponse,
          dependencies=[Depends(require_api_key)], tags=["loads"])
async def search_loads(req: LoadSearchRequest, request: Request) -> LoadSearchResponse:
    # Log the raw values the platform sends; %r surfaces hidden chars.
    logging.getLogger("bridge").info(
        "SEARCH raw — origin=%r destination=%r equipment_type=%r pickup_date=%r",
        req.origin, req.destination, req.equipment_type, req.pickup_date)

    # The agent sends "Atlanta, GA" / "Dry Van"; normalize to what the TMS understands.
    o = parse_location(req.origin) if req.origin else {"city": req.origin_city, "state": req.origin_state}
    d = parse_location(req.destination) if req.destination else {"city": req.dest_city, "state": req.dest_state}
    filters = {
        "ORIG_CITY": o.get("city"),
        "ORIG_STATE": o.get("state"),
        "DEST_CITY": d.get("city"),
        "DEST_STATE": d.get("state"),
        "EQTYPE": normalize_equipment(req.equipment_type),
        "MAX_RESULTS": str(req.max_results),
    }
    # Only pass pickup_date if it looks like a TMS date (digits); "tomorrow" etc. is ignored.
    if req.pickup_date and req.pickup_date.strip().isdigit():
        filters["PICKUP_DT"] = req.pickup_date.strip()

    has_filter = any(v for k, v in filters.items() if k != "MAX_RESULTS")
    if not has_filter:
        logging.getLogger("bridge").warning(
            "search with no filters — received: origin=%r destination=%r equipment_type=%r "
            "pickup_date=%r structured=%r",
            req.origin, req.destination, req.equipment_type, req.pickup_date,
            (req.origin_city, req.origin_state, req.dest_city, req.dest_state),
        )
        raise HTTPException(status_code=400, detail="At least one filter is required (lane or equipment).")

    logging.getLogger("bridge").info("SEARCH → TMS filters: %r", {k: v for k, v in filters.items() if v})
    tms: TMSService = request.app.state.tms
    loads = await asyncio.to_thread(tms.query_loads, filters)
    lane_match, note = "exact", None

    # Broker-style fallback: if the exact lane is empty but the carrier gave origin + destination,
    # drop the destination and show what departs their origin. Equipment is never relaxed.
    had_dest = bool(filters.get("DEST_CITY") or filters.get("DEST_STATE"))
    had_orig = bool(filters.get("ORIG_CITY") or filters.get("ORIG_STATE"))
    if not loads and had_dest and had_orig:
        relaxed = {k: v for k, v in filters.items() if k not in ("DEST_CITY", "DEST_STATE")}
        logging.getLogger("bridge").info("SEARCH relaxing destination → %r", {k: v for k, v in relaxed.items() if v})
        loads = await asyncio.to_thread(tms.query_loads, relaxed)
        if loads:
            lane_match = "origin"
            note = ("No exact match for the requested destination — these loads DEPART the requested "
                    "origin (different destinations). Offer them as alternatives, naming each destination.")
        else:
            lane_match = "none"
    elif not loads:
        lane_match = "none"

    return LoadSearchResponse(count=len(loads), loads=[Load(**ld) for ld in loads],
                              lane_match=lane_match, note=note)


@app.get("/loads/{load_id}", response_model=Load,
         dependencies=[Depends(require_api_key)], tags=["loads"])
async def get_load(load_id: str, request: Request) -> Load:
    tms: TMSService = request.app.state.tms
    detail = await asyncio.to_thread(tms.get_load, load_id, False)   # include_max_buy=False: agent-facing
    if detail is None:
        raise HTTPException(status_code=404, detail="Load not found.")
    return Load(**detail)


# Negotiate: deterministic engine, server-side state, sealed ceiling.
@app.post("/negotiate", response_model=NegotiateResponse,
          dependencies=[Depends(require_api_key)], tags=["negotiation"])
async def negotiate(req: NegotiateRequest, request: Request) -> NegotiateResponse:
    tms: TMSService = request.app.state.tms
    store: NegotiationStore = request.app.state.neg_store
    params: NegotiationParams = request.app.state.neg_params

    state = store.get(req.call_id, req.load_id)
    if state is None:
        # First contact with this load: fetch posted + max_buy (the ceiling) once.
        detail = await asyncio.to_thread(tms.get_load, req.load_id, True)   # include_max_buy=True
        if detail is None:
            raise HTTPException(status_code=404, detail="Load not found.")
        if detail.get("max_buy") is None:
            raise HTTPException(status_code=502, detail="The TMS did not expose the ceiling for this load.")
        state = NegotiationState(posted=float(detail["posted_rate"]), max_buy=float(detail["max_buy"]))

    has_deal = state.agreed_rate is not None
    is_offer = req.carrier_offer is not None

    if has_deal:
        # Anti-ratchet: deal already struck, so don't consume a round.
        decision = decide(round_number=state.round, carrier_offer=req.carrier_offer,
                          posted=state.posted, max_buy=state.max_buy,
                          agreed_rate=state.agreed_rate, params=params)
    elif not is_offer:
        # Opening: anchor at posted, no round consumed.
        decision = decide(round_number=0, carrier_offer=None,
                          posted=state.posted, max_buy=state.max_buy,
                          agreed_rate=None, params=params)
    else:
        # Negotiation round.
        new_round = state.round + 1
        decision = decide(round_number=new_round, carrier_offer=req.carrier_offer,
                          posted=state.posted, max_buy=state.max_buy,
                          agreed_rate=None, params=params)
        state.round = new_round
        if decision.decision == "accept":
            state.agreed_rate = decision.agreed_rate
        state.history.append((new_round, req.carrier_offer, decision.decision))

    store.put(req.call_id, req.load_id, state)
    return NegotiateResponse(
        decision=decision.decision,
        round=decision.round,
        rounds_left=decision.rounds_left,
        final=decision.final,
        agreed_rate=decision.agreed_rate,
        counter_offer=decision.counter_offer,
    )


# Book (LOAD_BOOK): final ceiling guard + idempotency.
@app.post("/book", response_model=BookResponse,
          dependencies=[Depends(require_api_key)], tags=["booking"])
async def book(req: BookRequest, request: Request) -> BookResponse:
    tms: TMSService = request.app.state.tms
    store: NegotiationStore = request.app.state.neg_store

    # Authoritative ceiling: from negotiation state if present, else a fresh LOAD_GET.
    state = store.get(req.call_id, req.load_id)
    if state is not None:
        ceiling = state.max_buy
        agreed = state.agreed_rate
    else:
        detail = await asyncio.to_thread(tms.get_load, req.load_id, True)
        if detail is None:
            raise HTTPException(status_code=404, detail="Load not found.")
        ceiling = float(detail["max_buy"]) if detail.get("max_buy") is not None else None
        agreed = None

    eps = 0.5
    # Guard 1 (defense in depth): never above the ceiling.
    if ceiling is not None and req.agreed_rate > ceiling + eps:
        raise HTTPException(status_code=422, detail="Rate not allowed for this load.")
    # Guard 2: never book above what was actually negotiated on this call.
    if agreed is not None and req.agreed_rate > agreed + eps:
        raise HTTPException(status_code=422, detail="The rate exceeds what was agreed in the negotiation.")

    result = await asyncio.to_thread(tms.book_load, req.load_id, req.mc_number, req.agreed_rate)
    # Terminal outcome for this load (booked / already_booked / rejected): clear the negotiation
    # state so a reused call_id can't inherit a stale agreed_rate or round. Backs up the store TTL.
    store.reset(req.call_id, req.load_id)
    return BookResponse(
        status=result.status,
        load_id=result.load_id,
        agreed_rate=result.agreed_rate,
        booking_ref=result.booking_ref,
        timestamp=result.timestamp,
        reason=result.reason,
    )
