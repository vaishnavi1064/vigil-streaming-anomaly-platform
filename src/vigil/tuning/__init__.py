"""Fine-tuning the agent's tool-calling planner.

Distinct from the detection spine's foundation model, which is used zero-shot and never
trained. This is the *other* model in the project (PROJECT_PLAN section 6) and it is the one
that gets fine-tuned. Keeping that straight matters.
"""

from vigil.tuning.dataset import build, read_jsonl, summarise, write_jsonl
from vigil.tuning.schema import (
    SYSTEM_PROMPT,
    ParsedPlan,
    TrainingExample,
    parse_plan,
    render_prompt,
    render_response,
)

__all__ = [
    "SYSTEM_PROMPT",
    "ParsedPlan",
    "TrainingExample",
    "build",
    "parse_plan",
    "read_jsonl",
    "render_prompt",
    "render_response",
    "summarise",
    "write_jsonl",
]
