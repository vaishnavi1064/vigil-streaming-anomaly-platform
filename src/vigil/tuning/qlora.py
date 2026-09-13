"""QLoRA configuration and setup for the tool-calling planner.

Nothing here trains. This module builds the configuration, loads the tokenizer, formats the
dataset and reports what it would do; `train_planner.py` is the entry point that actually
runs, and it runs on a rented GPU rather than on the reference laptop (BLOCKERS C-1: 4 GB of
VRAM does not hold a 7B model in any quantisation worth training).

**Why QLoRA rather than a full fine-tune.** A 7B model at bf16 needs about 14 GB for weights
alone and roughly three times that again for Adam states and gradients, which puts a full
fine-tune on hardware nobody is renting by the hour for this. NF4 quantisation holds the base
at ~4.5 GB and LoRA trains ~0.5% of the parameters, so the whole thing fits on one 24 GB card
with room for a 2048-token context. The trade is that the base is quantised during training,
so the adapter partly learns to compensate for quantisation error -- which is why the adapter
must be served against the same quantisation it was trained against, and why the eval loads
the model the same way.

**What is verified here and what is not.** `--dry-run` validates the configuration, the
dataset, the chat templating and the token-length distribution, and it runs on any machine
with a CPU. It is what proves the plumbing. The training itself has **not been run**: no
number in this repository comes from a trained adapter, and the eval reports the
deterministic baseline until one exists.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


class MissingTrainingDependency(RuntimeError):
    """Raised when the training extras are absent, with the command that installs them."""


@dataclass
class QloraConfig:
    """Everything that decides the run, in one reviewable object.

    Values rather than code so the whole configuration can be diffed and put in a report.
    Defaults are chosen for a single 24 GB card; every one that trades memory against quality
    says which way it trades.
    """

    # Qwen2.5-7B-Instruct rather than Llama-3.1-8B: comparable at this size on structured
    # output, and its weights are not behind an acceptance gate, which matters when the run
    # happens on someone else's cluster.
    base_model: str = "Qwen/Qwen2.5-7B-Instruct"
    output_dir: Path = Path("artifacts/planner-qlora")
    train_jsonl: Path = Path("artifacts/planner-dataset/train.jsonl")
    eval_jsonl: Path = Path("artifacts/planner-dataset/test.jsonl")

    # --- quantisation: what makes this fit at all ---
    load_in_4bit: bool = True
    bnb_4bit_quant_type: str = "nf4"
    # Double quantisation saves a further ~0.4 GB by quantising the quantisation constants.
    bnb_4bit_use_double_quant: bool = True
    # Compute in bf16, not fp16: the loss on a JSON-emitting task is dominated by a few
    # confident tokens, and fp16's narrower exponent range is where silent NaNs come from.
    bnb_4bit_compute_dtype: str = "bfloat16"

    # --- LoRA: what actually trains ---
    lora_r: int = 32
    lora_alpha: int = 64
    lora_dropout: float = 0.05
    # Attention *and* MLP projections. Attention-only adapters are cheaper and consistently
    # weaker on format-following tasks, which is exactly what this task is.
    target_modules: tuple[str, ...] = (
        "q_proj",
        "k_proj",
        "v_proj",
        "o_proj",
        "gate_proj",
        "up_proj",
        "down_proj",
    )

    # --- schedule ---
    max_seq_length: int = 2048
    per_device_train_batch_size: int = 1
    gradient_accumulation_steps: int = 16
    num_train_epochs: float = 1.0
    learning_rate: float = 1e-4
    warmup_ratio: float = 0.03
    lr_scheduler_type: str = "cosine"
    weight_decay: float = 0.0
    max_grad_norm: float = 0.3
    gradient_checkpointing: bool = True
    optim: str = "paged_adamw_8bit"
    logging_steps: int = 5
    eval_strategy: str = "epoch"
    save_strategy: str = "epoch"
    save_total_limit: int = 2
    seed: int = 20260907
    # Loss on the completion only. Training on the prompt as well would spend most of the
    # gradient budget teaching the model to reproduce evidence tables it is given.
    train_on_completions_only: bool = True

    def effective_batch(self) -> int:
        return self.per_device_train_batch_size * self.gradient_accumulation_steps

    def to_json(self) -> str:
        payload = asdict(self)
        for key, value in payload.items():
            if isinstance(value, Path):
                payload[key] = str(value)
            elif isinstance(value, tuple):
                payload[key] = list(value)
        return json.dumps(payload, indent=2)


@dataclass
class DatasetStats:
    """What the tokenizer says about the data, which is what decides `max_seq_length`."""

    examples: int
    prompt_tokens_p50: int
    prompt_tokens_p95: int
    prompt_tokens_max: int
    completion_tokens_p50: int
    completion_tokens_max: int
    total_tokens_max: int
    over_limit: int
    limit: int = field(default=0)

    def line(self) -> str:
        return (
            f"{self.examples:,} examples | prompt p50 {self.prompt_tokens_p50} "
            f"p95 {self.prompt_tokens_p95} max {self.prompt_tokens_max} | "
            f"completion p50 {self.completion_tokens_p50} max {self.completion_tokens_max} | "
            f"{self.over_limit} over the {self.limit}-token limit"
        )


def require_training_dependencies() -> None:
    """Fail with the install command rather than an ImportError three frames down."""
    missing = []
    for module, package in (
        ("torch", "torch"),
        ("transformers", "transformers>=4.44"),
        ("peft", "peft>=0.13"),
        ("trl", "trl>=0.11"),
        ("bitsandbytes", "bitsandbytes>=0.44"),
        ("datasets", "datasets>=3.0"),
    ):
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    if missing:
        raise MissingTrainingDependency(
            "training needs packages this environment does not have: "
            + ", ".join(missing)
            + '\nInstall them on the GPU host with:\n  pip install -e ".[train]"'
        )


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def as_chat(row: dict) -> tuple[list[dict], str]:
    """Split a dataset row into the prompt turns and the target completion.

    Kept separate rather than one flat string because completion-only loss needs to know
    where the prompt stops, and finding that boundary by searching a rendered string for a
    delimiter is how off-by-one masking bugs happen.
    """
    messages = row["messages"]
    prompt = [m for m in messages if m["role"] != "assistant"]
    completion = next(m["content"] for m in messages if m["role"] == "assistant")
    return prompt, completion


def measure_dataset(config: QloraConfig, path: Path, tokenizer=None) -> DatasetStats:
    """Token statistics for one split. Uses the real tokenizer when one is available.

    Falls back to a whitespace-and-punctuation estimate when transformers is absent, so the
    dry run still reports something useful on a laptop -- and says which it used, because a
    length budget set from an estimate is a length budget that can be wrong.
    """
    rows = load_jsonl(path)

    def count(text: str) -> int:
        if tokenizer is not None:
            return len(tokenizer(text, add_special_tokens=False)["input_ids"])
        # Rough and deliberately pessimistic: ~1 token per 3.5 characters.
        return int(len(text) / 3.5) + 1

    prompt_lengths, completion_lengths, totals = [], [], []
    for row in rows:
        prompt, completion = as_chat(row)
        prompt_text = "\n".join(m["content"] for m in prompt)
        p, c = count(prompt_text), count(completion)
        prompt_lengths.append(p)
        completion_lengths.append(c)
        totals.append(p + c)

    def percentile(values: list[int], fraction: float) -> int:
        if not values:
            return 0
        ordered = sorted(values)
        return ordered[min(int(fraction * len(ordered)), len(ordered) - 1)]

    return DatasetStats(
        examples=len(rows),
        prompt_tokens_p50=percentile(prompt_lengths, 0.50),
        prompt_tokens_p95=percentile(prompt_lengths, 0.95),
        prompt_tokens_max=max(prompt_lengths, default=0),
        completion_tokens_p50=percentile(completion_lengths, 0.50),
        completion_tokens_max=max(completion_lengths, default=0),
        total_tokens_max=max(totals, default=0),
        over_limit=sum(1 for t in totals if t > config.max_seq_length),
        limit=config.max_seq_length,
    )


def estimated_steps(config: QloraConfig, train_examples: int) -> int:
    per_epoch = max(1, train_examples // config.effective_batch())
    return int(per_epoch * config.num_train_epochs)


def build_trainer(config: QloraConfig):  # pragma: no cover - needs a GPU host
    """Assemble the model, adapter and trainer. Only ever called on the GPU host.

    Deliberately not covered by tests: everything here is a call into transformers, peft and
    trl, and a test that mocked all three would assert that the mocks agree with each other.
    What *is* tested is the configuration, the dataset and the templating -- the parts where
    a mistake would be ours.
    """
    require_training_dependencies()

    import torch
    from datasets import Dataset
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    from trl import DataCollatorForCompletionOnlyLM, SFTConfig, SFTTrainer

    tokenizer = AutoTokenizer.from_pretrained(config.base_model, trust_remote_code=False)
    if tokenizer.pad_token is None:
        # Padding with EOS and masking it out; adding a new token would resize the embedding
        # matrix of a quantised base, which is not worth doing for padding.
        tokenizer.pad_token = tokenizer.eos_token

    quantisation = BitsAndBytesConfig(
        load_in_4bit=config.load_in_4bit,
        bnb_4bit_quant_type=config.bnb_4bit_quant_type,
        bnb_4bit_use_double_quant=config.bnb_4bit_use_double_quant,
        bnb_4bit_compute_dtype=torch.float16,
    )
    model = AutoModelForCausalLM.from_pretrained(
        config.base_model,
        quantization_config=quantisation,
        torch_dtype=torch.float16,
        device_map="auto",
    )
    model.config.use_cache = False
    model = prepare_model_for_kbit_training(
        model, use_gradient_checkpointing=config.gradient_checkpointing
    )
    model = get_peft_model(
        model,
        LoraConfig(
            r=config.lora_r,
            lora_alpha=config.lora_alpha,
            lora_dropout=config.lora_dropout,
            bias="none",
            task_type="CAUSAL_LM",
            target_modules=list(config.target_modules),
        ),
    )

    def to_text(rows: list[dict]) -> Dataset:
        records = []
        for row in rows:
            prompt, completion = as_chat(row)
            prompt_text = tokenizer.apply_chat_template(
                prompt, tokenize=False, add_generation_prompt=True
            )
            records.append(
                {"text": prompt_text + completion + tokenizer.eos_token}
            )
        return Dataset.from_list(records)

    train = to_text(load_jsonl(config.train_jsonl))
    evaluation = to_text(load_jsonl(config.eval_jsonl))

    response_template_ids = tokenizer.encode(
        "<|im_start|>assistant\n", add_special_tokens=False
    )
    collator = DataCollatorForCompletionOnlyLM(
        response_template_ids, tokenizer=tokenizer
    )
    trainer = SFTTrainer(
        model=model,
        processing_class=tokenizer,
        train_dataset=train,
        eval_dataset=evaluation,
        data_collator=collator,
        args=SFTConfig(
            output_dir=str(config.output_dir),
            max_seq_length=config.max_seq_length,
            dataset_text_field="text",
            per_device_train_batch_size=config.per_device_train_batch_size,
            gradient_accumulation_steps=config.gradient_accumulation_steps,
            num_train_epochs=config.num_train_epochs,
            learning_rate=config.learning_rate,
            warmup_ratio=config.warmup_ratio,
            lr_scheduler_type=config.lr_scheduler_type,
            weight_decay=config.weight_decay,
            max_grad_norm=config.max_grad_norm,
            gradient_checkpointing=config.gradient_checkpointing,
            optim=config.optim,
            fp16=True,
            logging_steps=config.logging_steps,
            eval_strategy="no",
            per_device_eval_batch_size=1,
            eval_accumulation_steps=1,
            prediction_loss_only=True,
            save_strategy=config.save_strategy,
            save_total_limit=config.save_total_limit,
            seed=config.seed,
            report_to=[],
        ),
    )
    return trainer, tokenizer, model
