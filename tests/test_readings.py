from vigil.readings import Reading


def test_round_trip_preserves_every_field():
    r = Reading(channel="pump-00.vibration_mm_s", seq=42, event_ts_ms=1757000000123, value=4.5)
    assert Reading.from_json(r.to_json()) == r


def test_injected_label_round_trips_when_present():
    r = Reading(channel="c", seq=1, event_ts_ms=1, value=0.0, injected="spike")
    assert Reading.from_json(r.to_json()).injected == "spike"


def test_absent_label_is_omitted_from_the_wire_not_serialised_as_null():
    # Most readings are normal, so paying bytes for an always-null field on the hot path
    # would be a measurable waste at 20k events/s.
    assert b"injected" not in Reading(channel="c", seq=1, event_ts_ms=1, value=0.0).to_json()


def test_value_survives_full_float_precision():
    r = Reading(channel="c", seq=1, event_ts_ms=1, value=0.1 + 0.2)
    assert Reading.from_json(r.to_json()).value == 0.1 + 0.2
