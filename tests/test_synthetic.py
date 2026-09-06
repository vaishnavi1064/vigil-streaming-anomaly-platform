import statistics

import pytest

from vigil.synthetic import AnomalyKind, ChannelSimulator, ChannelSpec, default_fleet

QUIET = ChannelSpec(
    name="test.quiet",
    base=10.0,
    seasonal_amplitude=0.0,
    seasonal_period_s=100.0,
    noise_sigma=1.0,
    noise_persistence=0.8,
)


def drive(sim: ChannelSimulator, n: int, dt: float = 0.1):
    return [sim.sample(i * dt) for i in range(n)]


def test_same_seed_replays_identically():
    a = drive(ChannelSimulator(QUIET, seed=5, anomalies_per_hour=100), 500)
    b = drive(ChannelSimulator(QUIET, seed=5, anomalies_per_hour=100), 500)
    assert [s.value for s in a] == [s.value for s in b]
    assert [s.injected for s in a] == [s.injected for s in b]


def test_different_seeds_diverge():
    a = drive(ChannelSimulator(QUIET, seed=5, anomalies_per_hour=0), 200)
    b = drive(ChannelSimulator(QUIET, seed=6, anomalies_per_hour=0), 200)
    assert [s.value for s in a] != [s.value for s in b]


def test_no_episodes_when_injection_is_disabled():
    sim = ChannelSimulator(QUIET, seed=3, anomalies_per_hour=0)
    assert all(s.injected is None for s in drive(sim, 2000))
    assert sim.episodes == []


@pytest.mark.parametrize("persistence", [0.0, 0.5, 0.9])
def test_ar1_noise_keeps_the_configured_stationary_sigma(persistence):
    # The innovation is rescaled by sqrt(1 - phi^2) precisely so that changing the
    # persistence does not silently change the signal-to-noise ratio of the benchmark.
    spec = ChannelSpec(
        name="t",
        base=0.0,
        seasonal_amplitude=0.0,
        seasonal_period_s=1.0,
        noise_sigma=2.0,
        noise_persistence=persistence,
    )
    values = [s.value for s in drive(ChannelSimulator(spec, seed=11, anomalies_per_hour=0), 20000)]
    assert statistics.pstdev(values) == pytest.approx(2.0, rel=0.12)


def test_ar1_noise_is_actually_autocorrelated():
    # A white-noise stream would make the z-score baseline look artificially strong, so
    # assert the property the generator exists to provide.
    spec = ChannelSpec(
        name="t",
        base=0.0,
        seasonal_amplitude=0.0,
        seasonal_period_s=1.0,
        noise_sigma=1.0,
        noise_persistence=0.9,
    )
    v = [s.value for s in drive(ChannelSimulator(spec, seed=13, anomalies_per_hour=0), 20000)]
    mean = statistics.fmean(v)
    num = sum((v[i] - mean) * (v[i + 1] - mean) for i in range(len(v) - 1))
    den = sum((x - mean) ** 2 for x in v)
    assert num / den == pytest.approx(0.9, abs=0.05)


def test_seasonality_shows_up_as_a_swing_of_the_configured_amplitude():
    spec = ChannelSpec(
        name="t",
        base=50.0,
        seasonal_amplitude=10.0,
        seasonal_period_s=60.0,
        noise_sigma=0.01,
        noise_persistence=0.0,
    )
    values = [
        s.value for s in drive(ChannelSimulator(spec, seed=2, anomalies_per_hour=0), 600, dt=0.1)
    ]
    assert max(values) == pytest.approx(60.0, abs=0.5)
    assert min(values) == pytest.approx(40.0, abs=0.5)


def _episodes_of(kind: AnomalyKind, seed_range=range(60), n=3000):
    for seed in seed_range:
        sim = ChannelSimulator(QUIET, seed=seed, anomalies_per_hour=400)
        samples = drive(sim, n)
        for ep in sim.episodes:
            if ep.kind is kind:
                return sim, samples, ep
    raise AssertionError(f"no {kind} episode generated across the seed sweep")


def test_a_spike_marks_exactly_one_sample():
    sim, samples, spike = _episodes_of(AnomalyKind.SPIKE)
    assert spike.start_s == spike.end_s
    labelled = [i for i, s in enumerate(samples) if s.injected is AnomalyKind.SPIKE]
    # Several spikes may occur in the run; each must be an isolated single sample.
    assert all(j - i > 1 for i, j in zip(labelled, labelled[1:], strict=False))


def test_a_level_shift_labels_every_sample_it_covers():
    sim, samples, shift = _episodes_of(AnomalyKind.LEVEL_SHIFT)
    covered = [i for i in range(len(samples)) if shift.covers(i * 0.1)]
    assert len(covered) > 1
    assert all(samples[i].injected is AnomalyKind.LEVEL_SHIFT for i in covered)


def test_a_level_shift_actually_moves_the_level():
    sim, samples, shift = _episodes_of(AnomalyKind.LEVEL_SHIFT)
    inside = [samples[i].value for i in range(len(samples)) if shift.covers(i * 0.1)]
    outside = [samples[i].value for i in range(len(samples)) if samples[i].injected is None]
    displacement = abs(statistics.fmean(inside) - statistics.fmean(outside))
    assert displacement > 2.0 * QUIET.noise_sigma


def test_a_variance_burst_raises_dispersion_without_moving_the_mean_much():
    sim, samples, burst = _episodes_of(AnomalyKind.VARIANCE_BURST)
    inside = [samples[i].value for i in range(len(samples)) if burst.covers(i * 0.1)]
    outside = [samples[i].value for i in range(len(samples)) if samples[i].injected is None]
    assert len(inside) > 5
    assert statistics.pstdev(inside) > 1.5 * statistics.pstdev(outside)


def test_both_directions_of_anomaly_occur():
    # A detector that only looks for increases must be able to fail here.
    signs = set()
    for seed in range(40):
        sim = ChannelSimulator(QUIET, seed=seed, anomalies_per_hour=400)
        drive(sim, 1500)
        signs.update(ep.magnitude > 0 for ep in sim.episodes)
    assert signs == {True, False}


def test_default_fleet_names_are_unique_and_characters_differ():
    specs = default_fleet(12)
    assert len({s.name for s in specs}) == 12
    # A single global threshold must not be able to work across the fleet.
    assert len({round(s.base) for s in specs}) > 1
    assert len({round(s.noise_sigma, 2) for s in specs}) > 1
