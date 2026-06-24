"""TMS parser tests — framing, faults, and normalization (including dropping max_buy)."""
import pytest

from tms_parser import (
    parse_response, normalize_load, parse_location, normalize_equipment,
    TMSError, TMSPartial, TMSMalformed,
)

OK_QUERY = (
    "LOAD_ID:LD00324|ORIG_CITY:Atlanta                       |ORIG_STATE:GA|"
    "ORIG_ZIP:30303|DEST_CITY:Dallas                        |DEST_STATE:TX|"
    "DEST_ZIP:75201|PICKUP_DT:20260512080000|EQTYPE:DRY_VAN   |RATE:0002150|"
    "MILES:000785|STATUS:OPEN\r\nEND\r\n"
)
OK_GET = (
    "LOAD_ID:LD00324|ORIG_CITY:Atlanta                       |ORIG_STATE:GA|"
    "ORIG_ZIP:30303|DEST_CITY:Dallas                        |DEST_STATE:TX|"
    "DEST_ZIP:75201|PICKUP_DT:20260512080000|DELIVERY_DT:20260513170000|"
    "EQTYPE:DRY_VAN   |RATE:0002150|WEIGHT:0042000|"
    "COMMODITY:PALLETIZED CONSUMER GOODS       |PIECES:000026|MILES:000785|"
    "DIMS:48X40 STD GMA PALLETS              |NOTES:                         |"
    "STATUS:OPEN    |MAX_BUY:0001950\r\nEND\r\n"
)


def test_parse_ok_and_strip_padding():
    recs = parse_response(OK_QUERY)
    assert len(recs) == 1
    assert recs[0]["ORIG_CITY"] == "Atlanta"          # fixed-width padding stripped
    assert recs[0]["RATE"] == "0002150"


def test_parse_error_raises_tmserror():
    with pytest.raises(TMSError) as ei:
        parse_response("ERR|CODE:UNKNOWN_LOAD|MSG:load not found\r\n")
    assert ei.value.code == "UNKNOWN_LOAD"


def test_parse_partial_without_end():
    with pytest.raises(TMSPartial):
        parse_response("LOAD_ID:LD00324|ORIG_CITY:Atlanta\r\n")     # no END


def test_parse_malformed_line():
    with pytest.raises(TMSMalformed):
        parse_response("LOAD_ID:LD1|X:1\r\nGARBAGE_NO_PAIRS\r\nEND\r\n")


def test_parse_empty_result_is_not_error():
    assert parse_response("END\r\n") == []


def test_normalize_omits_max_buy_by_default():
    rec = parse_response(OK_GET)[0]
    public = normalize_load(rec)                       # default: agent-facing
    assert "max_buy" not in public
    internal = normalize_load(rec, include_max_buy=True)
    assert internal["max_buy"] == 1950                 # internal use only


def test_normalize_types_and_blank_notes():
    rec = parse_response(OK_GET)[0]
    nl = normalize_load(rec, include_max_buy=True)
    assert nl["posted_rate"] == 2150 and nl["miles"] == 785      # zero-padded -> int
    assert nl["weight"] == 42000 and nl["pieces"] == 26
    assert nl["notes"] is None                                   # blank NOTES -> None
    assert nl["load_id"] == "LD00324"


def test_parse_location():
    assert parse_location("Atlanta, GA") == {"city": "Atlanta", "state": "GA"}
    assert parse_location("Georgia") == {"state": "GA"}           # full name
    assert parse_location("GA") == {"state": "GA"}                # code
    assert parse_location("Atlanta") == {"city": "Atlanta"}       # city only
    assert parse_location("Miami Gardens, FL") == {"city": "Miami Gardens", "state": "FL"}
    assert parse_location("") == {}


def test_normalize_equipment():
    assert normalize_equipment("Dry Van") == "DRY_VAN"
    assert normalize_equipment("reefer") == "REEFER"
    assert normalize_equipment("Flatbed") == "FLATBED"
    assert normalize_equipment("Step Deck") == "STEP_DECK"
    assert normalize_equipment("POWER_ONLY") == "POWER_ONLY"     # already a code
    assert normalize_equipment("") is None
