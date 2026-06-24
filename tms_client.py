"""
TCP client for the legacy TMS.

Wire protocol (from the handbook):
- TCP, ASCII, lines terminated by \\r\\n, frame capped at 4096 bytes.
- One request per connection: the server closes after replying, so connections aren't reused.
- Request:  CMD:<cmd>|AUTH:<token>|<K>:<V>|...\\r\\n   (CMD first, AUTH always second)
- OK reply:  <record lines>...  END\\r\\n
- ERR reply: ERR|CODE:<code>|MSG:<msg>\\r\\n

Operational commands get faults injected under load, unsignalled:
- Timeout: server never replies -> socket.timeout.
- Partial: a response prefix with no END -> the parser catches it.
- Malformed: broken framing -> caught by the parser/validation.
- Delayed termination: full response but the connection stays open -> we stop once we see END.

DEBUG_ECHO is the one command not subject to faults — it's there to check auth/framing.
"""
import socket

MAX_FRAME = 4096
SAFETY_CAP = 65536  # defensive read ceiling


class TMSTimeout(Exception):
    """Server didn't reply in time (likely an injected timeout fault)."""


def encode_request(cmd: str, auth: str, fields: dict | None = None) -> bytes:
    """Build the request line: CMD first, AUTH second, then the fields."""
    parts = [f"CMD:{cmd}", f"AUTH:{auth}"]
    for k, v in (fields or {}).items():
        if v is None:
            continue
        v = str(v)
        if "|" in v or "\r" in v or "\n" in v:
            raise ValueError(f"el valor de {k} no puede contener '|' ni saltos de línea")
        parts.append(f"{k}:{v}")
    line = "|".join(parts) + "\r\n"
    payload = line.encode("ascii")
    if len(payload) > MAX_FRAME:
        raise ValueError(f"request excede el frame de {MAX_FRAME} bytes")
    return payload


def _looks_complete(buf: bytes) -> bool:
    """Whether the response is already complete, so we don't sit waiting through a delayed termination."""
    text = buf.decode("ascii", errors="replace")
    if text.startswith("ERR") and "\r\n" in text:
        return True
    if "END\r\n" in text:
        return True
    return False


def send_request(host: str, port: int, cmd: str, auth: str,
                 fields: dict | None = None, timeout: float = 10.0) -> str:
    """
    Open a fresh connection, send the request, return the raw ASCII response.
    Raises TMSTimeout if nothing arrives within the timeout. Doesn't parse the
    response — that's tms_parser.parse_response().
    """
    payload = encode_request(cmd, auth, fields)
    buf = b""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(payload)
        try:
            while not _looks_complete(buf):
                data = sock.recv(MAX_FRAME)
                if not data:
                    break  # server closed the connection
                buf += data
                if len(buf) > SAFETY_CAP:
                    break
        except socket.timeout:
            if not buf:
                raise TMSTimeout("sin respuesta dentro del timeout (posible fault de timeout)")
            # Timed out with partial data: hand back what we have; the parser will flag it as partial.
    return buf.decode("ascii", errors="replace")
