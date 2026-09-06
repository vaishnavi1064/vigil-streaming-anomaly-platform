# Pipeline disturbances and deploy artifacts

Excursions whose cause is the data path or a change to it, not the plant. Most of these
should never reach an operator at all -- conditioning attributes them before they are
raised -- so an episode arriving here with pipeline or deploy context attached means the
attribution was declined, and the first question is why.

## Excursion coinciding with a pipeline disturbance

The reconciliation harness reported missing readings, duplicates, or reordering in the same
window as the excursion. Lost readings distort a window's mean and dispersion directly: a
window that should hold three hundred samples and holds two hundred is measuring a different
thing from its neighbours.

Check the health record's counts before concluding. A window graded as disturbed purely on
lag has lost nothing -- lag delays a reading, it does not change it -- and cannot explain a
value excursion. That distinction is why the conditioning policy requires an actual
mechanism rather than mere overlap.

If readings were genuinely lost, the excursion is an artifact. Annotate it and investigate
the pipeline, not the plant. If nothing was lost, the coincidence is a coincidence and the
excursion should be handled on its own merits.

licensed-actions: fetch_pipeline_health, describe_channel, annotate_episode, raise_ticket

## Excursion during a deploy window

A change was in flight over the same window. Whether it explains the excursion depends on
scope and corroboration, not on timing alone.

A deploy that perturbs telemetry perturbs the channels it touched -- a collector restart
blips the whole batch it serves, a config change shifts the whole fleet it applied to. So
the question is whether this channel moved alone or with its siblings. Alone, during a
deploy, is still a real fault: a bearing does not fail because a collector was redeployed.

If the channel is not in the deploy's scope, the deploy is irrelevant to it regardless of
timing.

licensed-actions: describe_channel, fetch_recent_readings, annotate_episode, raise_ticket

## Fleet-wide excursion at the same instant

Every channel moves together, sharply, with no deploy or pipeline event to explain it. Treat
this as a pipeline problem until proven otherwise: a genuine physical event that reached
every asset simultaneously is far rarer than a collector restart or a clock change.

Check the pipeline health signal first, then the deploy markers. If neither accounts for it,
escalate -- a genuinely fleet-wide physical event is beyond what automated remediation
should be attempting.

licensed-actions: fetch_pipeline_health, describe_channel, escalate_to_human

## Post-deploy settling

Elevated variance for a few minutes after a rollout, decaying back to normal without
intervention. This is a change settling, not a fault, and the correct action is to wait.

Do not silence channels to ride out a deploy. A silenced channel is silent for real problems
too, and a deploy window is a period of elevated risk rather than reduced risk.

licensed-actions: annotate_episode

## Sunset or expected-versus-actual collapse on a solar channel

Generation falling to zero across the fleet in the evening is not an anomaly. The platform
detects on the residual between expected and actual power for exactly this reason, so an
excursion in the residual is meaningful while a collapse in raw power at dusk is not.

An episode raised on a raw-power channel at nightfall indicates the wrong channel is being
watched, not a plant problem.

licensed-actions: describe_channel, annotate_episode
