"""
Service layer over the legacy TMS: turns business intent into socket commands, tolerates
the injected faults, and makes booking idempotent.

Reads (LOAD_QUERY / LOAD_GET) are safe to retry, so we retry them with exponential backoff
when a fault hits (timeout / partial / malformed).

Booking (LOAD_BOOK) isn't idempotent on the wire, but the TMS is monotonic per token: once a
load is BOOKED, a repeat returns ALREADY_BOOKED, so we can never double-book. That makes
retrying safe — the only exposure is losing the BOOKING_REF when a confirmation comes back
truncated. We cover it two ways: an idempotency cache, and treating an ALREADY_BOOKED that
follows one of our own attempts as success (ref just unavailable).

Business errors (ERR|CODE|MSG) are never retried — we interpret them and return.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from tms_client import send_request, TMSTimeout
from tms_parser import (
    parse_response, normalize_load, _dt,
    TMSError, TMSPartial, TMSMalformed,
)

log = logging.getLogger("bridge.tms")

_FAULTS = (TMSTimeout, TMSPartial, TMSMalformed)

# On reads, these codes are deterministic — don't retry them. Any other business error on a
# read is a spurious injected fault (e.g. 'MISSING_FIELD: Invalid EQTYPE' on a query whose
# filters the Bridge already validated), so we do retry it.
_FATAL_READ_CODES = {"UNKNOWN_LOAD", "AUTH_FAILED", "UNKNOWN_CMD"}


@dataclass
class BookResult:
    status: str                          # booked | already_booked | rejected
    load_id: str
    agreed_rate: float
    booking_ref: Optional[str] = None
    timestamp: Optional[str] = None
    reason: Optional[str] = None


class TMSService:
    def __init__(self, host: str, port: int, token: str,
                 timeout: float = 8.0, retries: int = 3) -> None:
        self._host = host
        self._port = port
        self._token = token
        self._timeout = timeout
        self._retries = max(1, retries)
        # Booking idempotency cache, token-scoped, keyed by load_id.
        self._bookings: dict[str, BookResult] = {}
        self._lock = threading.Lock()

    def _send(self, cmd: str, fields: dict) -> list[dict]:
        raw = send_request(self._host, self._port, cmd, self._token, fields, timeout=self._timeout)
        return parse_response(raw)

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(2.0, 0.25 * (2 ** attempt))

    def _read(self, cmd: str, fields: dict) -> list[dict]:
        """
        Idempotent read with retries. Retries both transport faults (timeout/partial/malformed)
        and non-deterministic business errors (injected faults that arrive as a spurious ERR);
        only the deterministic codes propagate.
        """
        last: Optional[Exception] = None
        for attempt in range(self._retries):
            try:
                return self._send(cmd, fields)
            except TMSError as exc:
                if exc.code in _FATAL_READ_CODES:
                    raise                   # deterministic (missing / auth) — don't retry
                last = exc                  # spurious injected ERR — retry
                log.info("%s transient ERR (%s), retry %d/%d", cmd, exc.code,
                         attempt + 1, self._retries)
                time.sleep(self._backoff(attempt))
            except _FAULTS as exc:
                last = exc
                log.info("%s fault (%s), retry %d/%d", cmd, type(exc).__name__,
                         attempt + 1, self._retries)
                time.sleep(self._backoff(attempt))
        assert last is not None
        raise last

    def query_loads(self, filters: dict) -> list[dict]:
        """LOAD_QUERY → normalized loads, without max_buy."""
        recs = self._read("LOAD_QUERY", {k: v for k, v in filters.items() if v not in (None, "")})
        return [normalize_load(r) for r in recs]

    def get_load(self, load_id: str, include_max_buy: bool = False) -> Optional[dict]:
        """LOAD_GET → normalized load, or None when there's no record. UNKNOWN_LOAD surfaces as TMSError."""
        recs = self._read("LOAD_GET", {"LOAD_ID": load_id})
        if not recs:
            return None
        return normalize_load(recs[0], include_max_buy=include_max_buy)

    def ping(self) -> tuple[bool, str]:
        """DEBUG_ECHO (fault-free) → TMS liveness and auth check."""
        try:
            recs = self._send("DEBUG_ECHO", {"MSG": "PING"})
        except TMSError as exc:
            return False, f"{exc.code}: {exc.msg}"
        except _FAULTS as exc:
            return False, type(exc).__name__
        ok = bool(recs) and recs[0].get("AUTH") == "OK"
        return ok, "AUTH:OK" if ok else "unexpected DEBUG_ECHO response"

    def book_load(self, load_id: str, mc_number: str, agreed_rate: float) -> BookResult:
        """
        Idempotent LOAD_BOOK. agreed_rate goes out as an integer — the wire carries no decimals
        in the examples. Safe to retry through faults without risking a double-booking.
        """
        with self._lock:
            cached = self._bookings.get(load_id)
            if cached and cached.status in ("booked", "already_booked"):
                return cached                      # already settled in this process

        fields = {"LOAD_ID": load_id, "MC_NUM": mc_number, "AGREED_RATE": str(int(round(agreed_rate)))}
        attempted = False                          # did we ever send a BOOK that could have landed?
        last_fault: Optional[Exception] = None

        for attempt in range(self._retries):
            try:
                recs = self._send("LOAD_BOOK", fields)
                rec = recs[0] if recs else {}
                result = BookResult(
                    status="booked", load_id=load_id, agreed_rate=agreed_rate,
                    booking_ref=(rec.get("BOOKING_REF") or "").strip() or None,
                    timestamp=_dt(rec.get("TIMESTAMP")),
                )
                return self._remember(load_id, result)

            except TMSError as exc:
                if exc.code == "ALREADY_BOOKED":
                    if attempted:
                        # An earlier BOOK of ours did land; its confirmation just came back truncated.
                        result = BookResult(
                            status="booked", load_id=load_id, agreed_rate=agreed_rate,
                            reason="Booked; reference unavailable (truncated TMS confirmation).",
                        )
                    else:
                        result = BookResult(
                            status="already_booked", load_id=load_id, agreed_rate=agreed_rate,
                            reason="The load was already booked on this token.",
                        )
                    return self._remember(load_id, result)
                if exc.code == "INVALID_RATE":
                    return BookResult("rejected", load_id, agreed_rate,
                                      reason="The TMS rejected the rate (INVALID_RATE).")
                if exc.code == "UNKNOWN_LOAD":
                    return BookResult("rejected", load_id, agreed_rate, reason="Load does not exist.")
                return BookResult("rejected", load_id, agreed_rate, reason=f"{exc.code}: {exc.msg}")

            except _FAULTS as exc:
                last_fault = exc
                attempted = True               # request went out: the booking may have landed
                log.info("LOAD_BOOK fault (%s), retry %d/%d", type(exc).__name__,
                         attempt + 1, self._retries)
                time.sleep(self._backoff(attempt))

        return BookResult("rejected", load_id, agreed_rate,
                          reason=f"TMS unreachable after {self._retries} attempts "
                                 f"({type(last_fault).__name__ if last_fault else 'unknown'}).")

    def _remember(self, load_id: str, result: BookResult) -> BookResult:
        with self._lock:
            self._bookings[load_id] = result
        return result
