# Sensor and instrument faults

Operational guidance for excursions that originate at the instrument rather than in the
plant. Each section names the actions it licenses; the agent may propose only those, and the
safety gate still has to approve them.

## Sustained level shift on a single channel

A step change in a channel's mean that persists across several windows, with the channel's
neighbours unaffected, is characteristic of an instrument problem rather than a process
problem: a genuine process change moves correlated measurements together.

Typical causes are a drifted calibration, a loosened mount, or a replaced transducer whose
scaling was not updated. Bearing temperature and discharge pressure are the most frequent
offenders because both are sensitive to mounting.

Confirm before acting. Compare the channel against its siblings on the same unit over the
same window. If the siblings are calm, treat it as an instrument fault. If they moved
together, it is a process event and this section does not apply.

Do not silence the channel to make the alert stop. A drifted instrument still reports real
excursions on top of its offset, and silencing it removes the only signal available from
that point on the plant.

licensed-actions: describe_channel, fetch_recent_readings, request_recalibration, raise_ticket, annotate_episode

## Variance burst with a stable mean

Dispersion increases sharply while the mean stays put. This is usually electrical: a failing
signal conditioner, a loose earth, or interference from equipment switched on nearby. It is
rarely the process, because a process that became genuinely unstable would move its mean as
well.

Check whether the burst coincides with a shift change or scheduled equipment start. Repeated
bursts at the same time of day point at a neighbouring load rather than the instrument.

A short silence is acceptable here while an electrician is dispatched, because the channel
is not carrying usable information during the burst anyway. Keep it under an hour, and raise
a ticket in the same breath so the silence has an owner.

licensed-actions: describe_channel, fetch_recent_readings, silence_channel, raise_ticket, annotate_episode

## Isolated spike, single sample

One sample far from the distribution, with the samples either side normal. Almost always a
transmission artifact rather than a physical event: a genuine physical excursion has a rise
time and would touch neighbouring samples.

No remediation is warranted for a single spike. Annotate the episode so the pattern is
visible if it recurs, and do nothing else. Repeated isolated spikes on the same channel are
a different matter and belong under the level-shift section.

licensed-actions: describe_channel, annotate_episode

## Channel silent or intermittently reporting

Readings stop arriving, or arrive with gaps that the ingestion layer flags. Distinguish this
from a value anomaly: the instrument is not reporting a wrong value, it is not reporting.

Check the pipeline health signal for the same window first. Fleet-wide silence is a pipeline
problem and is handled in the pipeline runbook, not here. Silence on one channel while its
siblings continue is the instrument or its link.

licensed-actions: describe_channel, fetch_pipeline_health, raise_ticket, escalate_to_human

## Excursion on a safety instrument

No automated action. Escalate immediately.

Safety instruments, interlocks, fire detection and emergency shutdown channels are excluded
from automated remediation entirely, and the safety gate refuses actions on them regardless
of the reasoning presented. This is deliberate: an excursion on a safety instrument is
exactly the situation where a plausible chain of inference arrives at silencing the thing
that is trying to tell you something.

licensed-actions: escalate_to_human, describe_channel
