"""Fine-tune the tool-calling planner with QLoRA. Runs on a GPU host, not here.

    # on the reference laptop, to check everything before it costs GPU time
    python build_planner_dataset.py
    python train_planner.py --dry-run

    # on the GPU host
    pip install -e ".[train]"
    python build_planner_dataset.py
    python train_planner.py --base-model Qwen/Qwen2.5-7B-Instruct

`--dry-run` needs no GPU and no training libraries. It validates the configuration, reads
both splits, checks that the held-out split is disjoint from the training split, measures the
token-length distribution against `max_seq_length`, and prints the schedule it would run.
That is what makes it worth spending the GPU hour; it is not a substitute for having run it.

**Nothing in this repository comes from a trained adapter.** The training step has not been
executed -- see BLOCKERS B-3 for the hardware requirement and the exact command. Until it
runs, `evaluate_planner.py` reports the deterministic planner's score alone, which is the
honest state of the comparison.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vigil.tuning.qlora import (
    MissingTrainingDependency,
    QloraConfig,
    build_trainer,
    estimated_steps,
    load_jsonl,
    measure_dataset,
)

REPO = Path(__file__).resolve().parent


def dry_run(config: QloraConfig) -> int:
    """Everything that can be checked without a GPU."""
    print("configuration")
    print(config.to_json())

    problems: list[str] = []
    for path in (config.train_jsonl, config.eval_jsonl):
        if not path.exists():
            problems.append(f"{path} is missing; run: python build_planner_dataset.py")
    if problems:
        for problem in problems:
            print(f"\nFAILED: {problem}")
        return 1

    tokenizer = None
    try:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(config.base_model)
        print(f"\ntokenizer: {config.base_model} ({tokenizer.__class__.__name__})")
    except Exception as exc:  # noqa: BLE001 - the estimate is a documented fallback
        print(f"\ntokenizer unavailable ({type(exc).__name__}); using a character estimate")

    train_rows = load_jsonl(config.train_jsonl)
    test_rows = load_jsonl(config.eval_jsonl)
    train_stats = measure_dataset(config, config.train_jsonl, tokenizer)
    test_stats = measure_dataset(config, config.eval_jsonl, tokenizer)
    print(f"\ntrain  {train_stats.line()}")
    print(f"test   {test_stats.line()}")

    train_channels = {r["meta"]["channel"] for r in train_rows}
    test_channels = {r["meta"]["channel"] for r in test_rows}
    shared = train_channels & test_channels
    print(
        f"\nchannels: {len(train_channels)} train, {len(test_channels)} held out, "
        f"{len(shared)} shared"
    )

    steps = estimated_steps(config, len(train_rows))
    print(
        f"schedule: {config.num_train_epochs:g} epochs, effective batch "
        f"{config.effective_batch()}, about {steps:,} optimizer steps"
    )

    failures = []
    if shared:
        failures.append(f"{len(shared)} channels appear in both splits")
    if train_stats.over_limit or test_stats.over_limit:
        failures.append(
            f"{train_stats.over_limit + test_stats.over_limit} examples exceed "
            f"max_seq_length={config.max_seq_length} and would be truncated mid-target"
        )
    if not test_rows:
        failures.append("the held-out split is empty")

    print()
    if failures:
        print("DRY RUN FAILED")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("DRY RUN PASSED: dataset, splits and length budget are consistent.")
    print("The training step itself has not been run; this checks everything around it.")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    config = QloraConfig(
        base_model=args.base_model,
        output_dir=args.output_dir,
        train_jsonl=args.dataset / "train.jsonl",
        eval_jsonl=args.dataset / "test.jsonl",
        max_seq_length=args.max_seq_length,
        num_train_epochs=args.epochs,
        learning_rate=args.learning_rate,
        lora_r=args.lora_r,
        lora_alpha=args.lora_r * 2,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        seed=args.seed,
    )
    if args.dry_run:
        return dry_run(config)

    try:
        trainer, tokenizer, model = build_trainer(config)
    except MissingTrainingDependency as exc:
        print(f"\n{exc}")
        return 2

    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in model.parameters())
    print(f"trainable parameters: {trainable:,} of {total:,} ({trainable / total:.2%})")

    result = trainer.train()
    config.output_dir.mkdir(parents=True, exist_ok=True)
    trainer.save_model(str(config.output_dir))
    tokenizer.save_pretrained(str(config.output_dir))
    (config.output_dir / "qlora-config.json").write_text(config.to_json(), encoding="utf-8")
    (config.output_dir / "train-metrics.json").write_text(
        json.dumps(result.metrics, indent=2), encoding="utf-8"
    )
    print(f"\nadapter written to {config.output_dir}")
    print("next: python evaluate_planner.py --adapter " + str(config.output_dir))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    defaults = QloraConfig()
    p.add_argument("--dry-run", action="store_true", help="validate everything but the training")
    p.add_argument("--base-model", default=defaults.base_model)
    p.add_argument("--dataset", type=Path, default=REPO / "artifacts" / "planner-dataset")
    p.add_argument("--output-dir", type=Path, default=REPO / "artifacts" / "planner-qlora")
    p.add_argument("--max-seq-length", type=int, default=defaults.max_seq_length)
    p.add_argument("--epochs", type=float, default=defaults.num_train_epochs)
    p.add_argument("--learning-rate", type=float, default=defaults.learning_rate)
    p.add_argument("--lora-r", type=int, default=defaults.lora_r)
    p.add_argument("--batch-size", type=int, default=defaults.per_device_train_batch_size)
    p.add_argument("--grad-accum", type=int, default=defaults.gradient_accumulation_steps)
    p.add_argument("--seed", type=int, default=defaults.seed)
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
