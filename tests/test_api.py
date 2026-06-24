"""
API tests (FastAPI TestClient) with fake TMS/FMCSA.

Covers auth, each route's contract, the "max_buy never leaks" invariant, the
negotiation state machine across calls, and the ceiling guard on /book.
"""
import pytest
from fastapi.testclient import TestClient

from fmcsa import CarrierVerdict
from tms_service import BookResult

AUTH = {"X-API-Key": "test-bridge-key"}


def _load(load_id="LD00324", max_buy=None):
    d = {
        "load_id": load_id,
        "origin": {"city": "Atlanta", "state": "GA", "zip": "30303"},
        "destination": {"city": "Dallas", "state": "TX", "zip": "75201"},
        "equipment": "DRY_VAN", "posted_rate": 2000, "miles": 785, "status": "OPEN",
        "pickup_dt": "2026-05-12T08:00:00+00:00", "delivery_dt": "2026-05-13T17:00:00+00:00",
        "weight": 42000, "pieces": 26, "commodity": "GOODS", "dims": "48X40", "notes": None,
    }
    if max_buy is not None:
        d["max_buy"] = max_buy
    return d


class FakeTMS:
    def ping(self):
        return True, "AUTH:OK"

    def query_loads(self, filters):
        return [_load()]                                  # no max_buy

    def get_load(self, load_id, include_max_buy=False):
        if load_id == "LD_MISSING":
            return None
        return _load(load_id, max_buy=2400 if include_max_buy else None)

    def book_load(self, load_id, mc_number, agreed_rate):
        return BookResult("booked", load_id, agreed_rate,
                          booking_ref="BR00000000091277", timestamp="2026-05-04T19:31:22+00:00")


class FakeFMCSA:
    def verify(self, mc_number):
        return CarrierVerdict(True, "872144", "ACTIVE", carrier_name="TEST CARRIER LLC",
                              dot_number="1234567")


@pytest.fixture
def client():
    import main
    with TestClient(main.app) as c:
        c.app.state.tms = FakeTMS()
        c.app.state.fmcsa = FakeFMCSA()
        yield c


# meta / auth
def test_health_no_auth_required(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["tms"] == "up"


def test_protected_route_requires_api_key(client):
    r = client.post("/loads/search", json={"origin_state": "GA"})
    assert r.status_code == 401


def test_bad_api_key_rejected(client):
    r = client.post("/loads/search", json={"origin_state": "GA"},
                    headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


# loads
def test_search_requires_a_filter(client):
    r = client.post("/loads/search", json={"max_results": 5}, headers=AUTH)
    assert r.status_code == 400


def test_search_returns_loads_without_max_buy(client):
    r = client.post("/loads/search", json={"origin_state": "GA"}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert "max_buy" not in r.text.lower() and "max_rate" not in r.text.lower()


def test_get_load_detail_hides_ceiling(client):
    r = client.get("/loads/LD00324", headers=AUTH)
    assert r.status_code == 200
    assert "max_buy" not in r.text.lower() and "max_rate" not in r.text.lower()


def test_get_unknown_load_404(client):
    r = client.get("/loads/LD_MISSING", headers=AUTH)
    assert r.status_code == 404


def test_search_lane_match_exact(client):
    r = client.post("/loads/search",
                    json={"origin": "Atlanta, GA", "destination": "Dallas, TX", "equipment_type": "Dry Van"},
                    headers=AUTH)
    assert r.status_code == 200 and r.json()["lane_match"] == "exact"


def test_search_relaxes_destination_when_lane_empty():
    # Broker-style fallback: empty exact lane -> relax destination -> loads leaving the origin.
    class SparseTMS(FakeTMS):
        def query_loads(self, filters):
            return [] if (filters.get("DEST_CITY") or filters.get("DEST_STATE")) else [_load("LD00248")]
    import main
    with TestClient(main.app) as c:
        c.app.state.tms = SparseTMS(); c.app.state.fmcsa = FakeFMCSA()
        body = c.post("/loads/search",
                      json={"origin": "Atlanta, GA", "destination": "Dallas, TX", "equipment_type": "Reefer"},
                      headers=AUTH).json()
        assert body["count"] == 1 and body["lane_match"] == "origin" and body["note"]


def test_search_lane_match_none_when_truly_empty():
    class EmptyTMS(FakeTMS):
        def query_loads(self, filters):
            return []
    import main
    with TestClient(main.app) as c:
        c.app.state.tms = EmptyTMS(); c.app.state.fmcsa = FakeFMCSA()
        body = c.post("/loads/search",
                      json={"origin": "Nowhere, ZZ", "destination": "Void, ZZ", "equipment_type": "Reefer"},
                      headers=AUTH).json()
        assert body["count"] == 0 and body["lane_match"] == "none"


# negotiate
def test_negotiation_state_advances_across_calls(client):
    body = {"call_id": "c1", "load_id": "LD00324", "carrier_offer": 2500}
    r1 = client.post("/negotiate", json=body, headers=AUTH).json()
    r2 = client.post("/negotiate", json=body, headers=AUTH).json()
    assert r1["decision"] == "counter" and r2["decision"] == "counter"
    assert r1["counter_offer"] < r2["counter_offer"]      # round advanced (server-side state)
    assert r1["round"] == 1 and r2["round"] == 2


def test_negotiate_never_leaks_ceiling(client):
    r = client.post("/negotiate", json={"call_id": "c2", "load_id": "LD00324",
                                        "carrier_offer": 2500}, headers=AUTH)
    assert "max_buy" not in r.text.lower() and "2400" not in r.text


def test_negotiate_accepts_low_offer(client):
    r = client.post("/negotiate", json={"call_id": "c3", "load_id": "LD00324",
                                        "carrier_offer": 2050}, headers=AUTH).json()
    assert r["decision"] == "accept" and r["agreed_rate"] == 2050


# book
def test_book_blocks_rate_above_ceiling(client):
    # Prior negotiation sets the state (ceiling 2400)...
    client.post("/negotiate", json={"call_id": "c4", "load_id": "LD00324",
                                    "carrier_offer": 2500}, headers=AUTH)
    # ...then a booking attempt above the ceiling is blocked (defense in depth).
    r = client.post("/book", json={"call_id": "c4", "load_id": "LD00324",
                                   "mc_number": "872144", "agreed_rate": 9999}, headers=AUTH)
    assert r.status_code == 422


def test_book_happy_path(client):
    client.post("/negotiate", json={"call_id": "c5", "load_id": "LD00324",
                                    "carrier_offer": 2050}, headers=AUTH)         # accepts 2050
    r = client.post("/book", json={"call_id": "c5", "load_id": "LD00324",
                                   "mc_number": "872144", "agreed_rate": 2050}, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "booked" and body["booking_ref"] == "BR00000000091277"


def test_verify_carrier(client):
    r = client.post("/verify-carrier", json={"mc_number": "MC 872144"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["eligible"] is True
