"""
Negotiation engine tests — the brief's invariants, locked down.

Curve with posted=2000, alpha=0.08, fracs=(0.45,0.72,0.88), step=25, max_rounds=3:
  counter r1=2075, r2=2125, r3=2150  -> shrinking concessions (+75/+50/+25).
"""
from negotiation import NegotiationParams, NegotiationState, NegotiationStore, decide

P = NegotiationParams()
POSTED = 2000.0
CEIL = 2400.0


def _counter(round_number, offer, max_buy=CEIL, agreed=None):
    return decide(round_number=round_number, carrier_offer=offer, posted=POSTED,
                  max_buy=max_buy, agreed_rate=agreed, params=P)


def test_decreasing_concessions():
    # Carrier asks high every round -> broker climbs in shrinking steps.
    c1 = _counter(1, 2500).counter_offer
    c2 = _counter(2, 2500).counter_offer
    c3 = _counter(3, 2500).counter_offer
    assert (c1, c2, c3) == (2075, 2125, 2150)
    assert (c2 - c1) > (c3 - c2)                 # +50 > +25, concessions shrink


def test_offer_curve_is_independent_of_ceiling():
    # Same curve under different ceilings -> the ceiling never leaks through behavior.
    for ceil in (2300.0, 2400.0, 2600.0, 5000.0):
        assert _counter(1, 2500, max_buy=ceil).counter_offer == 2075


def test_never_offers_at_or_above_ceiling():
    # Tight ceiling: the counter stays invisibly below it.
    d = _counter(3, 2500, max_buy=2080.0)
    assert d.counter_offer < 2080.0


def test_accepts_when_carrier_asks_for_less_than_we_would_offer():
    d = _counter(1, 2050)                          # 2050 < my_offer(2075)
    assert d.decision == "accept" and d.agreed_rate == 2050


def test_coverage_accept_in_last_round_within_ceiling():
    d = _counter(3, 2390)                          # <= ceiling on the last round
    assert d.decision == "accept" and d.agreed_rate == 2390


def test_final_counter_when_above_ceiling_on_last_round():
    d = _counter(3, 2500)                          # > ceiling on the last round
    assert d.decision == "counter" and d.final is True
    assert d.counter_offer <= CEIL


def test_no_deal_when_still_above_ceiling_after_rounds():
    d = _counter(4, 2500)                          # rounds exhausted and still above
    assert d.decision == "reject"


def test_anti_ratchet_hold_when_asking_more_after_deal():
    d = _counter(3, 2350, agreed=2300.0)           # already closed at 2300, they ask 2350
    assert d.decision == "hold" and d.agreed_rate == 2300.0


def test_anti_ratchet_accepts_lower_after_deal():
    d = _counter(3, 2250, agreed=2300.0)           # they ask less -> works in our favor
    assert d.decision == "accept" and d.agreed_rate == 2250.0


def test_opening_anchors_at_posted():
    d = decide(round_number=0, carrier_offer=None, posted=POSTED, max_buy=CEIL,
               agreed_rate=None, params=P)
    assert d.decision == "counter" and d.counter_offer == 2000


def test_never_pays_above_ceiling_in_any_path():
    # Sweep: no decision (accept/counter) ever exceeds the ceiling.
    for rnd in (1, 2, 3, 4):
        for offer in range(1800, 3001, 25):
            d = _counter(rnd, float(offer))
            rate = d.agreed_rate if d.agreed_rate is not None else d.counter_offer
            if rate is not None:
                assert rate <= CEIL


# NegotiationStore: TTL against stale state + reset
def _state(agreed=None):
    return NegotiationState(posted=2000.0, max_buy=2400.0, round=2, agreed_rate=agreed)


def test_store_roundtrip_and_reset():
    s = NegotiationStore()
    s.put("call-1", "LD1", _state(agreed=2100.0))
    assert s.get("call-1", "LD1").agreed_rate == 2100.0
    s.reset("call-1", "LD1")
    assert s.get("call-1", "LD1") is None


def test_store_ttl_evicts_stale_state():
    # The actual bug: a reused call_id inherited an old agreed_rate (1800) -> nonsense counter.
    s = NegotiationStore(ttl_seconds=60.0)
    st = _state(agreed=1800.0)
    s.put("reused-call", "LD1", st)
    st.touched_at -= 120.0                      # age it past the TTL
    assert s.get("reused-call", "LD1") is None  # stale -> fresh state, does NOT inherit 1800


def test_store_ttl_zero_disables_expiry():
    s = NegotiationStore(ttl_seconds=0.0)       # 0 = no expiry
    st = _state()
    s.put("c", "LD1", st)
    st.touched_at -= 10_000.0
    assert s.get("c", "LD1") is not None


def test_store_put_refreshes_ttl():
    s = NegotiationStore(ttl_seconds=60.0)
    st = _state()
    s.put("c", "LD1", st)
    st.touched_at -= 120.0                       # old...
    s.put("c", "LD1", st)                         # ...but rewriting refreshes the clock
    assert s.get("c", "LD1") is not None
