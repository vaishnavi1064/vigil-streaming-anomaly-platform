"""Mapping tests against payloads captured from the live feed on 2026-09-06.

Real captured messages rather than invented ones: a mapping test that asserts against a
fixture the mapping's own author made up proves only that the author was self-consistent.
"""

import pytest

from vigil.ingest.solar_feed import (
    SOLAR_TOPICS,
    TOPIC_MAPPINGS,
    map_payload,
    parse_event_ts_ms,
)
from vigil.ingest.source import SequenceAssigner

SITE = {
    "ts": "2026-09-06T04:31:55.125720+00:00",
    "Site_ID": "SITE_001",
    "Fleet": "california",
    "Location": "34.4208,-118.4696",
    "MWdc": 26.25,
    "MWac": 21,
    "POA_Irradiance_Wm2": 1091.68,
    "AC_Power_MW": 21.0,
    "Expected_Power_MW": 22.29,
    "Performance_Ratio": 0.96,
    "Availability_%": 99.2,
    "Curtailment_%": 19.7,
    "Energy_Today_MWh": 139.79,
    "Soiling_Index": 0.911,
    "Active_Alarms": 1,
    "Alarm_Messages": ["MAINTENANCE: Soiling index on strings S06-S12 measured at 0.87"],
}

STRING = {
    "ts": "2026-09-06T04:31:21.625894+00:00",
    "String_ID": "INV_001_001_STR001",
    "Inverter_ID": "INV_001_001",
    "Site_ID": "SITE_001",
    "Fleet": "california",
    "Location": "34.4208,-118.4696",
    "DC_Current_A": 0.09,
    "DC_Voltage_V": 854.99,
    "Expected_Power_W": 82.54,
    "Actual_Power_W": 80.4,
    "Deviation_%": -2.59,
    "Status": "Normal",
    "Temperature_Cell": 51.08,
    "Soiling_Factor": 0.956,
    "Last_Cleaned": "2024-01-15",
}

INVERTER = {
    "ts": "2026-09-06T04:31:54.075966+00:00",
    "Inverter_ID": "INV_005_006",
    "Site_ID": "SITE_005",
    "Fleet": "california",
    "Location": "33.8121,-115.4336",
    "Model": "SMA-SC2500",
    "Rated_AC_kW": 2000.0,
    "Input_DC_Voltage": 651.91,
    "Output_AC_Power_kW": 2000.0,
    "AC_Voltage": 486.32,
    "AC_Current": 3195.62,
    "Temperature_Module": 51.23,
    "Efficiency_%": 95,
    "Status": "Running",
    "Status_Message": "RUNNING: INV_005_006 active and generating power.",
    "Availability_%": 95,
    "PR_Local": 0.867,
    "Energy_Today_kWh": 10084.16,
}

GRID = {
    "ts": "2026-09-06T04:31:55.193427+00:00",
    "Meter_ID": "GRID_001",
    "Site_ID": "SITE_001",
    "Active_Power_MW": 20.91,
    "Reactive_Power_MVar": 0.0,
    "Voltage_kV": 33.88,
    "Frequency_Hz": 60.159,
    "Setpoint_MW": 16.08,
    "Curtailment_MW": 0.0,
    "Curtailment_%": 0.0,
    "Status": "SYSTEM NORMAL",
    "Energy_Today_MWh": 131.13,
}

WEATHER = {
    "ts": "2026-09-06T04:31:55.192854+00:00",
    "Station_ID": "WS_001",
    "Site_ID": "SITE_001",
    "POA_Irradiance_Wm2": 1091.68,
    "GHI_Wm2": 1112.1,
    "DNI_Wm2": 784.2,
    "Ambient_Temperature_C": 14.54,
    "Module_Temperature_C": 30.02,
    "Wind_Speed_mps": 0.0,
    "Wind_Direction_deg": 29.0,
    "Humidity_%": 60.03,
}

CAPTURED = {
    "sites": SITE,
    "strings": STRING,
    "inverters": INVERTER,
    "grid": GRID,
    "weather": WEATHER,
}


def channels(readings):
    return {r.channel: r.value for r in readings}


def test_event_time_comes_from_the_feed_not_the_clock():
    # Using arrival time would destroy the event-time windowing this feeds.
    assert parse_event_ts_ms("2026-09-06T04:31:55.125720+00:00") == 1788669115125


@pytest.mark.parametrize("topic", SOLAR_TOPICS)
def test_every_mapped_topic_produces_readings_from_a_real_payload(topic):
    got = map_payload(topic, CAPTURED[topic], SequenceAssigner())
    assert got, f"{topic} mapping produced nothing from a captured message"


@pytest.mark.parametrize("topic", SOLAR_TOPICS)
def test_every_topic_declares_exactly_one_primary_metric(topic):
    mapping = TOPIC_MAPPINGS[topic]
    assert sum(1 for m in mapping.metrics if m.primary) == 1


@pytest.mark.parametrize("topic", SOLAR_TOPICS)
def test_the_primary_metric_is_actually_emitted(topic):
    mapping = TOPIC_MAPPINGS[topic]
    got = channels(map_payload(topic, CAPTURED[topic], SequenceAssigner()))
    assert any(c.endswith("." + mapping.primary_metric) for c in got)


def test_site_shortfall_is_expected_minus_actual():
    got = channels(map_payload("sites", SITE, SequenceAssigner()))
    assert got["SITE_001.power_shortfall_mw"] == pytest.approx(22.29 - 21.0)


def test_site_keeps_raw_power_as_the_sunset_control():
    # Deliberately retained: this is the channel that shows what an unconditioned detector
    # does at nightfall, which is the comparison docs/EVALUATION.md is built on.
    got = channels(map_payload("sites", SITE, SequenceAssigner()))
    assert got["SITE_001.ac_power_mw"] == 21.0


def test_string_deviation_is_passed_through_unaltered():
    got = channels(map_payload("strings", STRING, SequenceAssigner()))
    assert got["INV_001_001_STR001.deviation_pct"] == -2.59


def test_string_entity_is_the_string_not_its_inverter():
    got = channels(map_payload("strings", STRING, SequenceAssigner()))
    assert all(c.startswith("INV_001_001_STR001.") for c in got)


def test_grid_shortfall_is_setpoint_minus_delivered():
    got = channels(map_payload("grid", GRID, SequenceAssigner()))
    assert got["GRID_001.setpoint_shortfall_mw"] == pytest.approx(16.08 - 20.91)


def test_inverter_capacity_factor_is_output_over_rating():
    got = channels(map_payload("inverters", INVERTER, SequenceAssigner()))
    assert got["INV_005_006.capacity_factor"] == pytest.approx(1.0)


def test_a_derived_ratio_is_withheld_rather_than_invented_when_its_denominator_vanishes():
    # This is the sunset guard: dividing a near-zero output by a near-zero rating yields a
    # number, and that number is noise. Emitting it would manufacture anomalies at night.
    dark = INVERTER | {"Rated_AC_kW": 0.0, "Output_AC_Power_kW": 0.0}
    got = channels(map_payload("inverters", dark, SequenceAssigner()))
    assert "INV_005_006.capacity_factor" not in got
    # Fields the feed itself provides still come through: judging them is the detector's job.
    assert "INV_005_006.output_ac_power_kw" in got


def test_a_field_the_feed_reports_as_zero_is_still_published():
    # Zero is a measurement, not a missing value. Dropping it would put a hole in the
    # series that gap detection would then report as an outage.
    quiet = SITE | {"AC_Power_MW": 0.0, "Expected_Power_MW": 0.0}
    got = channels(map_payload("sites", quiet, SequenceAssigner()))
    assert got["SITE_001.ac_power_mw"] == 0.0
    assert got["SITE_001.power_shortfall_mw"] == 0.0


def test_shortfall_stays_near_zero_at_night_which_is_why_it_is_the_primary_signal():
    noon = SITE | {"Expected_Power_MW": 22.29, "AC_Power_MW": 21.0}
    night = SITE | {"Expected_Power_MW": 0.02, "AC_Power_MW": 0.01}
    noon_v = channels(map_payload("sites", noon, SequenceAssigner()))
    night_v = channels(map_payload("sites", night, SequenceAssigner()))
    # Raw power collapses across the fleet at sunset; the shortfall does not move.
    assert noon_v["SITE_001.ac_power_mw"] - night_v["SITE_001.ac_power_mw"] > 20.0
    assert abs(noon_v["SITE_001.power_shortfall_mw"] - night_v["SITE_001.power_shortfall_mw"]) < 2.0


def test_non_numeric_fields_are_skipped_not_coerced():
    got = channels(map_payload("inverters", INVERTER, SequenceAssigner()))
    assert not any("status" in c for c in got)
    assert not any("model" in c for c in got)


def test_a_boolean_is_not_treated_as_a_number():
    # bool is an int subclass in Python; a flag silently becoming a 1.0 measurement would
    # be a plausible-looking series that means nothing.
    payload = INVERTER | {"PR_Local": True}
    got = channels(map_payload("inverters", payload, SequenceAssigner()))
    assert "INV_005_006.pr_local" not in got


def test_a_message_without_a_timestamp_is_dropped_whole():
    # Stamping an arrival time would silently corrupt event-time windowing.
    assert (
        map_payload("sites", {k: v for k, v in SITE.items() if k != "ts"}, SequenceAssigner()) == []
    )


def test_a_message_with_an_unparsable_timestamp_is_dropped_whole():
    assert map_payload("sites", SITE | {"ts": "yesterday"}, SequenceAssigner()) == []


def test_a_message_without_its_entity_id_is_dropped_whole():
    assert (
        map_payload("sites", {k: v for k, v in SITE.items() if k != "Site_ID"}, SequenceAssigner())
        == []
    )


def test_an_unmapped_topic_yields_nothing_rather_than_guessing():
    assert (
        map_payload("datacenter/nodes", {"ts": SITE["ts"], "node_id": "x"}, SequenceAssigner())
        == []
    )


def test_sequences_advance_per_channel_across_successive_messages():
    seqs = SequenceAssigner()
    first = map_payload("sites", SITE, seqs)
    second = map_payload("sites", SITE, seqs)
    assert all(r.seq == 1 for r in first)
    assert all(r.seq == 2 for r in second)


def test_two_entities_on_one_topic_get_independent_sequences():
    seqs = SequenceAssigner()
    map_payload("sites", SITE, seqs)
    other = map_payload("sites", SITE | {"Site_ID": "SITE_009"}, seqs)
    assert all(r.seq == 1 for r in other)


def test_live_readings_carry_no_injected_label():
    # Ground truth exists only on the synthetic source; claiming it here would be a lie.
    assert all(r.injected is None for r in map_payload("sites", SITE, SequenceAssigner()))


def test_readings_survive_the_wire_codec():
    from vigil.readings import Reading

    for r in map_payload("strings", STRING, SequenceAssigner()):
        assert Reading.from_json(r.to_json()) == r
