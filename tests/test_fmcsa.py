"""FMCSA tests — MC normalization, QCMobile schema mapping, and mock mode."""
from fmcsa import FMCSAClient, normalize_mc


def test_normalize_mc_strips_non_digits():
    assert normalize_mc("MC 872144") == "872144"
    assert normalize_mc("8 72144") == "872144"          # STT splits the number
    assert normalize_mc("") == ""


def test_interpret_active_carrier():
    payload = {"content": [{"carrier": {
        "allowedToOperate": "Y", "statusCode": "A",
        "legalName": "ACME TRUCKING LLC", "dotNumber": 1234567}}]}
    v = FMCSAClient._interpret(payload, "872144")
    assert v.eligible is True and v.authority_status == "ACTIVE"
    assert v.carrier_name == "ACME TRUCKING LLC" and v.dot_number == "1234567"


def test_interpret_inactive_carrier():
    payload = {"content": {"carrier": {"allowedToOperate": "N", "statusCode": "I",
                                       "legalName": "REVOKED CO"}}}
    v = FMCSAClient._interpret(payload, "872144")
    assert v.eligible is False and v.authority_status == "INACTIVE"


def test_interpret_no_carrier_is_not_found():
    v = FMCSAClient._interpret({"content": []}, "999999")
    assert v.eligible is False and v.authority_status == "NOT_FOUND"


def test_mock_mode_active_and_inactive():
    c = FMCSAClient(api_key="", base_url="x", mode="mock")
    assert c.verify("MC 999999").eligible is True            # anything -> active
    assert c.verify("000000").eligible is False              # denylist -> inactive (the "fails" branch)
