"""Model tests — cleaning monetary amounts that arrive by voice."""
from models import BookRequest, NegotiateRequest


def test_carrier_offer_cleaning():
    assert NegotiateRequest(call_id="c", load_id="L", carrier_offer="$2,500").carrier_offer == 2500.0
    assert NegotiateRequest(call_id="c", load_id="L", carrier_offer="2500").carrier_offer == 2500.0
    assert NegotiateRequest(call_id="c", load_id="L", carrier_offer=2500).carrier_offer == 2500.0
    assert NegotiateRequest(call_id="c", load_id="L", carrier_offer="").carrier_offer is None
    assert NegotiateRequest(call_id="c", load_id="L").carrier_offer is None


def test_agreed_rate_cleaning():
    assert BookRequest(call_id="c", load_id="L", mc_number="1", agreed_rate="3,900").agreed_rate == 3900.0
    assert BookRequest(call_id="c", load_id="L", mc_number="1", agreed_rate="$3900").agreed_rate == 3900.0
