"""
Server-side OTP: issues and verifies a one-time code per call_id.

The code never reaches the agent's LLM context — the platform's delivery node (email
or SMS) reads it from this service's response and sends it to the contact on file, so
the agent only ever sees the delivery node's status. Codes are single-use, expire on a
TTL, and lock after a few bad attempts.

The fixed test code exists because a simulated caller (the adversarial suite) can't
receive a real email/SMS, so it's accepted unconditionally to get past the gate — the
same way payment APIs ship test card numbers. Off in production.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass


@dataclass
class OTPRecord:
    code: str
    attempts_left: int
    expires_at: float


class OTPStore:
    """In-memory, thread-safe, keyed by call_id. Ephemeral codes with a TTL and an attempt cap."""

    def __init__(self, ttl_seconds: float = 600.0, max_attempts: int = 3) -> None:
        self._ttl = ttl_seconds
        self._max_attempts = max_attempts
        self._data: dict[str, OTPRecord] = {}
        self._lock = threading.Lock()

    def issue(self, call_id: str, code: str) -> None:
        with self._lock:
            self._data[call_id] = OTPRecord(
                code=code, attempts_left=self._max_attempts, expires_at=time.time() + self._ttl,
            )

    def verify(self, call_id: str, candidate: str, fixed_code: str | None = None) -> tuple[bool, int]:
        """Returns (verified, attempts_left). Accepts the issued code, or fixed_code if given."""
        candidate = _digits(candidate)
        with self._lock:
            rec = self._data.get(call_id)
            now = time.time()
            if rec is None or rec.expires_at < now:
                if rec is not None:
                    self._data.pop(call_id, None)
                # Test fixture passes even with no prior issue — a simulated caller never got one.
                if fixed_code and candidate == fixed_code:
                    return True, self._max_attempts
                return False, 0

            if candidate == rec.code or (fixed_code and candidate == fixed_code):
                self._data.pop(call_id, None)            # single use
                return True, rec.attempts_left
            rec.attempts_left -= 1
            if rec.attempts_left <= 0:
                self._data.pop(call_id, None)
                return False, 0
            return False, rec.attempts_left


def generate_code() -> str:
    """Six-digit numeric code, cryptographically random."""
    return f"{secrets.randbelow(1_000_000):06d}"


def mask_phone(number: str) -> str:
    """'+15551234567' -> '4567' — last four, to confirm without reading the whole number."""
    d = _digits(number)
    return d[-4:] if len(d) >= 4 else ""


def _digits(s: str) -> str:
    return "".join(c for c in (s or "") if c.isdigit())
