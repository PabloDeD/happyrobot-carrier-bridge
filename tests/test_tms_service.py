"""
TMS service-layer tests — booking idempotency and fault handling.

A fake transport (`send_request`) is injected with a scripted sequence of behaviors.
"""
import pytest

import tms_service
from tms_client import TMSTimeout
from tms_parser import TMSError
from tms_service import TMSService

QUERY_OK = ("LOAD_ID:LD00324|ORIG_CITY:Atlanta|ORIG_STATE:GA|DEST_CITY:Portland|"
            "DEST_STATE:ME|EQTYPE:REEFER|RATE:0003280|MILES:001022|STATUS:OPEN\r\nEND\r\n")
ERR_SPURIOUS = "ERR|CODE:MISSING_FIELD|MSG:Invalid EQTYPE\r\n"
ERR_UNKNOWN = "ERR|CODE:UNKNOWN_LOAD|MSG:load not found\r\n"

BOOKED_RAW = ("LOAD_ID:LD00324|BOOKING_REF:BR00000000091277|STATUS:BOOKED  |"
              "TIMESTAMP:20260504193122\r\nEND\r\n")
ERR_ALREADY = "ERR|CODE:ALREADY_BOOKED|MSG:load not available\r\n"
ERR_INVALID = "ERR|CODE:INVALID_RATE|MSG:rate rejected\r\n"


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(tms_service.time, "sleep", lambda *_a, **_k: None)


def _wire(monkeypatch, behaviors):
    """behaviors: list of str (raw to return) or Exception (to raise). The last one repeats."""
    calls = {"n": 0}
    seq = list(behaviors)
    last = behaviors[-1] if behaviors else ""

    def fake(*_args, **_kwargs):
        calls["n"] += 1
        b = seq.pop(0) if seq else last
        if isinstance(b, Exception):
            raise b
        return b

    monkeypatch.setattr(tms_service, "send_request", fake)
    return calls


def _svc():
    return TMSService("h", 1, "tok", timeout=1.0, retries=3)


def test_book_success_returns_ref(monkeypatch):
    _wire(monkeypatch, [BOOKED_RAW])
    r = _svc().book_load("LD00324", "872144", 2200)
    assert r.status == "booked" and r.booking_ref == "BR00000000091277"


def test_book_is_idempotent_in_process(monkeypatch):
    calls = _wire(monkeypatch, [BOOKED_RAW])
    svc = _svc()
    r1 = svc.book_load("LD00324", "872144", 2200)
    r2 = svc.book_load("LD00324", "872144", 2200)        # same load -> cached
    assert r1.booking_ref == r2.booking_ref
    assert calls["n"] == 1                                # TMS not hit again


def test_book_already_booked_preexisting(monkeypatch):
    _wire(monkeypatch, [ERR_ALREADY])
    r = _svc().book_load("LD00324", "872144", 2200)
    assert r.status == "already_booked"


def test_fault_then_already_booked_is_treated_as_booked(monkeypatch):
    # Timeout on the 1st attempt (the BOOK may have landed), ALREADY_BOOKED on the 2nd.
    _wire(monkeypatch, [TMSTimeout("no reply"), ERR_ALREADY])
    r = _svc().book_load("LD00324", "872144", 2200)
    assert r.status == "booked"          # our earlier BOOK did go through
    assert r.booking_ref is None         # but we lost the ref (truncated confirmation)
    assert "truncada" in (r.reason or "")


def test_invalid_rate_is_rejected(monkeypatch):
    _wire(monkeypatch, [ERR_INVALID])
    r = _svc().book_load("LD00324", "872144", 0)
    assert r.status == "rejected" and "INVALID_RATE" in (r.reason or "")


def test_persistent_faults_exhaust_retries(monkeypatch):
    calls = _wire(monkeypatch, [TMSTimeout("x")])
    r = _svc().book_load("LD00324", "872144", 2200)
    assert r.status == "rejected" and calls["n"] == 3     # == retries


def test_read_retries_spurious_err_then_succeeds(monkeypatch):
    # TMS returns a spurious ERR ('Invalid EQTYPE'); the retry comes back clean.
    calls = _wire(monkeypatch, [ERR_SPURIOUS, QUERY_OK])
    loads = _svc().query_loads({"ORIG_STATE": "GA", "EQTYPE": "REEFER"})
    assert len(loads) == 1 and loads[0]["load_id"] == "LD00324"
    assert calls["n"] == 2                                 # retried once


def test_read_does_not_retry_unknown_load(monkeypatch):
    calls = _wire(monkeypatch, [ERR_UNKNOWN])
    with pytest.raises(TMSError):
        _svc().get_load("LDZZZ")
    assert calls["n"] == 1                                 # deterministic -> no retry
