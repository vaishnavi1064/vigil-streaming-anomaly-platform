"""Write the tool-calling training set to disk, in the two shapes the pipeline needs.

    python build_planner_dataset.py                       # both sets, default sizes
    python build_planner_dataset.py --hard-only
    python build_planner_dataset.py --out artifacts/planner-dataset

Two sets, because they answer different questions:

  * **easy** -- the original distillation set (`vigil.tuning.dataset.build`). Kept because it
    is the evidence for B-3's finding: five distinct targets, each a function of a symptom
    the prompt already contains. Not used for training any more; used to show why.
  * **hard** -- episodes whose shape is outside what `Diagnoser` can name, with half the
    passages stating their licences in prose. The deployed planner scores 20% exact-match
    against these targets, so the fine-tune has measurable room above the baseline.

The hard test split shares no channel, no metric vocabulary and no prose phrasing template
with its training split, so a model that memorised surface forms fails it by construction.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vigil.tuning.dataset import build, build_hard, summarise, summarise_hard, write_jsonl

REPO = Path(__file__).resolve().parent


def split_of(examples, name):
    return [e for e in examples if e.split == name]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out = args.out
    out.mkdir(parents=True, exist_ok=True)

    print(f"writing to {out}")

    hard = build_hard(train_count=args.hard_train, test_count=args.hard_test, seed=args.seed)
    train, test = split_of(hard, "train"), split_of(hard, "test")
    write_jsonl(train, out / "train.jsonl")
    write_jsonl(test, out / "test.jsonl")
    print("\nhard set (used for training):")
    print(summarise_hard(hard))
    print(f"  -> {out / 'train.jsonl'} ({len(train):,} rows)")
    print(f"  -> {out / 'test.jsonl'} ({len(test):,} rows)")

    if not args.hard_only:
        easy = build(REPO / "runbooks", count=args.easy_count, seed=args.seed)
        write_jsonl(easy, out / "easy-reference.jsonl")
        print("\neasy set (kept as the evidence for B-3, not for training):")
        print(summarise(easy))
        print(f"  -> {out / 'easy-reference.jsonl'} ({len(easy):,} rows)")

    manifest = {
        "hard_train": len(train),
        "hard_test": len(test),
        "seed": args.seed,
        "train_channels": sorted({e.channel for e in train}),
        "test_channels": sorted({e.channel for e in test}),
        "shared_channels": sorted({e.channel for e in train} & {e.channel for e in test}),
        "test_all_prose": all(e.source == "hard:prose" for e in test),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nmanifest -> {out / 'manifest.json'}")

    if manifest["shared_channels"]:
        print("\nFAILED: the splits share channels, so the held-out score would be inflated")
        return 1
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--out", type=Path, default=REPO / "artifacts" / "planner-dataset")
    p.add_argument("--hard-train", type=int, default=1200)
    p.add_argument("--hard-test", type=int, default=300)
    p.add_argument("--easy-count", type=int, default=1200)
    p.add_argument("--hard-only", action="store_true")
    p.add_argument("--seed", type=int, default=20260907)
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
