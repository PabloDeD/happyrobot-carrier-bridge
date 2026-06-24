"""
Smoke test against the real TMS. Checks connection, auth, framing, and parsing before building on top.

Usage:
    1) cp .env.example .env  and fill in TMS_TOKEN (and host/port if they differ)
    2) python smoke_test.py

Reads credentials from environment variables or a local `.env` file (gitignored).
Never prints the token.
"""
import os
import sys

from tms_client import send_request, TMSTimeout
from tms_parser import parse_response, normalize_load, load_id_to_short, TMSError, TMSPartial, TMSMalformed


def load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (no dependencies), if the file exists."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def run(host, port, token, cmd, fields, label):
    print(f"\n== {label} ==")
    try:
        raw = send_request(host, port, cmd, token, fields)
    except TMSTimeout as e:
        print("  TIMEOUT:", e)
        return None
    print("  raw:", repr(raw)[:400])
    try:
        records = parse_response(raw)
        print(f"  OK — {len(records)} record(s)")
        return records
    except TMSError as e:
        print(f"  TMS ERROR — {e.code}: {e.msg}")
    except TMSPartial as e:
        print("  PARTIAL:", e)
    except TMSMalformed as e:
        print("  MALFORMED:", e)
    return None


def main():
    load_dotenv()
    host = os.environ.get("TMS_HOST")
    port = os.environ.get("TMS_PORT")
    token = os.environ.get("TMS_TOKEN")
    if not (host and port and token):
        print("Missing TMS_HOST / TMS_PORT / TMS_TOKEN (set them in .env or the environment).")
        sys.exit(1)
    port = int(port)
    print(f"TMS {host}:{port} — token {'*' * 8} (hidden)")

    # DEBUG_ECHO checks auth + framing without going through faults. FIELDS_PARSED should be 3.
    run(host, port, token, "DEBUG_ECHO", {"MSG": "HELLO"}, "DEBUG_ECHO (auth + framing)")

    # Real loads, GA -> TX, dry van.
    recs = run(host, port, token, "LOAD_QUERY",
               {"ORIG_STATE": "GA", "DEST_STATE": "TX", "EQTYPE": "DRY_VAN", "MAX_RESULTS": "5"},
               "LOAD_QUERY GA->TX DRY_VAN")
    first_id = None
    if recs:
        for r in recs:
            nl = normalize_load(r)
            print("   ->", nl["load_id"], nl["origin"]["city"], "->", nl["destination"]["city"],
                  "| posted", nl["posted_rate"], "| miles", nl["miles"])
        first_id = load_id_to_short(recs[0].get("LOAD_ID", ""))

    # LOAD_GET on the first load: pulls MAX_BUY (the real ceiling, internal use only).
    if first_id:
        from tms_parser import load_id_to_tms
        recs = run(host, port, token, "LOAD_GET", {"LOAD_ID": load_id_to_tms(first_id)},
                   f"LOAD_GET {first_id}")
        if recs:
            nl = normalize_load(recs[0], include_max_buy=True)
            print("   detail:", nl.get("load_id"), "| posted", nl.get("posted_rate"),
                  "| MAX_BUY", nl.get("max_buy"), "| notes:", (nl.get("notes") or "")[:60])


if __name__ == "__main__":
    main()
