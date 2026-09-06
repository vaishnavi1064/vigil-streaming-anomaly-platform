"""Runbook retrieval: grounding a diagnosis in operational documents.

The agent does not reason freely about what to do. It retrieves the runbook passages that
match an episode and proposes only what those passages license. That is the difference
between a diagnosis and a guess, and it is what makes a wrong answer traceable to a
document rather than to a model's mood.

**Why lexical retrieval and not embeddings.** The corpus is small (tens of documents), the
queries are dominated by exact operational vocabulary -- channel names, metric names, fault
kinds -- and BM25 handles precisely that better than a dense model does, with no weights to
load, no service to run, and no per-query latency worth measuring. A dense retriever earns
its place when queries are paraphrases of documents; here they largely are not. This is
recorded in ADR-024 rather than defended as an obvious choice, because it is a real trade:
a genuine paraphrase will be missed.

Retrieval returns **passages with scores**, not an answer. Whatever consumes them has to
decide what they license, and a passage that scored poorly should be visible as such.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

_TOKEN = re.compile(r"[a-z0-9_]+")


def tokenize(text: str) -> list[str]:
    """Lowercase word tokens, keeping underscores so channel names survive intact.

    `bearing_temp_c` must not become three tokens: it is one operational term, and splitting
    it would let a query about temperature match every channel with a `_c` suffix.
    """
    return _TOKEN.findall(text.lower())


@dataclass(frozen=True)
class Passage:
    """One retrievable chunk of a runbook."""

    runbook: str
    title: str
    text: str
    # Actions this passage explicitly licenses, by name. The agent may propose these; the
    # gate still has to approve them. Retrieval widens what is considered, never what is
    # permitted.
    licenses: tuple[str, ...] = ()

    @property
    def tokens(self) -> list[str]:
        return tokenize(f"{self.title} {self.text}")


@dataclass(frozen=True)
class Retrieved:
    passage: Passage
    score: float
    matched_terms: tuple[str, ...]


@dataclass
class RunbookIndex:
    """BM25 over a small corpus of operational passages."""

    passages: list[Passage] = field(default_factory=list)
    k1: float = 1.5
    b: float = 0.75

    _df: Counter = field(default_factory=Counter, init=False, repr=False)
    _lengths: list[int] = field(default_factory=list, init=False, repr=False)
    _avg_length: float = field(default=0.0, init=False, repr=False)
    _tokenized: list[Counter] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        self.reindex()

    def add(self, passage: Passage) -> None:
        self.passages.append(passage)
        self.reindex()

    def reindex(self) -> None:
        self._df = Counter()
        self._tokenized = []
        self._lengths = []
        for passage in self.passages:
            tokens = passage.tokens
            counts = Counter(tokens)
            self._tokenized.append(counts)
            self._lengths.append(len(tokens))
            for term in counts:
                self._df[term] += 1
        self._avg_length = sum(self._lengths) / len(self._lengths) if self._lengths else 0.0

    def search(self, query: str, limit: int = 3, min_score: float = 0.0) -> list[Retrieved]:
        """Rank passages against a query. Empty when nothing scores above the floor.

        An empty result is a real answer -- "the runbooks do not cover this" -- and is what
        should send the agent to a human rather than to invention.
        """
        terms = tokenize(query)
        if not terms or not self.passages:
            return []

        n = len(self.passages)
        scored: list[Retrieved] = []
        for i, passage in enumerate(self.passages):
            counts = self._tokenized[i]
            length = self._lengths[i]
            score = 0.0
            matched = []
            for term in set(terms):
                tf = counts.get(term, 0)
                if not tf:
                    continue
                matched.append(term)
                df = self._df[term]
                # BM25 with the standard smoothed IDF, which stays positive for a term in
                # every document rather than going negative and penalising a match.
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                denominator = tf + self.k1 * (
                    1 - self.b + self.b * length / max(self._avg_length, 1e-9)
                )
                score += idf * (tf * (self.k1 + 1)) / denominator
            if score > min_score:
                scored.append(
                    Retrieved(passage=passage, score=score, matched_terms=tuple(sorted(matched)))
                )

        scored.sort(key=lambda r: (-r.score, r.passage.title))
        return scored[:limit]

    def licensed_actions(self, retrieved: list[Retrieved]) -> set[str]:
        """Everything the retrieved passages license, as a set of action names."""
        out: set[str] = set()
        for hit in retrieved:
            out.update(hit.passage.licenses)
        return out

    def __len__(self) -> int:
        return len(self.passages)


def parse_runbook(path: Path) -> list[Passage]:
    """Split a markdown runbook into passages at its level-2 headings.

    One passage per heading rather than a fixed chunk size: an operational document is
    already organised by symptom, and cutting across that boundary would produce passages
    that answer half a question.
    """
    text = path.read_text(encoding="utf-8")
    runbook = path.stem
    passages: list[Passage] = []
    current_title: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current_title is None:
            return
        body = "\n".join(buffer).strip()
        if body:
            passages.append(
                Passage(
                    runbook=runbook,
                    title=current_title,
                    text=body,
                    licenses=_parse_licenses(body),
                )
            )

    for line in text.splitlines():
        if line.startswith("## "):
            flush()
            current_title = line[3:].strip()
            buffer = []
        elif current_title is not None:
            buffer.append(line)
    flush()
    return passages


_LICENSE = re.compile(r"^\s*licensed-actions:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


def _parse_licenses(body: str) -> tuple[str, ...]:
    match = _LICENSE.search(body)
    if not match:
        return ()
    return tuple(sorted({a.strip() for a in match.group(1).split(",") if a.strip()}))


def load_runbooks(directory: Path) -> RunbookIndex:
    passages: list[Passage] = []
    for path in sorted(Path(directory).glob("*.md")):
        passages.extend(parse_runbook(path))
    return RunbookIndex(passages=passages)
