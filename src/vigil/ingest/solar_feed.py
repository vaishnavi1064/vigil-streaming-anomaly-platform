"""The public TDengine solar-fleet feed as a reading source.

Feed: mqtt.tdengine.com:1883, no auth, QoS 0. Topics `sites`, `inverters`, `strings`,
`weather`, `grid`. Observed on 2026-09-06: strings ~504 msg/s, inverters ~30 msg/s,
sites/weather/grid ~3.6 msg/s each.

Each MQTT message describes one entity at one instant and carries a dozen fields. It is
fanned out into one reading per (entity, metric), so a channel is `SITE_001.ac_power_mw`
-- the same shape the synthetic fleet produces, so nothing downstream needs to know which
source it is reading.

**Which metric is the anomaly signal.** Raw generation follows the sun: it collapses every
evening on every channel at once, and a value-only detector reads that as a fleet-wide
incident. The signal that does not is the shortfall between what the plant should be
producing given the irradiance it is actually receiving and what it produced --
`Expected_Power_MW - AC_Power_MW` for sites, the feed's own `Deviation_%` for strings.
Those sit near zero at noon and near zero at midnight, so nightfall is not an excursion.
Raw power is still published as its own channel, deliberately: it is the control that
shows what an unconditioned detector does at sunset (docs/EVALUATION.md).

**The sunset quirk.** A ratio is undefined when its denominator goes to zero, so any ratio
this module derives is suppressed below a production floor rather than emitted as a wild
number invented by division. Fields the feed itself provides are always passed through
unaltered -- deciding they are meaningless is the detector's job, informed by the
irradiance channel, not the bridge's.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from vigil.ingest.source import ReadingSource, SequenceAssigner
from vigil.readings import Reading

log = logging.getLogger(__name__)

# Below this the plant is effectively not generating and derived ratios are noise.
PRODUCTION_FLOOR_MW = 0.05
PRODUCTION_FLOOR_KW = 50.0
PRODUCTION_FLOOR_W = 5.0


@dataclass(frozen=True)
class MetricSpec:
    """One channel derived from an MQTT payload."""

    metric: str
    extract: Callable[[dict[str, Any]], float | None]
    # The metric a detector should watch for this topic. Recorded so the benchmark and the
    # dashboard agree on what "the signal" is instead of each picking its own.
    primary: bool = False


@dataclass(frozen=True)
class TopicMapping:
    topic: str
    entity_field: str
    metrics: tuple[MetricSpec, ...]

    @property
    def primary_metric(self) -> str:
        return next(m.metric for m in self.metrics if m.primary)


def _number(payload: dict[str, Any], field: str) -> float | None:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _plain(field: str, metric: str, *, primary: bool = False) -> MetricSpec:
    return MetricSpec(metric=metric, extract=lambda p: _number(p, field), primary=primary)


def _site_shortfall_mw(payload: dict[str, Any]) -> float | None:
    expected = _number(payload, "Expected_Power_MW")
    actual = _number(payload, "AC_Power_MW")
    if expected is None or actual is None:
        return None
    return expected - actual


def _inverter_capacity_factor(payload: dict[str, Any]) -> float | None:
    rated = _number(payload, "Rated_AC_kW")
    output = _number(payload, "Output_AC_Power_kW")
    if rated is None or output is None or rated < PRODUCTION_FLOOR_KW:
        return None
    return output / rated


def _string_shortfall_w(payload: dict[str, Any]) -> float | None:
    expected = _number(payload, "Expected_Power_W")
    actual = _number(payload, "Actual_Power_W")
    if expected is None or actual is None:
        return None
    return expected - actual


TOPIC_MAPPINGS: dict[str, TopicMapping] = {
    "sites": TopicMapping(
        topic="sites",
        entity_field="Site_ID",
        metrics=(
            # Diurnally neutral: near zero whether the sun is overhead or down.
            MetricSpec("power_shortfall_mw", _site_shortfall_mw, primary=True),
            # Kept as the control that demonstrates the sunset false positive.
            _plain("AC_Power_MW", "ac_power_mw"),
            _plain("Expected_Power_MW", "expected_power_mw"),
            _plain("Performance_Ratio", "performance_ratio"),
            _plain("POA_Irradiance_Wm2", "poa_irradiance_wm2"),
            _plain("Curtailment_%", "curtailment_pct"),
            _plain("Availability_%", "availability_pct"),
            _plain("Soiling_Index", "soiling_index"),
        ),
    ),
    "inverters": TopicMapping(
        topic="inverters",
        entity_field="Inverter_ID",
        metrics=(
            # Inverters publish no expected-power field, so the normalised analogue is the
            # local performance ratio: output against what these conditions warrant.
            _plain("PR_Local", "pr_local", primary=True),
            _plain("Efficiency_%", "efficiency_pct"),
            _plain("Output_AC_Power_kW", "output_ac_power_kw"),
            _plain("Temperature_Module", "temperature_module_c"),
            _plain("Input_DC_Voltage", "input_dc_voltage_v"),
            _plain("AC_Voltage", "ac_voltage_v"),
            _plain("Availability_%", "availability_pct"),
            MetricSpec("capacity_factor", _inverter_capacity_factor),
        ),
    ),
    "strings": TopicMapping(
        topic="strings",
        entity_field="String_ID",
        metrics=(
            _plain("Deviation_%", "deviation_pct", primary=True),
            MetricSpec("power_shortfall_w", _string_shortfall_w),
            _plain("Actual_Power_W", "actual_power_w"),
            _plain("Expected_Power_W", "expected_power_w"),
            _plain("DC_Current_A", "dc_current_a"),
            _plain("DC_Voltage_V", "dc_voltage_v"),
            _plain("Temperature_Cell", "temperature_cell_c"),
            _plain("Soiling_Factor", "soiling_factor"),
        ),
    ),
    "weather": TopicMapping(
        topic="weather",
        entity_field="Station_ID",
        metrics=(
            _plain("POA_Irradiance_Wm2", "poa_irradiance_wm2", primary=True),
            _plain("GHI_Wm2", "ghi_wm2"),
            _plain("DNI_Wm2", "dni_wm2"),
            _plain("Ambient_Temperature_C", "ambient_temperature_c"),
            _plain("Module_Temperature_C", "module_temperature_c"),
            _plain("Wind_Speed_mps", "wind_speed_mps"),
            _plain("Humidity_%", "humidity_pct"),
        ),
    ),
    "grid": TopicMapping(
        topic="grid",
        entity_field="Meter_ID",
        metrics=(
            # What the operator asked for against what the meter recorded.
            MetricSpec(
                "setpoint_shortfall_mw",
                lambda p: (
                    None
                    if _number(p, "Setpoint_MW") is None or _number(p, "Active_Power_MW") is None
                    else _number(p, "Setpoint_MW") - _number(p, "Active_Power_MW")
                ),
                primary=True,
            ),
            _plain("Active_Power_MW", "active_power_mw"),
            _plain("Setpoint_MW", "setpoint_mw"),
            _plain("Voltage_kV", "voltage_kv"),
            _plain("Frequency_Hz", "frequency_hz"),
            _plain("Curtailment_MW", "curtailment_mw"),
        ),
    ),
}

SOLAR_TOPICS = tuple(TOPIC_MAPPINGS)


def parse_event_ts_ms(raw: str) -> int:
    """The feed stamps ISO 8601 with an offset; that is the event time."""
    return int(datetime.fromisoformat(raw).timestamp() * 1000)


def map_payload(topic: str, payload: dict[str, Any], sequences: SequenceAssigner) -> list[Reading]:
    """Fan one MQTT message out into one reading per (entity, metric).

    Returns an empty list for a message that cannot be placed on the timeline or attributed
    to an entity: a reading with no event time or no channel is not a partial reading, it is
    an unusable one, and inventing a timestamp would corrupt the windowing it feeds.
    """
    mapping = TOPIC_MAPPINGS.get(topic)
    if mapping is None:
        return []

    entity = payload.get(mapping.entity_field)
    raw_ts = payload.get("ts")
    if not isinstance(entity, str) or not isinstance(raw_ts, str):
        return []
    try:
        event_ts_ms = parse_event_ts_ms(raw_ts)
    except ValueError:
        return []

    out: list[Reading] = []
    for spec in mapping.metrics:
        value = spec.extract(payload)
        if value is None:
            continue
        channel = f"{entity}.{spec.metric}"
        out.append(
            Reading(
                channel=channel,
                seq=sequences.next_for(channel),
                event_ts_ms=event_ts_ms,
                value=value,
            )
        )
    return out


class SolarFleetSource(ReadingSource):
    """Live readings from the public solar-fleet MQTT feed.

    The paho client runs its own network thread and hands messages to this one through a
    bounded queue. Bounded on purpose: if the publisher outruns the consumer, the right
    behaviour is to drop and say how many were dropped, not to grow a queue until the
    process dies. Dropped counts are reported, never silently absorbed.

    The feed is public, free and carries no SLA, so reconnection backs off exponentially to
    a one-minute ceiling and the client identifies itself. One connection per process.
    """

    def __init__(
        self,
        host: str,
        port: int,
        topics: tuple[str, ...],
        *,
        client_id: str | None = None,
        queue_size: int = 100_000,
        reconnect_min_s: float = 5.0,
        reconnect_max_s: float = 60.0,
    ) -> None:
        self.name = f"solar-fleet:{','.join(topics)}"
        self.host = host
        self.port = port
        self.topics = topics
        self.sequences = SequenceAssigner()
        self.messages_received = 0
        self.messages_unparsable = 0
        self.messages_dropped = 0
        self._queue: queue.Queue[tuple[str, bytes]] = queue.Queue(maxsize=queue_size)
        self._closed = threading.Event()
        self._client_id = client_id or f"vigil-ingest-{int(time.time())}"
        self._reconnect_min_s = reconnect_min_s
        self._reconnect_max_s = reconnect_max_s
        self._client = self._build_client()

    def _build_client(self):
        import paho.mqtt.client as mqtt

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self._client_id)
        client.reconnect_delay_set(
            min_delay=int(self._reconnect_min_s), max_delay=int(self._reconnect_max_s)
        )

        def on_connect(_client, _userdata, _flags, reason_code, _properties=None):
            if reason_code != 0:
                log.error("mqtt connect refused: %s", reason_code)
                return
            log.info("connected to %s:%s, subscribing to %s", self.host, self.port, self.topics)
            # Re-subscribing inside on_connect rather than once at startup is what makes a
            # reconnect actually restore the stream; subscriptions do not survive the
            # session with a clean session.
            for topic in self.topics:
                _client.subscribe(topic, qos=0)

        def on_disconnect(_client, _userdata, _flags, reason_code, _properties=None):
            if not self._closed.is_set():
                log.warning("mqtt disconnected (%s); paho will retry with backoff", reason_code)

        def on_message(_client, _userdata, message):
            try:
                self._queue.put_nowait((message.topic, message.payload))
            except queue.Full:
                self.messages_dropped += 1

        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        client.on_message = on_message
        return client

    def readings(self) -> Iterator[Reading]:
        self._client.connect(self.host, self.port, keepalive=60)
        self._client.loop_start()
        try:
            while not self._closed.is_set():
                try:
                    topic, raw = self._queue.get(timeout=0.5)
                except queue.Empty:
                    continue
                self.messages_received += 1
                try:
                    payload = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    self.messages_unparsable += 1
                    continue
                if not isinstance(payload, dict):
                    self.messages_unparsable += 1
                    continue
                yield from map_payload(topic, payload, self.sequences)
        finally:
            self._client.loop_stop()

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._client.disconnect()
        except Exception:  # noqa: BLE001 - closing must not mask the original error
            log.debug("mqtt disconnect during close failed", exc_info=True)
