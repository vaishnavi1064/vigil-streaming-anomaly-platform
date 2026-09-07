"""Score a planner on the held-out hard cases, against the deterministic baseline.

    python evaluate_planner.py                                  # baseline only
    python evaluate_planner.py --adapter artifacts/planner-qlora
    python evaluate_planner.py --endpoint http://localhost:8000/v1/chat/completions

This is the file that answers B-3's question. The claim to be tested is not "the model emits
valid JSON" -- a template does that -- but "the model beats the rules on cases the rules get
wrong". So every metric is reported for both planners on the same held-out split, and the
head-to-head is the headline.

**What the held-out split is.** Episodes whose shape is outside the five symptoms `Diagnoser`
can name, on channels and metric vocabularies the training split never contains, with every
passage stating its licences in prose rather than as a machine-readable line. Measured
against these targets the deployed planner scores **20% exact-match**: it is right on the one
symptom included precisely because the rules handle it correctly, and wrong on the rest.

**The ceiling is the teacher, not perfection.** Targets come from the policy in
`vigil.tuning.hard.TEACHER`, so a model can reach the teacher and not exceed it. What is
being measured is the gap between the deployed rules and that policy, and how much of it a
fine-tune closes.

**With no adapter and no endpoint this reports the baseline alone.** That is the honest state
until the training runs -- there is no placeholder model and no simulated score.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from vigil.agent.actions import ActionKind
from vigil.agent.gate import GatePolicy, SafetyGate
from vigil.agent.loop import Diagnoser, RunbookPlanner
from vigil.agent.runbooks import Passage, Retrieved, RunbookIndex, _parse_licenses
from vigil.tuning.hard import (
    HARD_SYSTEM_PROMPT,
    TEACHER,
    HardCase,
    generate_hard_cases,
    render_hard_prompt,
)
from vigil.tuning.schema import parse_plan

REPO = Path(__file__).resolve().parent


@dataclass
class PlannerScore:
    """One planner's performance on the held-out split."""

    planner: str
    cases: int
    exact_match: int = 0
    action_set_match: int = 0
    forbidden_proposed: int = 0
    missing_wanted: int = 0
    ungrounded_actions: int = 0
    gate_rejected: int = 0
    schema_invalid: int = 0
    unknown_verbs: int = 0
    fenced_replies: int = 0
    escalated_only: int = 0
    per_symptom_exact: dict[str, int] = field(default_factory=dict)
    per_symptom_total: dict[str, int] = field(default_factory=dict)
    latencies_ms: list[float] = field(default_factory=list)

    @property
    def exact_rate(self) -> float:
        return self.exact_match / self.cases if self.cases else 0.0

    @property
    def harm_rate(self) -> float:
        return self.forbidden_proposed / self.cases if self.cases else 0.0

    def line(self) -> str:
        latency = (
            f" | p50 {statistics.median(self.latencies_ms):.0f} ms" if self.latencies_ms else ""
        )
        return (
            f"{self.planner:<22} exact {self.exact_rate:>6.1%} | "
            f"forbidden {self.harm_rate:>6.1%} | "
            f"ungrounded {self.ungrounded_actions:>3} | "
            f"gate-rejected {self.gate_rejected:>3} | "
            f"invalid {self.schema_invalid:>3}{latency}"
        )

    def table(self) -> str:
        rows = ["  symptom            n  exact   rate"]
        for symptom in sorted(self.per_symptom_total):
            total = self.per_symptom_total[symptom]
            exact = self.per_symptom_exact.get(symptom, 0)
            rows.append(f"  {symptom:<16} {total:>3} {exact:>6}  {exact / total:>5.0%}")
        return "\n".join(rows)


def score_case(score: PlannerScore, case: HardCase, kinds: list[ActionKind], raw: str) -> None:
    """Fold one answer into the running score."""
    policy = TEACHER[case.symptom]
    wanted = [k for k in policy.wanted if str(k) in case.licensed]
    symptom = str(case.symptom)
    score.per_symptom_total[symptom] = score.per_symptom_total.get(symptom, 0) + 1

    parsed = parse_plan(raw) if raw else None
    if parsed is not None:
        if not parsed.schema_valid:
            score.schema_invalid += 1
        if parsed.unknown_verbs:
            score.unknown_verbs += 1
        if raw.strip().startswith("```"):
            score.fenced_replies += 1

    # Exact match is order-sensitive; the set match is not. Both are reported because order
    # carries real meaning here -- gather evidence before changing state -- but a model that
    # picks the right actions in a different order has not made the same mistake as one that
    # picks the wrong actions.
    if kinds == wanted:
        score.exact_match += 1
        score.per_symptom_exact[symptom] = score.per_symptom_exact.get(symptom, 0) + 1
    if set(kinds) == set(wanted):
        score.action_set_match += 1
    if set(kinds) & set(policy.forbidden):
        score.forbidden_proposed += 1
    if set(wanted) - set(kinds):
        score.missing_wanted += 1
    if kinds == [ActionKind.ESCALATE_TO_HUMAN]:
        score.escalated_only += 1
    # Escalation is exempt, for the same reason the quality gate exempts it (ADR-030): it is
    # the absence of a remediation rather than one, so no passage needs to license it.
    score.ungrounded_actions += sum(
        1 for k in kinds if k is not ActionKind.ESCALATE_TO_HUMAN and str(k) not in case.licensed
    )


def score_baseline(cases: list[HardCase], as_deployed: bool = False) -> PlannerScore:
    """The deterministic planner, scored two ways because both are true.

    `as_deployed=False` hands it the machine-readable licence set for every passage, prose
    included. That is *more* than the live parser could extract from a prose entry -- there is
    no `licensed-actions:` line to find -- so it is deliberately generous: it measures the
    planner's reasoning rather than its inability to read.

    `as_deployed=True` strips the licences from prose passages, which is exactly what
    `parse_runbook` produces from them. This is what the planner does today.

    Reporting only the first would flatter the rules; reporting only the second would make the
    fine-tune's win look larger than the reasoning gap it actually closes. Where BM25 fails to
    retrieve anything the first passage is handed over directly, so neither number is about
    retrieval missing a query these documents were never written for.
    """
    diagnoser, planner = Diagnoser(), RunbookPlanner()
    name = "rules (as deployed)" if as_deployed else "rules (given licences)"
    score = PlannerScore(planner=name, cases=len(cases))
    for case in cases:
        passages = list(case.passages)
        if as_deployed:
            passages = [
                Passage(
                    runbook=entry.runbook,
                    title=entry.title,
                    text=entry.text,
                    licenses=_parse_licenses(entry.text),
                )
                for entry in passages
            ]
        index = RunbookIndex(passages=passages)
        diagnosis = diagnoser.diagnose(case.episode)
        retrieved = index.search(diagnosis.query, limit=3) or [
            Retrieved(passage=passages[0], score=1.0, matched_terms=())
        ]
        started = time.perf_counter()
        actions = planner.plan(case.episode, case.episode_id, diagnosis, retrieved)
        score.latencies_ms.append((time.perf_counter() - started) * 1000.0)
        gate = SafetyGate(policy=GatePolicy())
        for action in actions:
            if not gate.verdict(action, case.episode, case.episode_id).approved:
                score.gate_rejected += 1
        score_case(score, case, [a.kind for a in actions], raw="")
    return score


def score_model(cases: list[HardCase], generate, name: str) -> PlannerScore:
    """Any callable that maps (system, user) to a reply string."""
    score = PlannerScore(planner=name, cases=len(cases))
    for case in cases:
        prompt = render_hard_prompt(case)
        started = time.perf_counter()
        raw = generate(HARD_SYSTEM_PROMPT, prompt)
        score.latencies_ms.append((time.perf_counter() - started) * 1000.0)
        parsed = parse_plan(raw)
        gate = SafetyGate(policy=GatePolicy())
        for action in parsed.actions:
            if not gate.verdict(action, case.episode, case.episode_id).approved:
                score.gate_rejected += 1
        score_case(score, case, [a.kind for a in parsed.actions], raw=raw)
    return score


def local_adapter(adapter: Path, base_model: str, max_new_tokens: int = 320):  # pragma: no cover
    """Load the base model in 4-bit with the adapter on top, and return a generate callable.

    Loaded with the same quantisation the adapter was trained against: a QLoRA adapter partly
    learns to compensate for its base's quantisation error, so serving it against a different
    numeric format measures a model nobody trained.
    """
    from vigil.tuning.qlora import require_training_dependencies

    require_training_dependencies()

    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    tokenizer = AutoTokenizer.from_pretrained(base_model)
    model = AutoModelForCausalLM.from_pretrained(
        base_model,
        quantization_config=BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
        ),
        torch_dtype=torch.bfloat16,
        device_map="auto",
    )
    model = PeftModel.from_pretrained(model, str(adapter))
    model.eval()

    def generate(system: str, user: str) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = tokenizer(text, return_tensors="pt").to(model.device)
        with torch.no_grad():
            out = model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                do_sample=False,
                pad_token_id=tokenizer.eos_token_id,
            )
        return tokenizer.decode(out[0][inputs["input_ids"].shape[1] :], skip_special_tokens=True)

    return generate


def endpoint_planner(url: str, model: str, api_key: str = ""):  # pragma: no cover
    """An OpenAI-compatible endpoint -- vLLM serving the merged adapter, for instance."""
    import httpx

    def generate(system: str, user: str) -> str:
        response = httpx.post(
            url,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            json={
                "model": model,
                "temperature": 0.0,
                "max_tokens": 320,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=120.0,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]

    return generate


def compare(
    baseline: PlannerScore, model: PlannerScore | None, deployed: PlannerScore | None = None
) -> str:
    lines = [f"\n{'=' * 92}", "HELD-OUT HARD CASES", f"{'=' * 92}"]
    if deployed is not None:
        lines.append(deployed.line())
    lines.append(baseline.line())
    if model is not None:
        lines.append(model.line())
        delta = model.exact_rate - baseline.exact_rate
        harm = model.harm_rate - baseline.harm_rate
        lines += [
            "",
            f"exact-match delta {delta:+.1%} | forbidden-action delta {harm:+.1%}",
            (
                "The fine-tune beats the rules on this split."
                if delta > 0
                else "The fine-tune does not beat the rules on this split."
            ),
        ]
    else:
        lines += [
            "",
            "No model scored: pass --adapter or --endpoint. The training step has not been",
            "run in this repository, so the baseline column is the whole of the result.",
        ]
    lines += ["", "baseline, per symptom:", baseline.table()]
    if model is not None:
        lines += ["", f"{model.planner}, per symptom:", model.table()]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cases = generate_hard_cases(args.cases, seed=args.seed, split="test")
    print(f"scoring {len(cases)} held-out hard cases (seed {args.seed})")

    deployed = score_baseline(cases, as_deployed=True)
    baseline = score_baseline(cases, as_deployed=False)
    model = None
    if args.adapter:
        model = score_model(
            cases, local_adapter(args.adapter, args.base_model), f"qlora:{args.adapter.name}"
        )
    elif args.endpoint:
        model = score_model(
            cases,
            endpoint_planner(args.endpoint, args.model, args.api_key),
            f"endpoint:{args.model}",
        )

    print(compare(baseline, model, deployed))

    if args.report_json:
        args.report_json.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "cases": len(cases),
            "seed": args.seed,
            "baseline_given_licences": asdict(baseline),
            "baseline_as_deployed": asdict(deployed),
            "model": asdict(model) if model else None,
        }
        args.report_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nreport written to {args.report_json}")

    if model is None:
        return 0
    return 0 if model.exact_rate > baseline.exact_rate else 1


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--cases", type=int, default=300)
    p.add_argument("--seed", type=int, default=20260907)
    p.add_argument("--adapter", type=Path, default=None, help="a QLoRA adapter directory")
    p.add_argument("--base-model", default="Qwen/Qwen2.5-7B-Instruct")
    p.add_argument("--endpoint", default=None, help="an OpenAI-compatible chat-completions URL")
    p.add_argument("--model", default="planner", help="model name to send to --endpoint")
    p.add_argument("--api-key", default="")
    p.add_argument("--report-json", type=Path, default=None)
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
