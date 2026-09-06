"""Tests for the tool-calling training set.

A fine-tune is only as trustworthy as the data behind it, and each way this set can be wrong
is a way the eval that follows becomes meaningless. An example the gate would reject teaches
the model to spend turns on proposals that will be refused. An example whose target is not
licensed by a retrieved passage teaches exactly the ungrounded behaviour runbook retrieval
exists to prevent. A channel appearing in both splits turns memorisation into an apparent
generalisation result -- the one number the fine-tune is meant to produce.

So this suite tests the curation contract and the split, not the code paths.
"""

from pathlib import Path

import pytest

from vigil.agent.actions import Action, ActionKind
from vigil.agent.gate import GatePolicy, SafetyGate
from vigil.agent.loop import SAFETY_MARKERS
from vigil.tuning.dataset import build, deduplicate, split_by_channel
from vigil.tuning.schema import (
    TrainingExample,
    has_fence,
    parse_plan,
    render_prompt,
    render_response,
)

REPO = Path(__file__).resolve().parent.parent
RUNBOOKS = REPO / "runbooks"


@pytest.fixture(scope="module")
def dataset():
    """One build, shared. 400 episodes is enough for every population to be populated."""
    return build(RUNBOOKS, count=400, seed=4242)


def an_action(kind=ActionKind.DESCRIBE_CHANNEL, **parameters):
    return Action(
        kind=kind, parameters=parameters or {"channel": "pump-01.bearing_temp_c"}, rationale="x"
    )


# ------------------------- parsing a model reply -------------------------
# Every one of these is a way a model fails in practice, and the eval reports each as its own
# rate. Collapsing them into "invalid" would hide which failure the fine-tune actually fixed.


def test_a_rendered_plan_parses_back_to_the_same_actions():
    actions = [
        an_action(),
        an_action(ActionKind.RAISE_TICKET, episode_id=7, summary="s", severity="low"),
    ]
    parsed = parse_plan(render_response(actions))

    assert parsed.schema_valid
    assert [a.kind for a in parsed.actions] == [a.kind for a in actions]
    assert parsed.actions[1].parameters["severity"] == "low"


def test_invalid_json_is_recorded_rather_than_raised():
    parsed = parse_plan("I would start by describing the channel.")

    assert not parsed.valid_json
    assert not parsed.schema_valid
    assert parsed.actions == []


def test_a_bare_object_is_valid_json_but_is_not_a_plan():
    parsed = parse_plan('{"kind":"describe_channel","parameters":{"channel":"a.b"}}')

    assert parsed.valid_json
    assert not parsed.is_array
    assert not parsed.schema_valid


def test_an_invented_verb_is_recorded_and_never_becomes_an_action():
    parsed = parse_plan('[{"kind":"restart_collector","parameters":{"host":"h"}}]')

    assert parsed.unknown_verbs == ("restart_collector",)
    assert parsed.actions == []
    assert not parsed.schema_valid


def test_a_missing_required_parameter_fails_validity_but_keeps_the_action():
    parsed = parse_plan('[{"kind":"silence_channel","parameters":{"channel":"a.b"}}]')

    assert parsed.missing_parameters == ("silence_channel.minutes", "silence_channel.reason")
    assert not parsed.schema_valid
    # Kept, because the eval reports what the model proposed, not only what survived.
    assert len(parsed.actions) == 1


def test_a_fenced_reply_parses_and_the_fence_is_counted_separately():
    raw = '```json\n[{"kind":"describe_channel","parameters":{"channel":"a.b"}}]\n```'
    parsed = parse_plan(raw)

    assert parsed.schema_valid
    assert has_fence(raw)
    assert not has_fence(render_response([an_action()]))


@pytest.mark.parametrize(
    "raw",
    [
        '["describe_channel"]',
        '[{"parameters":{"channel":"a.b"}}]',
        '[{"kind":"describe_channel","parameters":"channel=a.b"}]',
    ],
)
def test_entries_that_are_not_actions_are_counted_as_malformed(raw):
    parsed = parse_plan(raw)

    assert parsed.malformed_entries == 1
    assert not parsed.schema_valid


# ------------------------- the example on disk -------------------------


def test_jsonl_round_trip_preserves_the_metadata_the_eval_scores_on():
    example = TrainingExample(
        prompt="EPISODE\n  id: 1",
        response=render_response([an_action()]),
        episode_id=1,
        channel="pump-01.bearing_temp_c",
        symptom="level_shift",
        licensed=("describe_channel", "raise_ticket"),
        is_safety_channel=False,
        source="distilled",
        split="test",
        notes="n",
    )

    assert TrainingExample.from_jsonl(example.to_jsonl()) == example


def test_the_prompt_says_when_retrieval_returned_nothing():
    """An empty passage list and a passage list that failed to render must not look alike."""
    prompt = render_prompt(
        episode_id=1,
        channel="pump-01.bearing_temp_c",
        peak_score=41.2,
        window_count=3,
        duration_s=90,
        status="real",
        diagnosis_summary="sustained level shift",
        passages=[],
    )

    assert "(none matched)" in prompt


def test_the_prompt_carries_the_licences_the_target_has_to_respect():
    prompt = render_prompt(
        episode_id=1,
        channel="pump-01.bearing_temp_c",
        peak_score=41.2,
        window_count=3,
        duration_s=90,
        status="real",
        diagnosis_summary="sustained level shift",
        passages=[("sensor-faults.md", "Bearing drift", ("describe_channel", "raise_ticket"))],
        context="deploy deploy-0042 in flight",
    )

    assert "licensed-actions: describe_channel, raise_ticket" in prompt
    assert "deploy-0042" in prompt


# ------------------------- the curation contract -------------------------
# These are the tests that matter. Each asserts a property of every row in the built set.


def test_the_build_populates_every_population(dataset):
    """A population the docstring claims and the set does not contain is an overstatement."""
    sources = {e.source for e in dataset}

    assert {"distilled", "generalisation", "safety", "abstention"} <= sources
    assert len(dataset) > 100


def test_every_target_parses_to_a_schema_valid_plan(dataset):
    bad = [e for e in dataset if not parse_plan(e.response).schema_valid]

    assert bad == []


def test_every_targeted_action_is_licensed_by_a_retrieved_passage(dataset):
    for example in dataset:
        if not example.licensed:
            continue
        kinds = {str(a.kind) for a in parse_plan(example.response).actions}
        assert kinds <= set(example.licensed), f"{example.channel}: {kinds - set(example.licensed)}"


def test_an_example_with_nothing_licensed_teaches_abstention(dataset):
    """The population that answers whether the model can say it does not know."""
    abstaining = [e for e in dataset if not e.licensed]
    assert abstaining, "the abstention population is empty; the set cannot teach abstention"

    for example in abstaining:
        kinds = [a.kind for a in parse_plan(example.response).actions]
        assert kinds == [ActionKind.ESCALATE_TO_HUMAN]
        assert "(none matched)" in example.prompt


def test_the_uncovered_fraction_is_what_creates_that_population():
    """Nothing in the generator produces an empty retrieval by accident, so it is set."""
    covered_only = build(RUNBOOKS, count=300, seed=4242, uncovered_fraction=0.0)

    assert {e.source for e in covered_only} & {"abstention"} == set()
    assert all(e.licensed for e in covered_only)


def test_an_uncovered_safety_channel_is_still_labelled_safety(dataset):
    """Coverage does not downgrade the population that must never be acted on."""
    for example in dataset:
        if any(m in example.channel.lower() for m in SAFETY_MARKERS):
            assert example.source == "safety"


def test_every_targeted_action_is_approved_by_the_real_gate(dataset):
    """Not a re-implementation of the gate's rules: the gate itself, on every row.

    A fresh gate per example so one row's budget cannot reject the next row's action.
    """
    from vigil.tuning.dataset import generate_episodes

    episodes = {g.episode_id: g.episode for g in generate_episodes(400, seed=4242)}
    for example in dataset:
        gate = SafetyGate(policy=GatePolicy())
        episode = episodes[example.episode_id]
        for action in parse_plan(example.response).actions:
            decision = gate.verdict(action, episode, example.episode_id)
            assert decision.approved, f"{example.channel} {action.kind}: {decision.reason}"


def test_a_safety_channel_is_only_ever_escalated_or_described(dataset):
    """The population a model trained on easy cases would be talked out of."""
    safety = [e for e in dataset if e.is_safety_channel]
    assert safety, "the safety population is empty; the set cannot show it resists persuasion"

    for example in safety:
        kinds = {a.kind for a in parse_plan(example.response).actions}
        assert kinds <= {ActionKind.ESCALATE_TO_HUMAN, ActionKind.DESCRIBE_CHANNEL}
        assert ActionKind.ESCALATE_TO_HUMAN in kinds


def test_the_safety_flag_agrees_with_the_channel_name(dataset):
    for example in dataset:
        marked = any(m in example.channel.lower() for m in SAFETY_MARKERS)
        assert example.is_safety_channel == marked


# ------------------------- the split -------------------------


def test_no_channel_appears_in_both_splits(dataset):
    train = {e.channel for e in dataset if e.split == "train"}
    test = {e.channel for e in dataset if e.split == "test"}

    assert train & test == set()
    assert test, "an empty held-out split cannot measure generalisation"


def test_splitting_by_row_is_what_this_avoids():
    """Same channel, different rows: a row-wise split would separate them. This must not."""
    examples = [
        _example(channel="pump-00.flow_m3_h", episode_id=i, prompt=f"p{i}") for i in range(20)
    ]
    split_by_channel(examples, holdout_fraction=0.5, seed=1)

    assert len({e.split for e in examples}) == 1


def test_deduplicate_drops_identical_prompts():
    examples = [_example(episode_id=i, prompt="identical") for i in range(5)]

    assert len(deduplicate(examples)) == 1


def test_deduplicate_caps_a_symptom_so_a_common_case_cannot_swamp_the_set():
    examples = [_example(episode_id=i, prompt=f"p{i}", symptom="level_shift") for i in range(50)]

    assert len(deduplicate(examples, max_per_symptom=10)) == 10


def test_the_cap_is_per_split_not_global():
    examples = [
        _example(episode_id=i, prompt=f"p{i}", symptom="level_shift", split="train")
        for i in range(10)
    ] + [
        _example(episode_id=100 + i, prompt=f"q{i}", symptom="level_shift", split="test")
        for i in range(10)
    ]

    kept = deduplicate(examples, max_per_symptom=4)

    assert len([e for e in kept if e.split == "train"]) == 4
    assert len([e for e in kept if e.split == "test"]) == 4


def test_the_build_is_deterministic_for_a_seed():
    first = build(RUNBOOKS, count=120, seed=99)
    second = build(RUNBOOKS, count=120, seed=99)

    assert [(e.prompt, e.response, e.split) for e in first] == [
        (e.prompt, e.response, e.split) for e in second
    ]


def _example(
    *, episode_id, prompt="p", channel="pump-00.flow_m3_h", symptom="spike", split="train"
):
    return TrainingExample(
        prompt=prompt,
        response=render_response([an_action()]),
        episode_id=episode_id,
        channel=channel,
        symptom=symptom,
        licensed=("describe_channel",),
        is_safety_channel=False,
        source="distilled",
        split=split,
    )
