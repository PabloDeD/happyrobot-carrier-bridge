"""
Proxy to the FMCSA QCMobile API — verifies carrier authority by MC number.

The agent only needs a yes/no plus the legal name. This module handles MC
normalization (STT splits the number: "8 72144" -> "872144"), the docket-number
REST lookup, and mapping FMCSA's raw schema to a clean verdict.

Fail-closed: anything that isn't a clear ACTIVE result — network error, timeout,
non-200, 404, inactive authority — returns not-eligible. We never crash.

The `webKey` rides in the query string, so the full URL is never logged.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

import httpx

log = logging.getLogger("bridge.fmcsa")


@dataclass
class CarrierVerdict:
    eligible: bool
    mc_number: str
    authority_status: str                  # ACTIVE | INACTIVE | NOT_FOUND | UNKNOWN
    carrier_name: Optional[str] = None
    dot_number: Optional[str] = None
    reason: Optional[str] = None


def normalize_mc(raw: str) -> str:
    """Digits only: 'MC 872144' / '8 72144' -> '872144'. STT often splits the number."""
    return re.sub(r"\D", "", raw or "")


# MCs that mock mode treats as non-active authority, to exercise the "fails -> end call" branch.
_MOCK_INACTIVE = {"000000", "111111", "000001"}


class FMCSAClient:
    def __init__(self, api_key: str, base_url: str, timeout: float = 10.0, mode: str = "live") -> None:
        self._key = api_key
        self._base = base_url.rstrip("/")
        self._timeout = timeout
        self._mode = (mode or "live").lower()

    def verify(self, mc_number: str) -> CarrierVerdict:
        mc = normalize_mc(mc_number)
        if not mc:
            return CarrierVerdict(False, mc_number, "UNKNOWN", reason="MC number empty or unreadable.")

        # Mock mode (local dev from outside the US): deterministic verdict, no network.
        if self._mode == "mock":
            return self._mock_verify(mc)

        if not self._key:
            return CarrierVerdict(False, mc, "UNKNOWN", reason="FMCSA_API_KEY not configured.")

        # QCMobile docket-number lookup (docket = MC number).
        url = f"{self._base}/carriers/docket-number/{mc}"
        headers = {"User-Agent": "happyrobot-bridge/1.0", "Accept": "application/json"}
        try:
            resp = httpx.get(url, params={"webKey": self._key}, headers=headers, timeout=self._timeout)
        except httpx.HTTPError as exc:
            log.warning("FMCSA unreachable: %s", type(exc).__name__)
            return CarrierVerdict(False, mc, "UNKNOWN", reason="FMCSA unavailable (network/timeout).")

        if resp.status_code == 404:
            return CarrierVerdict(False, mc, "NOT_FOUND", reason="MC not found in FMCSA.")
        if resp.status_code != 200:
            log.warning("FMCSA HTTP %s", resp.status_code)
            return CarrierVerdict(False, mc, "UNKNOWN", reason=f"FMCSA returned HTTP {resp.status_code}.")

        try:
            payload = resp.json()
        except ValueError:
            return CarrierVerdict(False, mc, "UNKNOWN", reason="FMCSA returned a non-JSON body.")

        return self._interpret(payload, mc)

    @staticmethod
    def _interpret(payload: dict, mc: str) -> CarrierVerdict:
        """
        Map the QCMobile schema to a verdict. The docket lookup returns `content` as a
        list of {carrier: {...}}; the DOT lookup returns an object. Handle both shapes.
        """
        content = payload.get("content")
        carrier = None
        if isinstance(content, list) and content:
            carrier = content[0].get("carrier") if isinstance(content[0], dict) else None
        elif isinstance(content, dict):
            carrier = content.get("carrier", content)

        if not carrier:
            return CarrierVerdict(False, mc, "NOT_FOUND", reason="No carrier record for that MC.")

        allowed = str(carrier.get("allowedToOperate", "")).upper()   # 'Y' / 'N'
        status_code = str(carrier.get("statusCode", "")).upper()     # 'A' / 'I'
        name = carrier.get("legalName") or carrier.get("dbaName")
        dot = carrier.get("dotNumber")
        dot = str(dot) if dot is not None else None

        eligible = allowed == "Y" and status_code in ("A", "")
        if eligible:
            status = "ACTIVE"
            reason = None
        elif status_code == "I" or allowed == "N":
            status = "INACTIVE"
            reason = "Authority inactive or not allowed to operate."
        else:
            status = "UNKNOWN"
            reason = "Authority status indeterminate."

        return CarrierVerdict(eligible, mc, status, carrier_name=name, dot_number=dot, reason=reason)

    @staticmethod
    def _mock_verify(mc: str) -> CarrierVerdict:
        """Deterministic verdict for local dev. Not a substitute for the real check in production."""
        if mc in _MOCK_INACTIVE:
            return CarrierVerdict(False, mc, "INACTIVE", carrier_name="TEST CARRIER (REVOKED)",
                                  dot_number="0000000", reason="Mock: inactive authority.")
        return CarrierVerdict(True, mc, "ACTIVE", carrier_name=f"TEST CARRIER {mc} LLC",
                              dot_number="0000000",
                              reason="Mock mode (QCMobile geo-blocked outside the US).")
