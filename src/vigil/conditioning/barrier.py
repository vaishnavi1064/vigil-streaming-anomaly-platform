"""Holding a verdict until the evidence for it has arrived (G-7).

Conditioning asks a question about several channels at once: did this channel's in-scope
siblings move at the same moment? Answering it the instant an episode closes answers it
against whatever happened to be in the index by then, which is a function of the order
episodes closed in rather than of the world. Measured over 76 episodes in v3, the
corroboration test returned `isolated` **zero** times -- not because siblings always moved,
but because the ones that would have exonerated a channel had usually not closed yet when
its turn came. No discriminator can separate populations from data that has not arrived.

So the verdict is delayed behind a barrier in **event time**. An episode is held until the
fleet watermark -- the minimum across channels, so the slowest channel governs -- has passed
its onset by a fixed buffer. By then every sibling window covering that onset has closed and
been scored, and the corroboration index holds the same evidence whichever order the
episodes happened to close in.

Event time, not processing time, and this is the whole point. A processing-time delay says
"wait 30 seconds of wall clock", which on a replay of an hour of history waits for nothing
at all and on a backlogged live stream waits for the wrong span. An event-time barrier is a
statement about the data: everything up to this point in the stream's own clock has been
seen.

The cost is latency, and it is real: an episode is now raised at least `buffer_ms` of event
time after it began. That is the trade G-7 named, taken deliberately, and it is measured
rather than assumed -- `hold_report()` prints the distribution of how long episodes actually
waited.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vigil.episodes import Episode


@dataclass(frozen=True, slots=True)
class HeldEpisode:
    """A closed episode waiting for its corroboration evidence to be complete."""

    episode: Episode
    release_at_ms: int
    # Fleet event time when this episode closed. The difference between this and the
    # watermark that releases it is what the barrier actually cost -- which is not the
    # buffer, because an episode long enough to outlast its own buffer waits for nothing.
    held_at_ms: int | None = None


@dataclass
class CorroborationBarrier:
    """Delays conditioning verdicts until the sibling evidence for them has closed.

    `buffer_ms` is measured from the episode's **onset**, not from its end, because the
    evidence that matters is what other channels did around the moment this one departed.
    A long episode is therefore released as soon as it closes: by then the buffer has
    already elapsed in event time and there is nothing left to wait for.
    """

    buffer_ms: int = 30_000
    # A channel that has stopped publishing must not hold every other channel's verdict
    # forever. ADR-019's idleness rule, applied to the fleet watermark this barrier reads.
    idle_ms: int = 60_000

    _held: list[HeldEpisode] = field(default_factory=list, init=False, repr=False)
    held_total: int = field(default=0, init=False)
    released_on_watermark: int = field(default=0, init=False)
    released_on_flush: int = field(default=0, init=False)
    _holds_ms: list[int] = field(default_factory=list, init=False, repr=False)

    def hold(self, episode: Episode, watermark_ms: int | None = None) -> None:
        self._held.append(
            HeldEpisode(
                episode=episode,
                release_at_ms=episode.began_ms + self.buffer_ms,
                held_at_ms=watermark_ms,
            )
        )
        self.held_total += 1

    def release(self, watermark_ms: int | None) -> list[Episode]:
        """Every held episode whose evidence window has now closed, in onset order."""
        if watermark_ms is None or not self._held:
            return []
        ready = [h for h in self._held if h.release_at_ms <= watermark_ms]
        if not ready:
            return []
        self._held = [h for h in self._held if h.release_at_ms > watermark_ms]
        ready.sort(key=lambda h: h.episode.began_ms)
        self.released_on_watermark += len(ready)
        for h in ready:
            if h.held_at_ms is not None:
                self._holds_ms.append(max(0, watermark_ms - h.held_at_ms))
        return [h.episode for h in ready]

    def flush(self) -> list[Episode]:
        """Release everything still held. For shutdown and bounded runs.

        A bounded replay ends with the barrier full by construction -- the last episodes of
        the stream have no data after them to advance the watermark past their buffer. Those
        episodes are decided on the evidence that exists, which is all there will ever be,
        and they are counted separately so the two populations stay distinguishable.
        """
        out = sorted(self._held, key=lambda h: h.episode.began_ms)
        self._held = []
        self.released_on_flush += len(out)
        return [h.episode for h in out]

    @property
    def pending(self) -> int:
        return len(self._held)

    def hold_report(self) -> str:
        """What the barrier actually cost, in event time, rather than what it was set to."""
        if not self._holds_ms:
            return (
                f"verdict barrier: buffer {self.buffer_ms / 1000:g}s | "
                f"held {self.held_total:,} | released on flush {self.released_on_flush:,}"
            )
        ordered = sorted(self._holds_ms)
        p50 = ordered[len(ordered) // 2]
        p95 = ordered[min(int(len(ordered) * 0.95), len(ordered) - 1)]
        delayed = sum(1 for h in ordered if h > 0)
        return (
            f"verdict barrier: buffer {self.buffer_ms / 1000:g}s | "
            f"held {self.held_total:,} | released on watermark {self.released_on_watermark:,} "
            f"| on flush {self.released_on_flush:,} | delayed past episode close "
            f"{delayed:,} of {len(ordered):,}, event-time p50 {p50 / 1000:.1f}s "
            f"p95 {p95 / 1000:.1f}s max {ordered[-1] / 1000:.1f}s"
        )
