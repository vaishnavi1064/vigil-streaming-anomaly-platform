"""Building and curating the tool-calling training set.

Four populations, and the mix is the design:

**1. Distilled.** Episodes the deterministic planner handles, with its output as the target.
This teaches the format and the common cases. On its own it would be pure rule distillation
and would not justify a fine-tune.

**2. Generalisation.** The same symptoms on channel names, units and metric vocabularies the
rules were never written against. The deterministic planner still produces a correct target
here -- its rules are symptom-based, not name-based -- but a model that has only memorised
surface forms will fail them. This is the population that answers "does the fine-tune buy
anything over the rules".

**3. Safety.** Protected channels, and prompts written to be persuasive about acting on
them. The target is always escalation. These are included because a model trained only on
the easy cases will happily be talked into the hard one, and because the gate refusing an
action is a worse outcome than the model never proposing it -- a refused proposal is a
wasted turn and a confusing trace.

**4. Abstention.** Episodes on an asset class the runbooks do not cover. The target is a
single escalation. A model that cannot say "I do not know" will invent, and invention is
exactly what runbook grounding exists to prevent.

This population has to be *constructed*, and it is worth being explicit about why. Every
symptom the diagnoser can name is covered by a runbook, and the diagnosis query shares
vocabulary with those passages, so no generated episode retrieves nothing by accident. A
fraction of episodes are therefore marked uncovered and retrieve against an empty index --
which is what a fleet looks like while a new asset class is onboarded and nobody has written
its runbook yet. The prompt the model sees is the real prompt for that condition, and the
target is the behaviour that condition calls for.

**Curation rules, applied to every example before it is written:**
- The target must parse to a valid plan (schema-valid, known verbs, required parameters).
- Every targeted action must be licensed by a retrieved passage, or the example teaches
  ungrounded behaviour.
- Every targeted action must be **approved by the real safety gate**. Training a model to
  propose something the gate rejects would teach it to waste turns.
- Duplicate prompts are dropped; near-duplicates within a symptom are capped, so a common
  case cannot swamp the set.

The held-out split is by **channel**, not by row. Splitting randomly would put the same
channel in both halves and let memorisation look like generalisation.
"""

from __future__ import annotations

import hashlib
import random
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from vigil.agent.actions import Action, ActionKind
from vigil.agent.gate import GatePolicy, SafetyGate
from vigil.agent.loop import SAFETY_MARKERS, Diagnoser, RunbookPlanner
from vigil.agent.runbooks import Retrieved, RunbookIndex, load_runbooks
from vigil.episodes import Episode, EpisodeStatus, ScoreSample
from vigil.tuning.schema import (
    TrainingExample,
    parse_plan,
    render_prompt,
    render_response,
)

# Channel vocabularies. `FAMILIAR` mirrors what the platform has actually seen; `NOVEL` is
# deliberately different in unit, naming convention and domain, and appears only in the
# held-out split.
FAMILIAR_UNITS = [f"pump-{i:02d}" for i in range(8)]
FAMILIAR_METRICS = [
    "bearing_temp_c",
    "vibration_mm_s",
    "discharge_pressure_bar",
    "flow_m3_h",
]

NOVEL_UNITS = [f"compressor-{c}" for c in "ABCDEF"] + [f"HX-{i:03d}" for i in range(4)]
NOVEL_METRICS = [
    "seal_gas_differential_kpa",
    "shaft_axial_position_um",
    "lube_oil_supply_temp_k",
    "stage2_suction_density_kgm3",
    "tube_side_fouling_index",
]

SAFETY_METRICS = [
    "safety_interlock_state",
    "fire_loop_continuity",
    "emergency_stop_circuit",
    "safety_valve_position_pct",
]

CONTEXT_SNIPPETS = [
    None,
    None,
    "deploy deploy-0042 in flight, scope 6 channels, this channel included",
    "pipeline window graded warning on lag only; missing=0 duplicates=0",
    "pipeline window missing=412 duplicates=0; readings lost in this window",
]


@dataclass
class GeneratedEpisode:
    episode: Episode
    episode_id: int
    context: str | None
    novel: bool
    # False means "no runbook has been written for this asset class yet", which is the only
    # condition under which retrieval legitimately returns nothing.
    covered: bool = True


def _episode(
    episode_id: int,
    channel: str,
    *,
    symptom: str,
    rng: random.Random,
    status: EpisodeStatus = EpisodeStatus.REAL,
) -> Episode:
    """Synthesise an episode with the shape a given symptom produces.

    Built rather than sampled from a live run because the training set needs coverage of
    combinations a 15-minute run never produces -- and because an episode's shape is fully
    determined by a handful of fields, so there is nothing to be gained by waiting for one.
    """
    if symptom == "isolated_spike":
        windows, duration_ms = 1, 30_000
        detail = {"mean_z": rng.uniform(30, 90), "dispersion_z": rng.uniform(1, 5)}
    elif symptom == "variance_burst":
        windows, duration_ms = rng.randint(4, 14), rng.randint(60_000, 240_000)
        detail = {"mean_z": rng.uniform(1, 6), "dispersion_z": rng.uniform(20, 80)}
    else:  # level_shift
        windows, duration_ms = rng.randint(5, 20), rng.randint(90_000, 300_000)
        detail = {"mean_z": rng.uniform(25, 120), "dispersion_z": rng.uniform(1, 8)}

    peak = max(detail["mean_z"], detail["dispersion_z"])
    return _finish(channel, windows, duration_ms, peak, detail, rng, status)


def _finish(channel, windows, duration_ms, peak, detail, rng, status) -> Episode:
    start = rng.randrange(1_000_000, 9_000_000)
    return Episode(
        channel=channel,
        t_start_ms=start,
        t_end_ms=start + duration_ms,
        raised_by="zscore",
        peak_score=peak,
        window_count=windows,
        threshold=8.0,
        scores=[
            ScoreSample("zscore", start, start + 30_000, peak, 0.2, dict(detail))
            for _ in range(windows)
        ],
        status=status,
    )


def generate_episodes(
    count: int, seed: int = 4242, uncovered_fraction: float = 0.10
) -> list[GeneratedEpisode]:
    rng = random.Random(seed)
    out: list[GeneratedEpisode] = []
    symptoms = ["level_shift", "variance_burst", "isolated_spike"]

    for i in range(count):
        roll = rng.random()
        if roll < 0.12:
            unit = rng.choice(FAMILIAR_UNITS + NOVEL_UNITS)
            channel = f"{unit}.{rng.choice(SAFETY_METRICS)}"
            novel = unit in NOVEL_UNITS
        elif roll < 0.45:
            channel = f"{rng.choice(NOVEL_UNITS)}.{rng.choice(NOVEL_METRICS)}"
            novel = True
        else:
            channel = f"{rng.choice(FAMILIAR_UNITS)}.{rng.choice(FAMILIAR_METRICS)}"
            novel = False

        symptom = rng.choice(symptoms)
        status = EpisodeStatus.REAL
        context = rng.choice(CONTEXT_SNIPPETS)
        out.append(
            GeneratedEpisode(
                episode=_episode(i + 1, channel, symptom=symptom, rng=rng, status=status),
                episode_id=i + 1,
                context=context,
                novel=novel,
                covered=rng.random() >= uncovered_fraction,
            )
        )
    return out


def _retrieve(
    runbooks: RunbookIndex,
    uncovered: RunbookIndex,
    query: str,
    covered: bool,
    limit: int,
) -> list[Retrieved]:
    """Retrieve for one episode, against an empty index when its asset class is uncovered."""
    return (runbooks if covered else uncovered).search(query, limit=limit)


def build_examples(
    generated: list[GeneratedEpisode],
    runbooks: RunbookIndex,
    *,
    retrieval_limit: int = 3,
) -> list[TrainingExample]:
    """Turn generated episodes into curated training examples."""
    diagnoser = Diagnoser()
    planner = RunbookPlanner()
    gate = SafetyGate(policy=GatePolicy())
    uncovered = RunbookIndex()
    examples: list[TrainingExample] = []

    for item in generated:
        episode, episode_id = item.episode, item.episode_id
        diagnosis = diagnoser.diagnose(episode)
        retrieved = _retrieve(runbooks, uncovered, diagnosis.query, item.covered, retrieval_limit)
        actions = planner.plan(episode, episode_id, diagnosis, retrieved)
        if not actions:
            continue

        licensed = tuple(sorted({n for hit in retrieved for n in hit.passage.licenses}))
        is_safety = any(m in episode.channel.lower() for m in SAFETY_MARKERS)

        if not _passes_curation(actions, licensed, episode, episode_id, gate, is_safety):
            continue

        prompt = render_prompt(
            episode_id=episode_id,
            channel=episode.channel,
            peak_score=episode.peak_score,
            window_count=episode.window_count,
            duration_s=episode.duration_ms / 1000.0,
            status=str(episode.status),
            diagnosis_summary=diagnosis.summary,
            passages=[(h.passage.runbook, h.passage.title, h.passage.licenses) for h in retrieved],
            context=item.context,
        )
        # Safety wins over every other label: an uncovered safety channel is still the
        # population that must never be talked into acting.
        if is_safety:
            source = "safety"
        elif not retrieved:
            source = "abstention"
        elif item.novel:
            source = "generalisation"
        else:
            source = "distilled"
        examples.append(
            TrainingExample(
                prompt=prompt,
                response=render_response(actions),
                episode_id=episode_id,
                channel=episode.channel,
                symptom=str(diagnosis.symptom),
                licensed=licensed,
                is_safety_channel=is_safety,
                source=source,
            )
        )

    return examples


def _passes_curation(
    actions: list[Action],
    licensed: tuple[str, ...],
    episode: Episode,
    episode_id: int,
    gate: SafetyGate,
    is_safety: bool,
) -> bool:
    """Every rule from the module docstring, applied before an example is kept."""
    parsed = parse_plan(render_response(actions))
    if not parsed.schema_valid:
        return False

    for action in actions:
        if licensed and str(action.kind) not in licensed:
            return False
        # Training a model to propose what the gate rejects teaches it to waste turns and
        # produces confusing traces.
        if not gate.verdict(action, episode, episode_id).approved:
            return False

    if is_safety:
        permitted = {ActionKind.ESCALATE_TO_HUMAN, ActionKind.DESCRIBE_CHANNEL}
        if not {a.kind for a in actions} <= permitted:
            return False
        if ActionKind.ESCALATE_TO_HUMAN not in {a.kind for a in actions}:
            return False

    return True


def split_by_channel(
    examples: list[TrainingExample], holdout_fraction: float = 0.2, seed: int = 7
) -> list[TrainingExample]:
    """Assign a split, holding out whole channels.

    By channel and not by row: a random row split would put the same channel in both halves,
    and a model that had memorised `compressor-B.shaft_axial_position_um` would look like it
    had generalised.
    """
    channels = sorted({e.channel for e in examples})
    rng = random.Random(seed)
    rng.shuffle(channels)
    holdout = set(channels[: max(1, int(len(channels) * holdout_fraction))])
    for example in examples:
        example.split = "test" if example.channel in holdout else "train"
    return examples


def deduplicate(
    examples: list[TrainingExample], max_per_symptom: int = 400
) -> list[TrainingExample]:
    """Drop exact duplicates and cap each symptom, so a common case cannot swamp the set."""
    seen: set[str] = set()
    per_symptom: Counter[str] = Counter()
    out: list[TrainingExample] = []
    for example in examples:
        digest = hashlib.sha256(example.prompt.encode()).hexdigest()
        if digest in seen:
            continue
        key = f"{example.split}:{example.symptom}"
        if per_symptom[key] >= max_per_symptom:
            continue
        seen.add(digest)
        per_symptom[key] += 1
        out.append(example)
    return out


def write_jsonl(examples: list[TrainingExample], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(e.to_jsonl() for e in examples) + "\n", encoding="utf-8")


def read_jsonl(path: Path) -> list[TrainingExample]:
    return [
        TrainingExample.from_jsonl(line)
        for line in Path(path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def summarise(examples: list[TrainingExample]) -> str:
    by_split: dict[str, Counter] = defaultdict(Counter)
    for example in examples:
        by_split[example.split]["total"] += 1
        by_split[example.split][example.source] += 1
        by_split[example.split][f"symptom:{example.symptom}"] += 1

    lines = []
    for split in sorted(by_split):
        counts = by_split[split]
        detail = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()) if k != "total")
        lines.append(f"  {split:<6} {counts['total']:>5} examples | {detail}")
    channels_train = {e.channel for e in examples if e.split == "train"}
    channels_test = {e.channel for e in examples if e.split == "test"}
    lines.append(
        f"  channels: {len(channels_train)} train, {len(channels_test)} held out, "
        f"{len(channels_train & channels_test)} shared (must be 0)"
    )
    return "\n".join(lines)


def build(
    runbook_dir: Path,
    count: int = 3000,
    seed: int = 4242,
    holdout_fraction: float = 0.2,
    uncovered_fraction: float = 0.10,
) -> list[TrainingExample]:
    runbooks = load_runbooks(runbook_dir)
    generated = generate_episodes(count, seed=seed, uncovered_fraction=uncovered_fraction)
    examples = build_examples(generated, runbooks)
    examples = split_by_channel(examples, holdout_fraction=holdout_fraction, seed=seed)
    return deduplicate(examples)


# ------------------------- the hard set (B-3) -------------------------


def build_hard(
    train_count: int = 900,
    test_count: int = 300,
    seed: int = 20260907,
) -> list[TrainingExample]:
    """The set a fine-tune can actually win on, curated by the same rules as the easy one.

    Distinct from `build` in what it withholds rather than in what it adds: the prompt has no
    symptom label and no diagnosis summary, and half the training passages (all of the
    held-out ones) state their licences in prose instead of a machine-readable line. The
    deployed planner scores 20% exact-match against these targets, so there is measurable
    room above the baseline -- which was the whole objection to the first set.

    The splits share no channel, no metric vocabulary and no prose phrasing template, so the
    held-out score measures reading rather than recall.
    """
    from vigil.tuning.hard import generate_hard_cases, render_hard_prompt

    examples: list[TrainingExample] = []
    for split, count in (("train", train_count), ("test", test_count)):
        for case in generate_hard_cases(count, seed=seed, split=split):
            actions = case.teacher_actions
            if not _passes_curation(
                actions,
                case.licensed,
                case.episode,
                case.episode_id,
                SafetyGate(policy=GatePolicy()),
                is_safety=False,
            ):
                continue
            examples.append(
                TrainingExample(
                    prompt=render_hard_prompt(case),
                    response=render_response(actions),
                    episode_id=case.episode_id,
                    channel=case.episode.channel,
                    symptom=str(case.symptom),
                    licensed=case.licensed,
                    is_safety_channel=False,
                    source=f"hard:{'prose' if case.prose_licences else 'enumerated'}",
                    split=split,
                    notes=(
                        f"novel_vocabulary={case.novel_vocabulary} "
                        f"prose_licences={case.prose_licences}"
                    ),
                )
            )
    return examples


def summarise_hard(examples: list[TrainingExample]) -> str:
    """Report the hard set by split, symptom and licence style.

    Reported per symptom because the interesting failure is uneven: a model can look strong
    overall while getting one shape consistently wrong, and the aggregate would hide it.
    """
    lines = []
    for split in ("train", "test"):
        rows = [e for e in examples if e.split == split]
        if not rows:
            continue
        symptoms = Counter(e.symptom for e in rows)
        styles = Counter(e.source for e in rows)
        lines.append(f"  {split:<6} {len(rows):>4} examples")
        detail = ", ".join(f"{k}={v}" for k, v in sorted(symptoms.items()))
        lines.append(f"         symptoms: {detail}")
        detail = ", ".join(f"{k}={v}" for k, v in sorted(styles.items()))
        lines.append(f"         licences: {detail}")
    train_channels = {e.channel for e in examples if e.split == "train"}
    test_channels = {e.channel for e in examples if e.split == "test"}
    lines.append(
        f"  channels: {len(train_channels)} train, {len(test_channels)} held out, "
        f"{len(train_channels & test_channels)} shared (must be 0)"
    )
    return "\n".join(lines)
