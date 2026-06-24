"""
Request/response contracts for the Bridge REST API.

These models are the boundary between the voice agent (via the platform's webhooks)
and the Bridge. Security rule: no agent-facing model exposes `max_buy` / `max_rate`.
The ceiling lives only in the server-side engine.
"""
import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator


def _to_amount(v):
    """Clean amounts that arrive by voice: '$3,900' / '2,500' / 2500 -> float; '' / None -> None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = re.sub(r"[^0-9.]", "", str(v))
    return float(s) if s else None


class Place(BaseModel):
    city: Optional[str] = None
    state: Optional[str] = None
    zip: Optional[str] = None


class Load(BaseModel):
    """A load as the agent and carrier see it. No `max_buy`, by design."""
    load_id: str
    origin: Place
    destination: Place
    equipment: Optional[str] = None
    posted_rate: Optional[int] = None
    miles: Optional[int] = None
    status: Optional[str] = None
    pickup_dt: Optional[str] = None
    # extras only LOAD_GET returns
    delivery_dt: Optional[str] = None
    weight: Optional[int] = None
    pieces: Optional[int] = None
    commodity: Optional[str] = None
    dims: Optional[str] = None
    notes: Optional[str] = None


# /verify-carrier (FMCSA)
class VerifyCarrierRequest(BaseModel):
    mc_number: str = Field(..., description="MC number as spoken by the carrier (normalized).")


class VerifyCarrierResponse(BaseModel):
    eligible: bool = Field(..., description="True if the carrier has active authority to operate.")
    mc_number: str
    carrier_name: Optional[str] = None
    dot_number: Optional[str] = None
    authority_status: str = Field(..., description="ACTIVE / INACTIVE / NOT_FOUND / UNKNOWN.")
    reason: Optional[str] = None


# /otp/request + /otp/verify (identity via the platform's native SMS)
class OTPRequestRequest(BaseModel):
    call_id: str = Field(..., description="Stable identifier for the call.")
    mc_number: Optional[str] = Field(None, description="Carrier MC (informational).")


class OTPRequestResponse(BaseModel):
    to: str = Field(..., description="On-file number (E.164) the Send SMS node delivers the code to.")
    code: str = Field(..., description="Code to send by SMS. NB: consumed by the Send SMS node as "
                                       "@request_otp.code; the agent never sees it (not the last child).")
    masked: str = Field("", description="Last 4 digits of the on-file number.")


class OTPVerifyRequest(BaseModel):
    call_id: str
    otp_code: str = Field(..., description="Code the carrier reads aloud (stripped to digits).")

    @field_validator("otp_code", mode="before")
    @classmethod
    def _clean_code(cls, v):
        return "".join(c for c in str(v if v is not None else "") if c.isdigit())


class OTPVerifyResponse(BaseModel):
    otp_verified: bool
    attempts_left: int


# /loads/search (LOAD_QUERY)
class LoadSearchRequest(BaseModel):
    # What the agent sends (natural language; the Bridge normalizes it).
    origin: Optional[str] = Field(None, description="e.g. 'Atlanta, GA' / 'GA' / 'Atlanta'.")
    destination: Optional[str] = None
    equipment_type: Optional[str] = Field(None, description="e.g. 'Dry Van' -> DRY_VAN.")
    pickup_date: Optional[str] = Field(None, description="Ignored unless it's a TMS date (YYYYMMDD...).")
    # Structured form (optional; still supported).
    origin_state: Optional[str] = None
    origin_city: Optional[str] = None
    dest_state: Optional[str] = None
    dest_city: Optional[str] = None
    max_results: int = Field(5, ge=1, le=25)


class LoadSearchResponse(BaseModel):
    count: int
    loads: list[Load]
    lane_match: Literal["exact", "origin", "none"] = Field(
        "exact",
        description="'exact' = full lane; 'origin' = no exact destination, loads leaving the origin; 'none' = nothing.",
    )
    note: Optional[str] = Field(None, description="Hint for the agent when the match was relaxed.")


# /negotiate
class NegotiateRequest(BaseModel):
    call_id: str = Field(..., description="Stable identifier for the call (server-side state).")
    load_id: str
    carrier_offer: Optional[float] = Field(
        None, description="What the carrier is asking. Empty = opening move (anchor to posted)."
    )

    @field_validator("carrier_offer", mode="before")
    @classmethod
    def _clean_carrier_offer(cls, v):
        return _to_amount(v)


class NegotiateResponse(BaseModel):
    decision: Literal["accept", "counter", "reject", "hold"]
    round: int = Field(..., description="Counter round in progress (1..max_rounds).")
    rounds_left: int = Field(..., description="Counters the carrier has left.")
    final: bool = Field(False, description="True if this is the broker's last offer.")
    agreed_rate: Optional[float] = Field(None, description="Settled rate (on accept/hold).")
    counter_offer: Optional[float] = Field(None, description="Broker's counter (on counter).")
    # NB: `max_buy` never appears here.


# /book (LOAD_BOOK)
class BookRequest(BaseModel):
    call_id: str
    load_id: str
    mc_number: str
    agreed_rate: float

    @field_validator("agreed_rate", mode="before")
    @classmethod
    def _clean_agreed_rate(cls, v):
        return _to_amount(v)


class BookResponse(BaseModel):
    status: Literal["booked", "already_booked", "rejected"]
    load_id: str
    agreed_rate: float
    booking_ref: Optional[str] = None
    timestamp: Optional[str] = None
    reason: Optional[str] = None


# /health
class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    tms: Literal["up", "down"]
    tms_detail: Optional[str] = None
