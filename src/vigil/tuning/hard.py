"""The hard cases: episodes where the deterministic planner is wrong, not merely quiet.

B-3 measured the first training set and found its ceiling: five distinct targets, each a
function of a symptom that `Diagnoser` computes *before* the prompt is rendered and then puts
in the prompt. A model trained on that learns a five-way classification whose answer is one
of its own inputs, so it can approach the rules and never beat them.

This module removes both crutches.

**The answer is no longer in the prompt.** The hard prompt carries the per-window detector
evidence -- the mean and dispersion terms, window by window -- and no symptom label and no
diagnosis summary. Inferring what the signal did is now the model's job, which is what the
first set skipped.

**The rules get these wrong.** `Diagnoser` has no fall-through: every episode becomes one of
level shift, variance burst, isolated spike, silent channel or safety channel. So an episode
whose shape is outside that set is not abstained on, it is *misclassified*, and the planner
then confidently proposes the action for the wrong symptom. A stuck sensor reads as a
variance burst and gets silenced -- which hides a dead instrument behind a suppressed alarm,
the worst available outcome. That is a case a reader of the evidence can get right and the
rules cannot.

**Some licences are prose.** The production runbooks end each passage with a machine-readable
`licensed-actions:` line, and the planner intersects its proposals with it. Half the hard
passages state their permitted actions in ordinary sentences instead, so that line is absent
and the planner -- correctly, per ADR-030 -- can only escalate. The licence set is retained
as *ground truth for scoring*, never shown to the model: the evaluator knows the answer, the
model has to read for it.

**What the ceiling is now, stated plainly.** The targets come from a teacher policy written
here (`TEACHER`), not from the deployed planner, so this is still distillation -- of a policy
the deployed system does not have rather than of one it does. The model can reach the teacher
and not exceed it. What is genuinely new is that the *deployed* baseline is measurably below
the teacher on these cases, so "the fine-tune beats the planner" becomes a question with a
real answer instead of an arithmetic impossibility.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import StrEnum

from vigil.agent.actions import Action, ActionKind
from vigil.agent.runbooks import Passage
from vigil.episodes import Episode, EpisodeStatus, ScoreSample


class HardSymptom(StrEnum):
    """Shapes outside `Symptom`, chosen because the rules mishandle each in a specific way."""

    STUCK_SENSOR = "stuck_sensor"
    OSCILLATION = "oscillation"
    COUNTER_ROLLOVER = "counter_rollover"
    CORRELATED_STEP = "correlated_step"
    SLOW_DRIFT = "slow_drift"


@dataclass(frozen=True)
class TeacherPolicy:
    """What the correct plan is for a hard symptom, and why.

    `why` is not decoration: it is the justification an operator would want, and writing one
    per symptom is what keeps this a policy rather than a lookup table someone invented to
    make the numbers work. `rules_do` records what the deterministic planner does instead,
    which is what the evaluation compares against.
    """

    wanted: tuple[ActionKind, ...]
    forbidden: tuple[ActionKind, ...]
    why: str
    rules_do: str


# The teacher. Each entry is a claim about operations that can be argued with, which is the
# point -- an ADR-worthy decision rather than an arbitrary mapping.
TEACHER: dict[HardSymptom, TeacherPolicy] = {
    HardSymptom.STUCK_SENSOR: TeacherPolicy(
        wanted=(
            ActionKind.DESCRIBE_CHANNEL,
            ActionKind.RAISE_TICKET,
            ActionKind.ESCALATE_TO_HUMAN,
        ),
        forbidden=(ActionKind.SILENCE_CHANNEL, ActionKind.REQUEST_RECALIBRATION),
        why=(
            "a channel reporting a constant value is not noisy, it has stopped measuring. "
            "Silencing it hides a dead instrument behind a suppressed alarm, and "
            "recalibrating a sensor that is not reading anything calibrates nothing"
        ),
        rules_do=(
            "dispersion collapses toward zero, which the dispersion term scores as strongly "
            "as a burst would, so the rules read it as a variance burst and silence the "
            "channel -- the single most harmful action available here"
        ),
    ),
    HardSymptom.OSCILLATION: TeacherPolicy(
        wanted=(
            ActionKind.DESCRIBE_CHANNEL,
            ActionKind.FETCH_RECENT_READINGS,
            ActionKind.RAISE_TICKET,
        ),
        forbidden=(ActionKind.SILENCE_CHANNEL,),
        why=(
            "a regular swing at a fixed period is a control loop hunting, not sensor noise. "
            "The instrument is telling the truth and the plant is misbehaving, so the record "
            "belongs with whoever owns the loop and the channel must keep reporting"
        ),
        rules_do=(
            "dispersion dominates, so the rules call it a variance burst and silence the "
            "channel for thirty minutes -- exactly while the loop is unstable"
        ),
    ),
    HardSymptom.COUNTER_ROLLOVER: TeacherPolicy(
        wanted=(ActionKind.DESCRIBE_CHANNEL, ActionKind.ANNOTATE_EPISODE),
        forbidden=(
            ActionKind.REQUEST_RECALIBRATION,
            ActionKind.SILENCE_CHANNEL,
            ActionKind.RAISE_TICKET,
        ),
        why=(
            "a monotonic counter returning to zero is the counter working. It needs recording "
            "so the next reader is not surprised, and nothing else -- a ticket for a rollover "
            "is a ticket someone has to close"
        ),
        rules_do=(
            "the drop reads as a large mean shift, so the rules call it a level shift and "
            "request recalibration of an instrument that is behaving correctly"
        ),
    ),
    HardSymptom.CORRELATED_STEP: TeacherPolicy(
        wanted=(
            ActionKind.DESCRIBE_CHANNEL,
            ActionKind.FETCH_PIPELINE_HEALTH,
            ActionKind.ESCALATE_TO_HUMAN,
        ),
        forbidden=(ActionKind.REQUEST_RECALIBRATION, ActionKind.SILENCE_CHANNEL),
        why=(
            "when unrelated channels step by the same amount at the same instant, the common "
            "cause is upstream -- a collector, a unit conversion, a clock -- not four "
            "instruments drifting in unison. The pipeline is the first thing to look at and a "
            "human decides the rest"
        ),
        rules_do=(
            "the rules see one channel at a time and have no notion of a sibling, so they "
            "request recalibration of each instrument independently"
        ),
    ),
    HardSymptom.SLOW_DRIFT: TeacherPolicy(
        wanted=(
            ActionKind.DESCRIBE_CHANNEL,
            ActionKind.FETCH_RECENT_READINGS,
            ActionKind.REQUEST_RECALIBRATION,
            ActionKind.RAISE_TICKET,
        ),
        forbidden=(ActionKind.SILENCE_CHANNEL,),
        why=(
            "a slow monotonic creep with calm siblings is instrument drift, and the evidence "
            "for it is the trend over hours rather than the excursion in one window -- so the "
            "history is fetched before the recalibration is asked for"
        ),
        rules_do=(
            "the rules reach the same conclusion here by coincidence, which is why this "
            "symptom is in the set: a model that has learned to answer 'not the rules' "
            "rather than to read the evidence will get this one wrong"
        ),
    ),
}

# Channel vocabularies. TRAIN and HELD_OUT never overlap, so a model that memorised a name
# instead of learning to read the evidence fails the held-out split by construction.
TRAIN_UNITS = [f"pump-{i:02d}" for i in range(8)] + [f"fan-{i:02d}" for i in range(4)]
TRAIN_METRICS = [
    "bearing_temp_c",
    "vibration_mm_s",
    "discharge_pressure_bar",
    "flow_m3_h",
    "winding_temp_c",
]
HELD_OUT_UNITS = [f"chiller-{c}" for c in "ABCD"] + [f"turbine-{i:02d}" for i in range(3)]
HELD_OUT_METRICS = [
    "condenser_approach_k",
    "lube_oil_particle_count",
    "blade_tip_clearance_um",
    "exhaust_gas_spread_c",
    "generator_slip_ppm",
]


@dataclass
class HardCase:
    """One episode the rules mishandle, with everything needed to score an answer.

    `licensed` is ground truth for the scorer and is deliberately absent from the prompt: the
    whole point of the prose passages is that the model has to find the licence by reading.
    """

    episode_id: int
    episode: Episode
    symptom: HardSymptom
    passages: list[Passage]
    # What *any* retrieved passage permits. Grounding is scored against this, because
    # proposing something a retrieved passage allows is grounded even when it is the wrong
    # passage -- being grounded and being right are different failures.
    licensed: tuple[str, ...]
    # What the passage matching the evidence permits. The teacher's target comes from here,
    # so a model that grounds itself in a distractor is wrong in a way the score can see.
    answer_licences: tuple[str, ...]
    teacher_actions: list[Action]
    prose_licences: bool
    split: str
    novel_vocabulary: bool


# ------------------------- evidence -------------------------
# The per-window numbers a reader can infer the shape from. These are the detector's own two
# terms, which is all the agent ever sees, so nothing here is information the deployed system
# would not have -- it is the same evidence, minus the label the first dataset handed over.


def _evidence(symptom: HardSymptom, windows: int, rng: random.Random) -> list[dict[str, float]]:
    out: list[dict[str, float]] = []
    for i in range(windows):
        if symptom is HardSymptom.STUCK_SENSOR:
            # Dispersion collapses: the ratio goes to zero, so |ratio - 1| is near its
            # maximum. Indistinguishable from a burst unless you notice the sign.
            out.append(
                {
                    "mean_z": rng.uniform(0.3, 2.0),
                    "dispersion_z": rng.uniform(22.0, 40.0),
                    "dispersion_ratio": rng.uniform(0.0, 0.02),
                    "window_mean": 61.4,
                    "window_sigma": rng.uniform(0.0, 0.004),
                }
            )
        elif symptom is HardSymptom.OSCILLATION:
            out.append(
                {
                    "mean_z": rng.uniform(0.5, 3.0),
                    "dispersion_z": rng.uniform(18.0, 35.0),
                    "dispersion_ratio": rng.uniform(3.5, 7.0),
                    "window_mean": 61.4 + rng.uniform(-0.2, 0.2),
                    "window_sigma": rng.uniform(3.0, 6.0),
                    "zero_crossings": float(rng.randint(8, 14)),
                }
            )
        elif symptom is HardSymptom.COUNTER_ROLLOVER:
            # One window carries the drop; the rest are calm on both terms.
            dropped = i == windows // 2
            out.append(
                {
                    "mean_z": rng.uniform(40.0, 90.0) if dropped else rng.uniform(0.2, 1.5),
                    "dispersion_z": rng.uniform(2.0, 6.0) if dropped else rng.uniform(0.1, 1.0),
                    "dispersion_ratio": 1.0 + (rng.uniform(1.0, 3.0) if dropped else 0.0),
                    "window_mean": 12.0 if dropped else rng.uniform(64000.0, 65500.0),
                    "monotonic_before_drop": 1.0,
                }
            )
        elif symptom is HardSymptom.CORRELATED_STEP:
            out.append(
                {
                    "mean_z": rng.uniform(25.0, 60.0),
                    "dispersion_z": rng.uniform(0.5, 2.5),
                    "dispersion_ratio": rng.uniform(0.9, 1.1),
                    "window_mean": 61.4 + 4.0,
                    "siblings_stepped_within_1s": float(rng.randint(3, 7)),
                    "step_size_matches_siblings": 1.0,
                }
            )
        else:  # SLOW_DRIFT
            out.append(
                {
                    "mean_z": 9.0 + i * rng.uniform(1.5, 3.0),
                    "dispersion_z": rng.uniform(0.2, 1.5),
                    "dispersion_ratio": rng.uniform(0.95, 1.05),
                    "window_mean": 61.4 + i * rng.uniform(0.05, 0.15),
                    "siblings_stepped_within_1s": 0.0,
                }
            )
    return out


def _episode(
    episode_id: int, channel: str, symptom: HardSymptom, rng: random.Random
) -> tuple[Episode, list[dict[str, float]]]:
    windows = rng.randint(5, 14)
    detail = _evidence(symptom, windows, rng)
    peak = max(max(d["mean_z"], d["dispersion_z"]) for d in detail)
    start = rng.randrange(1_000_000, 9_000_000)
    scores = [
        ScoreSample(
            detector="zscore",
            window_start_ms=start + i * 10_000,
            window_end_ms=start + i * 10_000 + 30_000,
            score=max(d["mean_z"], d["dispersion_z"]),
            latency_ms=0.2,
            detail=dict(d),
            onset_ms=start + i * 10_000 + rng.randrange(200, 9_800),
        )
        for i, d in enumerate(detail)
    ]
    episode = Episode(
        channel=channel,
        t_start_ms=start,
        t_end_ms=start + windows * 10_000 + 30_000,
        raised_by="zscore",
        peak_score=peak,
        window_count=windows,
        threshold=8.0,
        onset_ms=scores[0].onset_ms,
        scores=scores,
        status=EpisodeStatus.REAL,
    )
    return episode, detail


# ------------------------- passages -------------------------

_PROSE_TEMPLATES_TRAIN = (
    "Where this pattern is confirmed, {phrase} is appropriate. Nothing further should be done "
    "without an owner.",
    "Response: {phrase}. Anything beyond that needs a human in the loop.",
    "On seeing this, the on-call engineer {phrase_verb}, and no other change is authorised.",
)
_PROSE_TEMPLATES_HELD_OUT = (
    "Handling: it is permitted to {phrase_bare}, and that is the whole of the authorised response.",
    "The standing agreement with operations is that {phrase} may be carried out. No other "
    "action is covered by this entry.",
)

_ACTION_PHRASES = {
    "describe_channel": ("describing the channel's normal behaviour", "describe the channel"),
    "fetch_recent_readings": ("pulling the recent history", "pull the recent history"),
    "fetch_pipeline_health": (
        "checking the ingestion pipeline's health for the window",
        "check the ingestion pipeline's health",
    ),
    "annotate_episode": ("recording a note against the episode", "record a note on the episode"),
    "raise_ticket": ("raising a ticket against the owning team", "raise a ticket"),
    "silence_channel": ("silencing the channel", "silence the channel"),
    "request_recalibration": (
        "requesting recalibration of the instrument",
        "request recalibration",
    ),
    "escalate_to_human": ("handing the episode to a human", "hand the episode to a human"),
}


def _phrase(licences: tuple[str, ...]) -> tuple[str, str]:
    """Turn a licence set into a sentence fragment and its imperative form."""
    gerunds = [_ACTION_PHRASES[name][0] for name in licences if name in _ACTION_PHRASES]
    bares = [_ACTION_PHRASES[name][1] for name in licences if name in _ACTION_PHRASES]

    def join(parts: list[str]) -> str:
        if not parts:
            return "no action"
        if len(parts) == 1:
            return parts[0]
        return ", ".join(parts[:-1]) + " and " + parts[-1]

    return join(gerunds), join(bares)


def hard_passage(
    symptom: HardSymptom,
    licences: tuple[str, ...],
    prose: bool,
    rng: random.Random | None = None,
    held_out: bool = False,
) -> Passage:
    """A runbook entry for a pattern the production runbooks do not cover.

    Written here rather than added to `runbooks/` on purpose: those documents feed the live
    agent, and teaching the deployed planner these patterns would remove the very gap this
    dataset exists to measure.

    The body describes **what the pattern looks like in the data**, because that is what lets
    a reader match an entry to the evidence in front of them, and matching is the task. What
    the body deliberately does *not* contain: the symptom's internal name, anything about how
    the deterministic planner behaves, or any commentary on this dataset. An earlier version
    leaked all three, which told the model about the experiment it was part of.
    """
    why = TEACHER[symptom].why
    why = why[0].upper() + why[1:]
    body = f"{_PATTERNS[symptom]}\n\n{why}."
    if not prose:
        licence_text = f"licensed-actions: {', '.join(licences)}"
    else:
        gerund, bare = _phrase(licences)
        pool = _PROSE_TEMPLATES_HELD_OUT if held_out else _PROSE_TEMPLATES_TRAIN
        template = (rng or random.Random(0)).choice(pool)
        licence_text = template.format(
            phrase=gerund, phrase_bare=bare, phrase_verb=f"should begin by {gerund}"
        )
    return Passage(
        runbook=_RUNBOOKS[symptom],
        title=_TITLES[symptom],
        text=f"{body}\n\n{licence_text}",
        licenses=licences,
    )


# How each pattern presents in the detector's own terms. This is the bridge between the
# evidence table in the prompt and the entry that covers it, and writing it is what makes the
# retrieval decision answerable from the data rather than from the entry's title.
_PATTERNS = {
    HardSymptom.STUCK_SENSOR: (
        "Presents as a large dispersion term with a dispersion ratio near zero and a window "
        "sigma of effectively nothing, while the mean sits where it always has. The channel "
        "has not become noisy; it has stopped varying at all."
    ),
    HardSymptom.OSCILLATION: (
        "Presents as a large dispersion term with a dispersion ratio well above one, a stable "
        "window mean, and repeated crossings of that mean within every window. The spread is "
        "real movement at a regular period rather than measurement noise."
    ),
    HardSymptom.COUNTER_ROLLOVER: (
        "Presents as one window with a very large mean term and calm windows either side, "
        "where the window mean drops from a high value to near zero after having risen "
        "monotonically. The dispersion term stays small throughout."
    ),
    HardSymptom.CORRELATED_STEP: (
        "Presents as a large mean term with a dispersion ratio near one -- a clean step, not "
        "a noisier signal -- accompanied by several sibling channels stepping by a matching "
        "amount within the same second."
    ),
    HardSymptom.SLOW_DRIFT: (
        "Presents as a mean term that grows window over window from just above the threshold, "
        "with a dispersion ratio near one and no sibling channel moving at all. The window "
        "mean creeps rather than steps."
    ),
}


_TITLES = {
    HardSymptom.STUCK_SENSOR: "Channel reporting a constant value",
    HardSymptom.OSCILLATION: "Regular oscillation at a fixed period",
    HardSymptom.COUNTER_ROLLOVER: "Monotonic counter returning to zero",
    HardSymptom.CORRELATED_STEP: "Several channels stepping by the same amount at once",
    HardSymptom.SLOW_DRIFT: "Slow monotonic creep with calm siblings",
}

# Which document each entry lives in. Grouped by operational topic and never one file per
# symptom: a filename of `hard-stuck_sensor.md` would hand the model the label the prompt
# withholds, which is the crutch this whole set exists to remove. The first version did
# exactly that and a test caught it.
_RUNBOOKS = {
    HardSymptom.STUCK_SENSOR: "instrument-behaviour.md",
    HardSymptom.COUNTER_ROLLOVER: "instrument-behaviour.md",
    HardSymptom.SLOW_DRIFT: "instrument-behaviour.md",
    HardSymptom.OSCILLATION: "plant-and-pipeline.md",
    HardSymptom.CORRELATED_STEP: "plant-and-pipeline.md",
}


# ------------------------- the teacher's plan -------------------------


def teacher_actions(
    symptom: HardSymptom, episode_id: int, channel: str, licensed: tuple[str, ...]
) -> list[Action]:
    """The correct plan, intersected with what the retrieved passage licenses.

    Intersected rather than asserted: a target that proposes an action no passage permits
    would teach exactly the ungrounded behaviour retrieval exists to prevent, however right
    the action is on the merits.
    """
    policy = TEACHER[symptom]
    reason = f"{symptom}: {policy.why}"
    out: list[Action] = []
    for kind in policy.wanted:
        if str(kind) not in licensed:
            continue
        out.append(
            Action(kind=kind, parameters=_parameters(kind, episode_id, channel), rationale=reason)
        )
    if not out:
        out.append(
            Action(
                kind=ActionKind.ESCALATE_TO_HUMAN,
                parameters={"episode_id": episode_id, "reason": reason},
                rationale="nothing retrieved licenses an applicable action",
            )
        )
    return out


def _parameters(kind: ActionKind, episode_id: int, channel: str) -> dict:
    if kind is ActionKind.DESCRIBE_CHANNEL:
        return {"channel": channel}
    if kind is ActionKind.FETCH_RECENT_READINGS:
        return {"channel": channel, "minutes": 240}
    if kind is ActionKind.FETCH_PIPELINE_HEALTH:
        return {"window_start_ms": 0}
    if kind is ActionKind.ANNOTATE_EPISODE:
        return {"episode_id": episode_id, "note": "expected pattern, recorded"}
    if kind is ActionKind.RAISE_TICKET:
        return {
            "episode_id": episode_id,
            "summary": f"{channel} needs an owner",
            "severity": "medium",
        }
    if kind is ActionKind.SILENCE_CHANNEL:
        return {"channel": channel, "minutes": 30, "reason": "no usable signal"}
    if kind is ActionKind.REQUEST_RECALIBRATION:
        return {"channel": channel, "reason": "instrument drift"}
    return {"episode_id": episode_id, "reason": "handing to a human"}


# ------------------------- generation -------------------------

# Which licences each symptom's runbook entry grants. Deliberately not equal to the teacher's
# wanted set in every case: where a licence is missing the correct target is shorter, which
# is what stops a model from learning "always emit four actions".
LICENCES: dict[HardSymptom, tuple[str, ...]] = {
    HardSymptom.STUCK_SENSOR: ("describe_channel", "escalate_to_human", "raise_ticket"),
    HardSymptom.OSCILLATION: (
        "describe_channel",
        "fetch_recent_readings",
        "raise_ticket",
        "silence_channel",
    ),
    HardSymptom.COUNTER_ROLLOVER: ("annotate_episode", "describe_channel"),
    HardSymptom.CORRELATED_STEP: (
        "describe_channel",
        "escalate_to_human",
        "fetch_pipeline_health",
    ),
    HardSymptom.SLOW_DRIFT: (
        "describe_channel",
        "fetch_recent_readings",
        "raise_ticket",
        "request_recalibration",
    ),
}


def generate_hard_cases(
    count: int,
    seed: int = 20260907,
    split: str = "train",
    prose_fraction: float = 0.5,
    distractor_count: int = 2,
) -> list[HardCase]:
    """Generate hard cases for one split.

    The splits differ in three independent ways, so passing the held-out set requires reading
    rather than recall: the channel vocabulary is disjoint, the prose templates are disjoint,
    and the held-out split is entirely prose (no machine-readable licence line anywhere).
    """
    rng = random.Random(seed if split == "train" else seed + 977)
    held_out = split != "train"
    units = HELD_OUT_UNITS if held_out else TRAIN_UNITS
    metrics = HELD_OUT_METRICS if held_out else TRAIN_METRICS
    symptoms = list(HardSymptom)

    cases: list[HardCase] = []
    for i in range(count):
        symptom = symptoms[i % len(symptoms)]
        channel = f"{rng.choice(units)}.{rng.choice(metrics)}"
        prose = True if held_out else rng.random() < prose_fraction
        licences = LICENCES[symptom]
        episode, _ = _episode(i + 1, channel, symptom, rng)

        # Retrieval returns several entries, so the model has to work out which one the
        # evidence matches before it can read a licence off it. With a single passage the
        # task collapses to "read the only entry you were given", which needs no inference
        # at all -- and the whole point of this set is that inference is required.
        distractors = [other for other in symptoms if other is not symptom]
        rng.shuffle(distractors)
        chosen = [symptom, *distractors[:distractor_count]]
        passages = []
        for entry in chosen:
            passages.append(
                hard_passage(entry, LICENCES[entry], prose=prose, rng=rng, held_out=held_out)
            )
        rng.shuffle(passages)
        union = tuple(sorted({name for p in passages for name in p.licenses}))

        cases.append(
            HardCase(
                episode_id=i + 1,
                episode=episode,
                symptom=symptom,
                passages=passages,
                licensed=union,
                answer_licences=licences,
                teacher_actions=teacher_actions(symptom, i + 1, channel, licences),
                prose_licences=prose,
                split=split,
                novel_vocabulary=held_out,
            )
        )
    return cases


# ------------------------- the prompt -------------------------


def render_hard_prompt(case: HardCase, max_windows: int = 8) -> str:
    """The user turn for a hard case: evidence and passages, no label.

    Deliberately absent: the symptom, the diagnosis summary, and the licence list. Those three
    were what made the first dataset's task trivial (B-3). What is present is exactly what the
    platform recorded.
    """
    episode = case.episode
    lines = [
        "EPISODE",
        f"  id: {case.episode_id}",
        f"  channel: {episode.channel}",
        f"  status: {episode.status}",
        f"  windows: {episode.window_count}",
        f"  duration_s: {episode.duration_ms / 1000:.0f}",
        f"  peak_score: {episode.peak_score:.1f} (threshold {episode.threshold:.1f})",
        "",
        "PER-WINDOW DETECTOR EVIDENCE",
    ]
    for sample in episode.scores[:max_windows]:
        detail = sample.detail
        parts = [f"{key}={value:.3g}" for key, value in sorted(detail.items())]
        lines.append(f"  w{sample.window_start_ms}: " + " ".join(parts))
    if episode.window_count > max_windows:
        lines.append(f"  ... {episode.window_count - max_windows} further windows")

    lines += ["", "RETRIEVED RUNBOOK PASSAGES"]
    if not case.passages:
        lines.append("  (none matched)")
    for passage in case.passages:
        lines.append(f"  - {passage.runbook}: {passage.title}")
        for line in passage.text.splitlines():
            if line.strip():
                lines.append(f"    {line.strip()}")
    return "\n".join(lines)


HARD_SYSTEM_PROMPT = """You are the planning step of an operational anomaly-remediation \
agent.

You are given one detected episode, the detector's per-window evidence, and the runbook \
passages retrieved for it. Reply with a JSON array of actions and nothing else -- no prose, \
no markdown fence, no explanation outside the array.

Each action is an object: {"kind": <verb>, "parameters": {...}, "rationale": <short string>}

Permitted verbs and their required parameters:
  describe_channel        channel
  fetch_recent_readings   channel, minutes
  fetch_pipeline_health   window_start_ms
  search_runbooks         query
  annotate_episode        episode_id, note
  raise_ticket            episode_id, summary, severity   (severity: low | medium | high)
  silence_channel         channel, minutes, reason
  request_recalibration   channel, reason
  escalate_to_human       episode_id, reason

How to decide:
  - Work out from the evidence what the signal actually did. The evidence is the detector's \
own terms: mean_z is how far the window's mean sits from the channel's reference, \
dispersion_z is how far its spread does, and dispersion_ratio below 1 means the signal got \
*flatter* while above 1 means noisier. Nothing tells you the answer directly.
  - The retrieved passages say what you are allowed to do. They may state it as a list or in \
ordinary sentences; either way, propose only actions the passages permit.
  - If nothing retrieved permits an applicable action, reply with a single escalate_to_human.
  - Never propose anything but escalate_to_human and describe_channel for a channel whose \
name contains safety, interlock, fire, or emergency.
  - Gather evidence before changing state, and every action must concern this episode's own \
channel and id."""


@dataclass
class HardSet:
    """A generated pair of splits, kept together so a caller cannot mix them up."""

    train: list[HardCase] = field(default_factory=list)
    test: list[HardCase] = field(default_factory=list)

    def summary(self) -> str:
        def describe(cases: list[HardCase]) -> str:
            prose = sum(1 for c in cases if c.prose_licences)
            symptoms = len({c.symptom for c in cases})
            channels = len({c.episode.channel for c in cases})
            return (
                f"{len(cases):>4} cases | {prose:>4} prose-licence | {symptoms} symptoms | "
                f"{channels} channels"
            )

        shared = {c.episode.channel for c in self.train} & {c.episode.channel for c in self.test}
        return (
            f"  train {describe(self.train)}\n"
            f"  test  {describe(self.test)}\n"
            f"  channels shared between splits: {len(shared)} (must be 0)"
        )
