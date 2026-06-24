"""
Deterministic, server-side negotiation engine with a hard ceiling.

Why this lives in the bridge and not in the prompt or platform: HappyRobot has no
reliable mutable state within a call (`@` variables are read-only, the Python Sandbox
is stateless). A trustworthy round counter and a deterministic anti-ratchet both need
server-side state keyed by `call_id`. And the ceiling (`max_buy`) must never reach the
LLM. Either reason alone forces the decision into this engine.

Invariants:
  1. `max_buy` never appears in the response and never enters the offer formula.
  2. The broker's voluntary offer depends only on `posted`, so it can't be inverted to
     recover the ceiling. (Concessions that scaled toward the ceiling would be
     algebraically reversible — hence the curve is independent of it.)
  3. Concessions decrease (+75 / +50 / +25), so the carrier isn't trained to squeeze.
  4. We never offer or pay >= ceiling (silently clamped just below).
  5. Anti-ratchet: once a deal is struck we don't raise it; if they ask for less, take
     the lower number.

Stance — a deliberate call here — is coverage: on the last round, if the carrier asks
<= ceiling we close, since covering the load beats squeezing the last dollar. It's a
dial (`neg_alpha`, `neg_max_rounds`) that Phase 2 would calibrate against data.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class NegotiationParams:
    alpha: float = 0.08                 # concession amplitude relative to posted
    max_rounds: int = 3                 # brief: up to 3 counteroffers
    step: int = 25                      # rounding ($) so the offer sounds natural
    # Fraction of `alpha` released each round → decreasing steps (+75/+50/+25).
    fracs: tuple[float, ...] = (0.45, 0.72, 0.88)

    def frac_for(self, round_number: int) -> float:
        idx = max(1, min(round_number, len(self.fracs))) - 1
        return self.fracs[idx]


def _nice(x: float, step: int) -> int:
    """Round to the nearest multiple of `step`."""
    return int(round(x / step) * step)


@dataclass
class Decision:
    decision: str                       # accept | counter | reject | hold
    round: int
    rounds_left: int
    final: bool = False
    agreed_rate: Optional[float] = None
    counter_offer: Optional[float] = None


def decide(
    *,
    round_number: int,
    carrier_offer: Optional[float],
    posted: float,
    max_buy: float,
    agreed_rate: Optional[float],
    params: NegotiationParams,
) -> Decision:
    """
    Decide one round. Pure function — never returns `max_buy`.

    `round_number`  round this offer represents (1..); the engine derives it from state.
    `carrier_offer` what the carrier asks; None = opening (anchor to posted).
    `agreed_rate`   deal already closed this call (arms the anti-ratchet).
    """
    ceiling = float(max_buy)
    floor = min(float(posted), ceiling)            # defensive: posted never above the ceiling
    max_rounds = params.max_rounds

    # Anti-ratchet: a deal is already closed this call.
    if agreed_rate is not None:
        if carrier_offer is not None and carrier_offer <= agreed_rate:
            # they ask for less → in our favor, take it
            return Decision("accept", round_number, 0, final=True, agreed_rate=float(carrier_offer))
        # same or more → hold, don't raise
        return Decision("hold", round_number, 0, final=True, agreed_rate=float(agreed_rate))

    # Opening: no offer yet → anchor at posted.
    if carrier_offer is None:
        return Decision("counter", 0, max_rounds, final=False, counter_offer=_nice(floor, params.step))

    # Most the broker would put up voluntarily this round. Depends only on posted, not
    # the ceiling, so it can't be inverted to recover `max_buy`.
    frac = params.frac_for(round_number)
    my_offer = _nice(floor + params.alpha * floor * frac, params.step)
    if my_offer >= ceiling:
        my_offer = _nice(ceiling - params.step, params.step)   # stay invisibly below the ceiling

    last_round = round_number >= max_rounds
    rounds_left = 0 if last_round else max_rounds - round_number

    # They ask <= what we'd offer → accept at their number (cheaper for the broker).
    if carrier_offer <= my_offer:
        return Decision("accept", round_number, 0, final=True, agreed_rate=float(carrier_offer))

    # Middle rounds: hold the line with a decreasing counter.
    if not last_round:
        return Decision("counter", round_number, rounds_left, final=False, counter_offer=my_offer)

    # Last round (or past it).
    if carrier_offer <= ceiling:
        # Within the ceiling → cover the load (coverage stance).
        return Decision("accept", round_number, 0, final=True, agreed_rate=float(carrier_offer))

    # Still above the ceiling and out of rounds → no deal.
    if round_number > max_rounds:
        return Decision("reject", round_number, 0, final=True)

    # Exactly the last round and above the ceiling → final offer; no deal if they don't drop.
    return Decision("counter", round_number, 0, final=True, counter_offer=my_offer)


@dataclass
class NegotiationState:
    posted: float
    max_buy: float
    round: int = 0
    agreed_rate: Optional[float] = None
    history: list = field(default_factory=list)   # audit trail: [(round, carrier_offer, decision)]
    touched_at: float = field(default_factory=time.monotonic)   # for the TTL (anti-stale-state)


class NegotiationStore:
    """
    Negotiation state keyed by (call_id, load_id). In-memory, lock-guarded.

    Anti-stale-state TTL: an entry older than `ttl_seconds` is treated as absent. In
    production every web call carries a unique `run_id` (state is always fresh), but if
    an environment reuses `call_id` (e.g. re-running a test suite), without the TTL the
    anti-ratchet would inherit a stale `agreed_rate` and produce nonsense counters. The
    TTL (plus the reset on booking) avoids that. Real prod: Redis with EXPIRE, same interface.
    """
    def __init__(self, ttl_seconds: float = 1800.0) -> None:
        self._states: dict[tuple[str, str], NegotiationState] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds

    def get(self, call_id: str, load_id: str) -> Optional[NegotiationState]:
        with self._lock:
            key = (call_id, load_id)
            st = self._states.get(key)
            if st is None:
                return None
            if self._ttl and (time.monotonic() - st.touched_at) > self._ttl:
                self._states.pop(key, None)        # stale → treat as absent (fresh state)
                return None
            return st

    def put(self, call_id: str, load_id: str, state: NegotiationState) -> None:
        with self._lock:
            state.touched_at = time.monotonic()    # refresh on every write
            self._states[(call_id, load_id)] = state

    def reset(self, call_id: str, load_id: str) -> None:
        with self._lock:
            self._states.pop((call_id, load_id), None)
