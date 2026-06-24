"""
Parser for legacy TMS responses.

- OK:  N record lines (K:V|K:V|...) followed by an 'END' line.
- ERR: 'ERR|CODE:<code>|MSG:<msg>'.

The wire is fixed-width, so values come right-padded with spaces — we rstrip them.
A response with no END is partial (TMSPartial); a record line with no valid K:V pair
is malformed (TMSMalformed). Type coercion (zero-padded numbers, dates, LOAD_ID) lives
in normalize_load().
"""
from datetime import datetime, timezone


class TMSError(Exception):
    """A business error the TMS returned (ERR|CODE|MSG)."""
    def __init__(self, code: str, msg: str):
        self.code = code
        self.msg = msg
        super().__init__(f"{code}: {msg}")


class TMSPartial(Exception):
    """Truncated response: no END terminator (the partial-response fault)."""


class TMSMalformed(Exception):
    """Response that breaks the framing (the malformed-response fault)."""


def _split_fields(line: str) -> dict:
    out = {}
    for tok in line.split("|"):
        if ":" not in tok:
            continue
        k, v = tok.split(":", 1)
        out[k] = v.rstrip()  # strip the right padding (leaves "" if it was all spaces)
    return out


def parse_response(raw: str) -> list[dict]:
    """Records as dicts with padding stripped. Raises TMSError / TMSPartial / TMSMalformed."""
    if not raw:
        raise TMSPartial("respuesta vacía")

    lines = [ln for ln in raw.split("\r\n") if ln != ""]
    if not lines:
        raise TMSPartial("respuesta sin líneas")

    # Single-line error
    if lines[0].startswith("ERR"):
        f = _split_fields(lines[0])
        raise TMSError(f.get("CODE", "UNKNOWN"), f.get("MSG", ""))

    # Success must carry an END
    if "END" not in lines:
        raise TMSPartial("sin terminador END (respuesta truncada)")

    records = []
    for ln in lines:
        if ln == "END":
            break
        fields = _split_fields(ln)
        if not fields:
            raise TMSMalformed(f"línea de record sin pares K:V válidos: {ln!r}")
        records.append(fields)
    return records


# Coerce raw fields to clean types for the REST layer

def _num(padded: str):
    """'0002150' -> 2150 ; '' -> None."""
    s = (padded or "").strip()
    return int(s) if s.isdigit() else (s or None)


def _dt(padded: str):
    """'20260512080000' -> ISO 8601 UTC. Returns the raw string if it doesn't parse."""
    s = (padded or "").strip()
    try:
        return datetime.strptime(s, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return s or None


def load_id_to_tms(short: str) -> str:
    """'LD45821' -> 'LD0000045821' (TMS format: 'LD' + 10 digits)."""
    digits = "".join(c for c in (short or "") if c.isdigit())
    return "LD" + digits.zfill(10)


def load_id_to_short(tms: str) -> str:
    """'LD0000045821' -> 'LD45821' (drop the leading zeros)."""
    digits = "".join(c for c in (tms or "") if c.isdigit())
    return "LD" + str(int(digits)) if digits else (tms or "")


def normalize_load(rec: dict, include_max_buy: bool = False) -> dict:
    """
    Raw TMS record to a clean dict for the API. Omits max_buy by default (that's what the
    agent sees); the negotiation engine asks for it explicitly.
    """
    out = {
        # Keep the raw TMS id verbatim — padding varies by build (LD00315, LD0000045821…).
        # We reuse it as-is in LOAD_GET/LOAD_BOOK rather than rebuilding it.
        "load_id": rec.get("LOAD_ID", "").strip(),
        "origin": {"city": rec.get("ORIG_CITY"), "state": rec.get("ORIG_STATE"), "zip": rec.get("ORIG_ZIP")},
        "destination": {"city": rec.get("DEST_CITY"), "state": rec.get("DEST_STATE"), "zip": rec.get("DEST_ZIP")},
        "equipment": rec.get("EQTYPE"),
        "posted_rate": _num(rec.get("RATE")),
        "miles": _num(rec.get("MILES")),
        "status": rec.get("STATUS"),
        "pickup_dt": _dt(rec.get("PICKUP_DT")),
    }
    # Extra fields only LOAD_GET returns
    if "DELIVERY_DT" in rec:
        out["delivery_dt"] = _dt(rec.get("DELIVERY_DT"))
    for k_src, k_dst in (("WEIGHT", "weight"), ("PIECES", "pieces")):
        if k_src in rec:
            out[k_dst] = _num(rec.get(k_src))
    for k_src, k_dst in (("COMMODITY", "commodity"), ("DIMS", "dims"), ("NOTES", "notes")):
        if k_src in rec:
            out[k_dst] = rec.get(k_src) or None
    if include_max_buy and "MAX_BUY" in rec:
        out["max_buy"] = _num(rec.get("MAX_BUY"))  # internal only (engine), never to the agent
    return out


# Normalize search input — what the carrier actually says.
# The agent sends natural language ("Atlanta, GA", "Dry Van"); the TMS wants city and
# state split out and the equipment code uppercased with an underscore.

_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}
_STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD",
    "tennessee": "TN", "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
    "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}
_EQUIP_SYNONYMS = {
    "DRY VAN": "DRY_VAN", "DRYVAN": "DRY_VAN", "VAN": "DRY_VAN",
    "REEFER": "REEFER", "REFRIGERATED": "REEFER", "REFER": "REEFER",
    "FLATBED": "FLATBED", "FLAT BED": "FLATBED", "FLAT": "FLATBED",
    "STEP DECK": "STEP_DECK", "STEPDECK": "STEP_DECK", "STEP": "STEP_DECK",
    "POWER ONLY": "POWER_ONLY", "POWERONLY": "POWER_ONLY",
}


def _to_state_code(s: str):
    s = (s or "").strip()
    if len(s) == 2 and s.upper() in _STATE_CODES:
        return s.upper()
    return _STATE_NAMES.get(s.lower())


def parse_location(text: str) -> dict:
    """
    'Atlanta, GA' -> {city:'Atlanta', state:'GA'} · 'GA'/'Georgia' -> {state:'GA'}
    · 'Atlanta' -> {city:'Atlanta'} · '' -> {}. A bare token is treated as a state if it
    matches a known code/name, otherwise as a city.
    """
    s = (text or "").strip()
    if not s:
        return {}
    if "," in s:
        city, st = s.rsplit(",", 1)
        out: dict = {}
        if city.strip():
            out["city"] = city.strip()
        code = _to_state_code(st)
        if code:
            out["state"] = code
        return out
    code = _to_state_code(s)
    return {"state": code} if code else {"city": s}


def normalize_equipment(text: str):
    """'Dry Van' -> 'DRY_VAN' · 'reefer' -> 'REEFER' · '' -> None. Unknown values fall through uppercased+underscored."""
    s = (text or "").strip()
    if not s:
        return None
    key = " ".join(s.upper().replace("_", " ").replace("-", " ").split())
    return _EQUIP_SYNONYMS.get(key, key.replace(" ", "_"))
