"""Tests for the properties that make the core claim falsifiable.

Most of these assert on the *populations* the scenario schedules rather than on values.
That is the point: if the generator ever stops producing quiet deploys, or stops placing
real faults inside deploy windows, the evaluation silently loses its ability to tell
targeted attribution from blanket muting, and every later false-positive number becomes
unfalsifiable. These tests are the guard on that (ADR-015).
"""

import pytest

from vigil.context import ContextKind, Severity
from vigil.scenario import build_scenario
from vigil.synthetic import AnomalyOrigin, ChannelSimulator, ChannelSpec, default_fleet

CHANNELS = tuple(f"ch-{i:02d}" for i in range(10))
HOUR = 3600.0


def plan(**kw):
    params = dict(seed=11, deploys_per_hour=40.0, faults_per_hour=60.0)
    params.update(kw)
    return build_scenario(CHANNELS, HOUR, **params)


def test_a_scenario_is_reproducible_from_its_seed():
    a, b = plan(), plan()
    assert [e.event_id for e in a.context_events()] == [e.event_id for e in b.context_events()]
    assert [(e.t_start_ms, e.t_end_ms) for e in a.context_events()] == [
        (e.t_start_ms, e.t_end_ms) for e in b.context_events()
    ]
    assert a.summary() == b.summary()


def test_different_seeds_produce_different_scenarios():
    assert plan(seed=1).summary() != plan(seed=2).summary()


def test_both_perturbing_and_quiet_deploys_are_scheduled():
    # Without quiet deploys, suppressing on the marker alone is indistinguishable from
    # attributing an artifact to its cause.
    p = plan()
    assert p.perturbing_deploys, "no perturbing deploys: nothing legitimate to attribute"
    assert p.quiet_deploys, "no quiet deploys: blanket suppression would be undetectable"


def test_a_quiet_deploy_perturbs_nothing_and_says_so():
    for d in plan().quiet_deploys:
        assert d.artifacts == {}
        assert d.event.perturbed_telemetry is False


def test_a_perturbing_deploy_produces_artifacts_only_on_channels_it_touched():
    # A deploy that never touched a channel must not be usable to explain an excursion
    # there; that is the cheapest route from suppression to over-suppression.
    for d in plan().perturbing_deploys:
        assert d.artifacts
        assert set(d.artifacts) <= set(d.event.scope)


def test_every_artifact_is_labelled_as_deploy_caused():
    for d in plan().perturbing_deploys:
        for episodes in d.artifacts.values():
            assert all(e.origin is AnomalyOrigin.DEPLOY for e in episodes)
            assert not any(e.is_real for e in episodes)


def test_real_faults_are_scheduled_both_inside_and_outside_deploy_windows():
    p = plan()
    inside = p.faults_inside_deploy_windows()
    outside = p.faults_outside_deploy_windows()
    assert inside, "no faults inside deploy windows: recall loss under a window is untestable"
    assert outside, "no faults outside deploy windows: there is no control population"


def test_every_scheduled_fault_is_labelled_real():
    p = plan()
    for _, ep in p.faults_inside_deploy_windows() + p.faults_outside_deploy_windows():
        assert ep.origin is AnomalyOrigin.FAULT
        assert ep.is_real


def test_faults_placed_outside_really_do_not_overlap_any_deploy():
    p = plan()
    spans = [(d.event.t_start_ms, d.event.t_end_ms, d.event.scope) for d in p.deploys]
    for channel, ep in p.faults_outside_deploy_windows():
        start_ms, end_ms = int(ep.start_s * 1000), int(ep.end_s * 1000)
        for lo, hi, scope in spans:
            overlaps = lo <= end_ms and start_ms <= hi
            in_scope = not scope or channel in scope
            assert not (overlaps and in_scope), "a supposedly-outside fault overlaps a deploy"


def test_faults_placed_inside_land_on_a_channel_the_deploy_actually_touched():
    p = plan()
    for channel, ep in p.faults_inside_deploy_windows():
        covering = p._covering_deploy(channel, ep)
        assert covering is not None
        assert covering.event.applies_to(channel)


def test_faults_are_scheduled_inside_quiet_deploys_too():
    # The sharpest test in the suite: during a quiet deploy there is no artifact at all,
    # so anything suppressed there could only be blanket muting.
    found = False
    for seed in range(25):
        p = plan(seed=seed, fault_in_window_fraction=0.6)
        quiet_ids = {d.event.event_id for d in p.quiet_deploys}
        for channel, ep in p.faults_inside_deploy_windows():
            covering = p._covering_deploy(channel, ep)
            if covering and covering.event.event_id in quiet_ids:
                found = True
                break
        if found:
            break
    assert found, "no seed placed a real fault inside a quiet deploy window"


def test_the_in_window_fraction_is_respected_within_scheduling_slack():
    p = plan(fault_in_window_fraction=0.5, faults_per_hour=200.0)
    inside = len(p.faults_inside_deploy_windows())
    total = inside + len(p.faults_outside_deploy_windows())
    assert 0.3 <= inside / total <= 0.7


def test_the_quiet_fraction_is_respected_within_sampling_slack():
    p = plan(quiet_deploy_fraction=0.5, deploys_per_hour=300.0)
    quiet = len(p.quiet_deploys) / len(p.deploys)
    assert 0.35 <= quiet <= 0.65


def test_deploys_touch_a_subset_of_the_fleet_not_always_everything():
    # If every deploy were fleet-wide, scope would carry no information and a policy that
    # ignored it would be indistinguishable from one that respected it.
    p = plan(deploys_per_hour=300.0)
    sizes = {len(d.event.scope) for d in p.deploys}
    assert min(sizes) < len(CHANNELS), "every deploy touched the whole fleet"


def test_deploy_markers_are_well_formed_context_events():
    for e in plan().context_events():
        assert e.kind is ContextKind.DEPLOY
        assert e.t_end_ms > e.t_start_ms
        assert e.severity in (Severity.INFO, Severity.WARNING, Severity.CRITICAL)
        assert e.detail
        assert e.scope


def test_markers_round_trip_through_the_wire_codec():
    from vigil.context import ContextEvent

    for e in plan().context_events():
        assert ContextEvent.from_json(e.to_json()) == e


def test_context_events_come_out_in_time_order():
    starts = [e.t_start_ms for e in plan().context_events()]
    assert starts == sorted(starts)


def test_episodes_for_a_channel_merges_faults_and_artifacts_in_time_order():
    p = plan()
    for channel in CHANNELS:
        episodes = p.episodes_for(channel)
        assert [e.start_s for e in episodes] == sorted(e.start_s for e in episodes)
        origins = {e.origin for e in episodes}
        assert origins <= {AnomalyOrigin.FAULT, AnomalyOrigin.DEPLOY}


def test_a_scenario_needs_at_least_one_channel():
    with pytest.raises(ValueError, match="at least one channel"):
        build_scenario((), HOUR)


def test_no_deploys_still_yields_a_usable_all_outside_scenario():
    p = plan(deploys_per_hour=0.0)
    assert p.deploys == []
    assert p.faults_outside_deploy_windows()
    assert p.faults_inside_deploy_windows() == []


# --- the simulator honours a scheduled plan ---

SPEC = ChannelSpec(
    name="ch-00",
    base=10.0,
    seasonal_amplitude=0.0,
    seasonal_period_s=100.0,
    noise_sigma=1.0,
    noise_persistence=0.8,
)


def test_a_scheduled_episode_is_reproduced_by_the_simulator():
    p = plan(seed=5, faults_per_hour=400.0)
    episodes = p.episodes_for("ch-00")
    assert episodes, "seed produced no episodes on ch-00"
    sim = ChannelSimulator(SPEC, seed=1, anomalies_per_hour=0.0, scheduled=episodes)
    target = episodes[0]
    samples = [(t, sim.sample(t)) for t in _grid(0.0, target.end_s + 30.0, 0.25)]
    labelled = [t for t, s in samples if s.injected is not None]
    assert labelled, "a scheduled episode produced no labelled samples"
    assert min(labelled) >= target.start_s - 0.5


def test_the_origin_of_a_scheduled_episode_reaches_the_sample():
    artifact = next(
        (e for d in plan(seed=5).perturbing_deploys for eps in d.artifacts.values() for e in eps),
        None,
    )
    assert artifact is not None
    sim = ChannelSimulator(SPEC, seed=1, anomalies_per_hour=0.0, scheduled=[artifact])
    origins = {
        s.origin
        for s in (sim.sample(t) for t in _grid(0.0, artifact.end_s + 5.0, 0.25))
        if s.origin
    }
    assert origins == {AnomalyOrigin.DEPLOY}


def test_a_scheduled_run_generates_no_unplanned_episodes():
    # Unplanned excursions would surface as detections the ground truth cannot account
    # for, which would corrupt every precision number computed against the plan.
    p = plan(seed=9)
    episodes = p.episodes_for("ch-00")
    sim = ChannelSimulator(SPEC, seed=1, anomalies_per_hour=0.0, scheduled=episodes)
    for t in _grid(0.0, HOUR, 1.0):
        sim.sample(t)
    assert all(e in episodes for e in sim.episodes)


def test_origin_survives_the_reading_wire_codec():
    from vigil.readings import Reading

    r = Reading(channel="c", seq=1, event_ts_ms=1, value=0.0, injected="spike", origin="deploy")
    assert Reading.from_json(r.to_json()) == r


def test_origin_is_omitted_from_the_wire_when_absent():
    from vigil.readings import Reading

    assert b"origin" not in Reading(channel="c", seq=1, event_ts_ms=1, value=0.0).to_json()


# ------------------------- topology-aware placement (v4, ADR-038) -------------------------
# Where an excursion lands is now as deliberate as when. These guard the populations the
# blast-radius discriminator is scored against: if the generator stops producing node
# faults inside deploy windows, or stops spreading deploys across machines, the topology
# test becomes unfalsifiable in exactly the way ADR-015 exists to prevent.

FLEET_24 = tuple(spec.name for spec in default_fleet(24))


def topological_plan(channels=FLEET_24, **kw):
    params = dict(seed=4242, deploys_per_hour=120.0, faults_per_hour=240.0)
    params.update(kw)
    return build_scenario(channels, HOUR, **params)


def test_a_rollout_reaches_several_machines_not_a_random_handful_of_channels():
    p = topological_plan()
    spread = [len(d.nodes) for d in p.deploys if not d.canary]
    assert spread, "every deploy was a single-machine canary"
    assert min(spread) >= 2


def test_a_rollout_stays_inside_one_deploy_ring():
    p = topological_plan()
    for deploy in p.deploys:
        if not deploy.ring:
            continue
        assert set(p.topology.footprint(deploy.event.scope).rings) == {deploy.ring}


def test_a_rollout_touches_the_same_metrics_on_each_machine_it_reaches():
    # A redeploy of the vibration collector moves vibration everywhere it runs. A scope
    # sampled channel-by-channel would have no recognisable shape at all.
    p = topological_plan()
    for deploy in p.deploys:
        if len(deploy.nodes) < 2:
            continue
        per_node = {
            node: {c.partition(".")[2] for c in deploy.event.scope if c.startswith(f"{node}.")}
            for node in deploy.nodes
        }
        assert len({frozenset(v) for v in per_node.values()}) == 1


def test_single_machine_canary_rollouts_are_a_real_population():
    # They cannot be attributed by any topology test, so a generator that omitted them
    # would be quietly removing the cases the discriminator loses on.
    p = topological_plan()
    assert p.canary_deploys


def test_a_machine_fault_moves_several_metrics_on_one_machine():
    p = topological_plan()
    node_faults = [
        members
        for members in p.fault_incidents().values()
        if members[0][1].domain == "node" and len(members) > 1
    ]
    assert node_faults
    for members in node_faults:
        nodes = {p.topology.node_of(channel) for channel, _ in members}
        assert len(nodes) == 1


def test_a_machine_faults_metrics_move_within_the_synchrony_tolerance_of_each_other():
    # Deliberate. If a real fault were never synchronous, the timing test would already
    # have worked and there would have been nothing to fix.
    p = topological_plan()
    for members in p.fault_incidents().values():
        if len(members) < 2:
            continue
        onsets = [ep.start_s for _, ep in members]
        assert max(onsets) - min(onsets) <= 5.0


def test_a_cabinet_fault_moves_several_machines_inside_one_rack():
    p = topological_plan()
    rack_faults = [
        members for members in p.fault_incidents().values() if members[0][1].domain == "rack"
    ]
    assert rack_faults, "the population that punishes a node-spread-only rule is missing"
    for members in rack_faults:
        racks = {p.topology.placement(channel).rack for channel, _ in members}
        nodes = {p.topology.node_of(channel) for channel, _ in members}
        assert len(racks) == 1
        assert len(nodes) >= 2


def test_single_sensor_faults_remain_a_real_population():
    p = topological_plan()
    assert any(m[0][1].domain == "channel" for m in p.fault_incidents().values())


def test_a_fault_inside_a_deploy_window_lands_on_channels_the_deploy_touched():
    """The trap the whole of v4 turns on.

    A machine fault whose channels are all inside a deploy's scope, moving within seconds
    of each other, is indistinguishable from a deploy artifact on timing and scope alone.
    If the generator stopped producing these, the topology test would have nothing to beat.
    """
    p = topological_plan()
    trapped = 0
    for members in p.fault_incidents().values():
        channels = {c for c, _ in members}
        if len(channels) < 2:
            continue
        for deploy in p.deploys:
            span = (deploy.event.t_start_ms / 1000.0, deploy.event.t_end_ms / 1000.0)
            start = min(ep.start_s for _, ep in members)
            if span[0] <= start <= span[1] and channels <= set(deploy.event.scope):
                trapped += 1
                break
    assert trapped, "no multi-channel fault landed wholly inside a deploy's scope"


def test_every_member_of_an_incident_shares_its_identifier_and_domain():
    p = topological_plan()
    for incident, members in p.fault_incidents().items():
        assert {ep.incident for _, ep in members} == {incident}
        assert len({ep.domain for _, ep in members}) == 1


def test_an_incident_is_counted_once_however_many_channels_it_moved():
    p = topological_plan()
    channel_episodes = sum(len(v) for v in p.faults.values())
    assert len(p.fault_incidents()) < channel_episodes
    assert sum(p.faults_by_domain().values()) == len(p.fault_incidents())


def test_a_fleet_with_no_structure_in_its_names_still_schedules_faults():
    # ch-00 .. ch-09 are their own machines, so every fault is single-channel. The
    # generator must degrade to that rather than failing to place anything.
    p = topological_plan(CHANNELS)
    assert p.fault_incidents()
    assert set(p.faults_by_domain()) <= {"channel", "node", "rack"}


def _grid(start: float, stop: float, step: float):
    t = start
    while t <= stop:
        yield t
        t += step
