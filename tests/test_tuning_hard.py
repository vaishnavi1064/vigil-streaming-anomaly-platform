"""Tests for the hard training set and the QLoRA pipeline.

B-3's objection to the first dataset was arithmetic: the target was a function of a symptom
the prompt already contained, so a model could approach the rules and never beat them. This
set exists to remove that ceiling, and these tests hold the three things that have to be true
for it to have done so.

**The premise has to stay true.** The deployed planner must actually be wrong on these cases.
If someone teaches `Diagnoser` these shapes, the headroom disappears and the fine-tune becomes
pointless again -- so a test asserts the baseline is bad here, and it is meant to fail loudly
if that stops being the case.

**The answer must not be in the prompt.** No symptom, no diagnosis summary, no licence list.

**The held-out split must require reading.** Disjoint channels, disjoint metric vocabulary,
disjoint prose templates, and no machine-readable licence line anywhere in it.

The training step itself is not tested, because it has not been run. What is tested is
everything around it, which is what `--dry-run` checks and what these tests pin.
"""

import json

import pytest

from vigil.agent.actions import ActionKind
from vigil.agent.gate import GatePolicy, SafetyGate
from vigil.agent.loop import Diagnoser, RunbookPlanner
from vigil.agent.runbooks import Retrieved, RunbookIndex
from vigil.tuning.dataset import build_hard
from vigil.tuning.hard import (
    HARD_SYSTEM_PROMPT,
    HELD_OUT_METRICS,
    HELD_OUT_UNITS,
    LICENCES,
    TEACHER,
    TRAIN_METRICS,
    TRAIN_UNITS,
    HardSymptom,
    generate_hard_cases,
    hard_passage,
    render_hard_prompt,
    teacher_actions,
)
from vigil.tuning.qlora import (
    MissingTrainingDependency,
    QloraConfig,
    as_chat,
    estimated_steps,
    measure_dataset,
    require_training_dependencies,
)
from vigil.tuning.schema import parse_plan


@pytest.fixture(scope="module")
def train_cases():
    return generate_hard_cases(150, split="train")


@pytest.fixture(scope="module")
def test_cases():
    return generate_hard_cases(100, split="test")


# ------------------------- the premise: the rules are wrong here -------------------------


def baseline_kinds(case):
    """What the deployed planner proposes, given the passage directly so retrieval is not the
    thing under test."""
    index = RunbookIndex(passages=list(case.passages))
    diagnosis = Diagnoser().diagnose(case.episode)
    retrieved = index.search(diagnosis.query, limit=3) or [
        Retrieved(passage=case.passages[0], score=1.0, matched_terms=())
    ]
    actions = RunbookPlanner().plan(case.episode, case.episode_id, diagnosis, retrieved)
    return [a.kind for a in actions]


def test_the_deployed_planner_is_measurably_wrong_on_the_held_out_set(test_cases):
    """The headroom this dataset exists to create. If this test fails, the fine-tune is
    pointless again and the dataset needs rebuilding, not the assertion relaxing."""
    exact = 0
    for case in test_cases:
        wanted = [k for k in TEACHER[case.symptom].wanted if str(k) in case.licensed]
        if baseline_kinds(case) == wanted:
            exact += 1

    rate = exact / len(test_cases)
    assert rate < 0.5, f"the rules score {rate:.0%} here; there is no room for a model to win"
    assert rate > 0.0, "the rules score nothing at all, which suggests the teacher is arbitrary"


def test_the_rules_propose_a_forbidden_action_on_at_least_one_symptom(test_cases):
    """Not merely incomplete: actively wrong, which is the strongest form of the gap."""
    harmful = [
        case
        for case in test_cases
        if set(baseline_kinds(case)) & set(TEACHER[case.symptom].forbidden)
    ]

    assert harmful, "no case where the rules do harm; the set is weaker than it claims"
    assert {c.symptom for c in harmful} <= set(HardSymptom)


def test_a_licence_the_symptom_does_not_grant_bounds_the_damage(test_cases):
    """Where the wrong action is not licensed, the licence mechanism stops it.

    Worth pinning as a property of the design rather than a happy accident: a misdiagnosis
    can only do harm when the harmful action happens to be licensed for that entry.
    """
    for case in test_cases:
        forbidden = ActionKind.SILENCE_CHANNEL in TEACHER[case.symptom].forbidden
        if forbidden and "silence_channel" not in case.licensed:
            assert ActionKind.SILENCE_CHANNEL not in baseline_kinds(case)


# ------------------------- the answer is not in the prompt -------------------------


def test_the_episode_block_carries_no_label_of_any_kind(test_cases):
    """The passages name patterns, as real runbook headings do. The *episode* must not.

    The distinction that matters: a heading like "Regular oscillation at a fixed period" is a
    legitimate document title and appears for distractors too, so it does not say which entry
    this episode belongs to. A symptom name in the episode block, or in the filename, would.
    """
    for case in test_cases[:20]:
        prompt = render_hard_prompt(case)
        episode_block = prompt.split("RETRIEVED RUNBOOK PASSAGES")[0]

        for symptom in HardSymptom:
            assert str(symptom) not in episode_block
        assert "licensed-actions" not in prompt
        assert "summary" not in episode_block.lower()
        for passage in case.passages:
            assert str(case.symptom) not in passage.runbook
        # The evidence is present, which is the point: inference, not lookup.
        assert "PER-WINDOW DETECTOR EVIDENCE" in prompt
        assert "mean_z" in prompt and "dispersion_z" in prompt


def test_the_correct_entry_is_not_marked_or_ordered_first(test_cases):
    """If the answer were always the first passage, position would be the label."""
    from vigil.tuning.hard import _TITLES

    positions = set()
    for case in test_cases:
        titles = [p.title for p in case.passages]
        positions.add(titles.index(_TITLES[case.symptom]))

    assert len(positions) > 1, "the correct entry always sits in the same position"


def test_the_system_prompt_explains_the_evidence_rather_than_the_answer():
    assert "dispersion_ratio below 1" in HARD_SYSTEM_PROMPT
    assert "Nothing tells you the answer directly" in HARD_SYSTEM_PROMPT
    for symptom in HardSymptom:
        assert str(symptom) not in HARD_SYSTEM_PROMPT
        assert str(symptom).replace("_", " ") not in HARD_SYSTEM_PROMPT


# ------------------------- prose licences require reading -------------------------


def test_a_prose_passage_carries_no_machine_readable_licence_line():
    passage = hard_passage(HardSymptom.OSCILLATION, LICENCES[HardSymptom.OSCILLATION], prose=True)

    assert "licensed-actions:" not in passage.text.lower()
    # The set survives on the object, because the scorer needs the answer even though the
    # model must read for it.
    assert passage.licenses == LICENCES[HardSymptom.OSCILLATION]


def test_the_deployed_planner_cannot_act_on_a_prose_passage():
    """The reading requirement is real, not decorative: the rules abstain here."""
    from vigil.agent.runbooks import Passage

    passage = hard_passage(HardSymptom.OSCILLATION, LICENCES[HardSymptom.OSCILLATION], prose=True)
    # Licences stripped, which is what the live parser produces from prose: there is no
    # `licensed-actions:` line for it to find.
    as_seen = Passage(runbook=passage.runbook, title=passage.title, text=passage.text, licenses=())

    case = generate_hard_cases(1, split="train")[0]
    diagnosis = Diagnoser().diagnose(case.episode)
    actions = RunbookPlanner().plan(
        case.episode,
        case.episode_id,
        diagnosis,
        [Retrieved(passage=as_seen, score=1.0, matched_terms=())],
    )

    assert [a.kind for a in actions] == [ActionKind.ESCALATE_TO_HUMAN]


def test_the_held_out_split_is_entirely_prose(test_cases):
    assert all(case.prose_licences for case in test_cases)


def test_held_out_prose_phrasing_never_appears_in_training(train_cases, test_cases):
    """A model that matched a sentence template instead of reading it fails the held-out set."""
    training_text = "\n".join(p.text for case in train_cases for p in case.passages)
    held_out_markers = (
        "it is permitted to",
        "The standing agreement with operations",
    )
    for marker in held_out_markers:
        assert marker not in training_text
    held_out_text = "\n".join(p.text for case in test_cases for p in case.passages)
    assert any(marker in held_out_text for marker in held_out_markers)


# ------------------------- the split -------------------------


def test_the_splits_share_no_channel_and_no_vocabulary(train_cases, test_cases):
    train_channels = {c.episode.channel for c in train_cases}
    test_channels = {c.episode.channel for c in test_cases}

    assert train_channels & test_channels == set()
    for case in test_cases:
        unit, metric = case.episode.channel.split(".", 1)
        assert unit in HELD_OUT_UNITS and metric in HELD_OUT_METRICS
    for case in train_cases:
        unit, metric = case.episode.channel.split(".", 1)
        assert unit in TRAIN_UNITS and metric in TRAIN_METRICS


def test_every_symptom_appears_in_both_splits(train_cases, test_cases):
    assert {c.symptom for c in train_cases} == set(HardSymptom)
    assert {c.symptom for c in test_cases} == set(HardSymptom)


# ------------------------- the curation contract still holds -------------------------


def test_every_teacher_target_is_gate_approved(train_cases, test_cases):
    for case in train_cases + test_cases:
        gate = SafetyGate(policy=GatePolicy())
        for action in case.teacher_actions:
            decision = gate.verdict(action, case.episode, case.episode_id)
            assert decision.approved, f"{case.symptom} {action.kind}: {decision.reason}"


def test_every_teacher_target_is_licensed_and_parses(train_cases, test_cases):
    from vigil.tuning.schema import render_response

    for case in train_cases + test_cases:
        for action in case.teacher_actions:
            # Licensed by the entry that matches the evidence, not merely by something that
            # was retrieved: grounding in a distractor is a failure the score has to see.
            assert str(action.kind) in case.answer_licences
            assert str(action.kind) in case.licensed
        assert parse_plan(render_response(case.teacher_actions)).schema_valid


def test_retrieval_returns_distractors_so_the_evidence_has_to_be_read(test_cases):
    """With one passage the task is "read the only entry you were given" -- no inference."""
    for case in test_cases[:20]:
        assert len(case.passages) >= 3
        titles = {p.title for p in case.passages}
        assert len(titles) == len(case.passages), "a distractor was duplicated"
        # The union is strictly larger than the answer's own licences on most cases, which is
        # what makes grounded-but-wrong a distinguishable outcome.
        assert set(case.answer_licences) <= set(case.licensed)
    assert any(set(c.licensed) > set(c.answer_licences) for c in test_cases)


def test_the_passage_bodies_do_not_mention_the_planner_or_this_dataset(test_cases):
    """An earlier version put the design commentary in the runbook the model reads."""
    for case in test_cases[:20]:
        for passage in case.passages:
            body = passage.text.lower()
            for leak in ("the rules", "deterministic", "dataset", "fine-tune", "model"):
                assert leak not in body, f"{passage.title} leaks {leak!r}"


def test_a_teacher_target_never_contains_a_forbidden_action(train_cases, test_cases):
    for case in train_cases + test_cases:
        kinds = {a.kind for a in case.teacher_actions}
        assert kinds & set(TEACHER[case.symptom].forbidden) == set()


def test_an_unlicensed_wanted_action_shortens_the_target_rather_than_appearing():
    """Stops a model learning "always emit four actions" from a set where four always fit."""
    licences = ("describe_channel",)
    actions = teacher_actions(HardSymptom.SLOW_DRIFT, 1, "a.b", licences)

    assert [a.kind for a in actions] == [ActionKind.DESCRIBE_CHANNEL]


def test_a_symptom_with_nothing_licensed_falls_back_to_escalation():
    actions = teacher_actions(HardSymptom.STUCK_SENSOR, 1, "a.b", ())

    assert [a.kind for a in actions] == [ActionKind.ESCALATE_TO_HUMAN]


def test_the_built_examples_carry_the_split_and_the_licence_style():
    examples = build_hard(train_count=50, test_count=25)
    train = [e for e in examples if e.split == "train"]
    test = [e for e in examples if e.split == "test"]

    assert len(train) == 50 and len(test) == 25
    assert all(e.source.startswith("hard:") for e in examples)
    assert {e.source for e in test} == {"hard:prose"}


# ------------------------- the QLoRA pipeline -------------------------


def test_the_config_reports_its_effective_batch_and_step_count():
    config = QloraConfig(
        per_device_train_batch_size=2, gradient_accumulation_steps=8, num_train_epochs=3
    )

    assert config.effective_batch() == 16
    assert estimated_steps(config, 1600) == 300


def test_the_config_serialises_to_reviewable_json():
    payload = json.loads(QloraConfig().to_json())

    assert payload["bnb_4bit_quant_type"] == "nf4"
    assert payload["train_on_completions_only"] is True
    assert isinstance(payload["target_modules"], list)
    assert "q_proj" in payload["target_modules"] and "down_proj" in payload["target_modules"]


def test_a_dataset_row_splits_into_prompt_turns_and_a_target(tmp_path):
    examples = build_hard(train_count=4, test_count=2)
    from vigil.tuning.dataset import write_jsonl

    path = tmp_path / "rows.jsonl"
    write_jsonl(examples, path)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    prompt, completion = as_chat(rows[0])

    assert [m["role"] for m in prompt] == ["system", "user"]
    assert completion.startswith("[")
    assert parse_plan(completion).schema_valid


def test_measuring_a_dataset_reports_lengths_and_what_exceeds_the_limit(tmp_path):
    examples = build_hard(train_count=20, test_count=5)
    from vigil.tuning.dataset import write_jsonl

    path = tmp_path / "rows.jsonl"
    write_jsonl([e for e in examples if e.split == "train"], path)

    generous = measure_dataset(QloraConfig(max_seq_length=4096), path)
    tiny = measure_dataset(QloraConfig(max_seq_length=16), path)

    assert generous.examples == 20
    assert generous.prompt_tokens_p50 > 0
    assert generous.over_limit == 0
    assert tiny.over_limit == 20
    assert "over the 16-token limit" in tiny.line()


def test_missing_training_dependencies_name_the_install_command():
    """This environment has none of them, which is what makes this assertion meaningful."""
    with pytest.raises(MissingTrainingDependency, match=r'pip install -e ".\[train\]"'):
        require_training_dependencies()
