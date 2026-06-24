"""OTP tests: store (TTL / attempts / test fixture) + the /otp/request and /otp/verify endpoints."""
import pytest
from fastapi.testclient import TestClient

from otp import OTPStore, generate_code, mask_phone

AUTH = {"X-API-Key": "test-bridge-key"}


# OTPStore (pure)
def test_store_issue_and_verify_single_use():
    s = OTPStore()
    assert s.verify("c1", "123456") == (False, 0)        # never issued -> nothing
    s.issue("c1", "123456")
    assert s.verify("c1", "123456") == (True, 3)
    assert s.verify("c1", "123456")[0] is False          # single use


def test_store_wrong_code_decrements_then_locks():
    s = OTPStore(max_attempts=2)
    s.issue("c1", "123456")
    assert s.verify("c1", "000000") == (False, 1)
    assert s.verify("c1", "000000") == (False, 0)         # exhausted
    assert s.verify("c1", "123456")[0] is False           # invalidated once attempts run out


def test_store_fixed_code_accepted_even_with_spaces():
    s = OTPStore()
    s.issue("c1", "123456")
    assert s.verify("c1", "4 8 2 9 1 5", fixed_code="482915")[0] is True   # stripped to digits


def test_store_expired_code_rejected():
    s = OTPStore(ttl_seconds=-1)
    s.issue("c1", "123456")
    assert s.verify("c1", "123456")[0] is False


def test_generate_code_six_digits():
    for _ in range(50):
        c = generate_code()
        assert len(c) == 6 and c.isdigit()


def test_mask_phone():
    assert mask_phone("+1 (555) 123-4567") == "4567"
    assert mask_phone("12") == ""


# endpoints
@pytest.fixture
def client():
    import main
    with TestClient(main.app) as c:
        yield c


def test_otp_request_returns_code_and_verify_succeeds(client):
    r = client.post("/otp/request", json={"call_id": "call-1"}, headers=AUTH)
    assert r.status_code == 200
    code = r.json()["code"]
    assert len(code) == 6 and code.isdigit()
    v = client.post("/otp/verify", json={"call_id": "call-1", "otp_code": code}, headers=AUTH)
    assert v.status_code == 200 and v.json()["otp_verified"] is True


def test_otp_fixed_test_code_always_accepted(client):
    client.post("/otp/request", json={"call_id": "call-2"}, headers=AUTH)
    v = client.post("/otp/verify", json={"call_id": "call-2", "otp_code": "482915"}, headers=AUTH)
    assert v.json()["otp_verified"] is True


def test_otp_wrong_code_rejected(client):
    client.post("/otp/request", json={"call_id": "call-3"}, headers=AUTH)
    v = client.post("/otp/verify", json={"call_id": "call-3", "otp_code": "111111"}, headers=AUTH)
    body = v.json()
    assert body["otp_verified"] is False and body["attempts_left"] == 2


def test_otp_requires_api_key(client):
    assert client.post("/otp/request", json={"call_id": "x"}).status_code == 401
