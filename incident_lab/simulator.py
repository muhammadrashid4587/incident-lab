"""Deterministic incident-response simulation over a simulated clock.

The clock runs in minutes and never sleeps: the whole incident plays out
instantly. The engine pages each responder in policy order, waits for an
ack up to that level's timeout, escalates on a miss, and once someone acks
it walks through mitigate and resolve.

Scoring (0-100, documented in README.md):
- start at 100
- minus 15 per escalation
- minus 1 per minute of time-to-ack past the difficulty's ack grace
  (5 minutes on normal)
- minus 1 per 2 minutes of time-to-mitigate past the difficulty's
  mitigate grace (15 minutes on normal)
- an incident nobody acks scores 0
- a late ack (after its level's timeout) still counts, but the score is
  scaled by partial credit: full credit up to the timeout, then linear
  decay to zero at twice the timeout
- minus HINT_COST_POINTS per hint asked for (`run --hints N`), after the
  late-ack credit scaling, floored at 0
"""

from dataclasses import dataclass, field
import random
import statistics

from .difficulty import NORMAL
from .incident import Incident
from .policy import EscalationPolicy
from .scenarios import Scenario
from .timeline import Timeline

ACK_GRACE_MIN = 5
MITIGATE_GRACE_MIN = 15
ESCALATION_PENALTY = 15
# A late ack earns partial credit that decays linearly to zero when the
# ack delay reaches this many times the level's ack timeout.
LATE_ACK_CREDIT_WINDOW = 2
# Points one hint costs. Hints are the scenario's known-good next actions,
# asked for with `run --hints N`; they are guidance, not a free answer,
# so each one comes off the final score.
HINT_COST_POINTS = 5
# With --seed, each ack delay is jittered by a magnitude drawn from this
# range (minutes) with a random sign, from a seeded RNG, so the same
# seed replays the same run while different seeds vary the response
# times. The jitter is small enough that a scenario's structure (who
# acks, who misses) never changes. --jitter LO-HI overrides the range.
# The same jitter, from the same RNG, also applies to the mitigate and
# resolve delays once someone acks, so the whole incident varies with
# the seed while staying reproducible.
DEFAULT_JITTER_RANGE = (0, 1)


def compute_points(*, escalations: int, acked_at, ack_credit: float,
                   mitigated_at, difficulty, hint_cost: int) -> int:
    """Score a run from the milestones it reached, the run's own rule.

    Shared by `simulate` and the interactive drill, so a drill that
    makes the same decisions as a `run` scores the same: start at 100,
    minus 15 per escalation, minus 1 per minute of time-to-ack past
    the difficulty's ack grace, minus 1 per 2 minutes of
    time-to-mitigate past the mitigate grace, scaled by the ack credit
    (1.0 for an on-time ack), minus the hint cost, floored at 0. A run
    nobody acked scores 0; a run that stopped early (a drill ended
    with quit or EOF) is scored on the milestones it reached — a
    milestone never reached costs nothing, because it never happened.
    """
    if acked_at is None:
        return 0
    points = 100
    points -= ESCALATION_PENALTY * escalations
    points -= max(0, acked_at - difficulty.ack_grace_min)
    if mitigated_at is not None:
        points -= max(0, mitigated_at - difficulty.mitigate_grace_min) // 2
    # A late ack keeps only part of the score: the effective score is
    # the base score scaled by the ack credit (1.0 for an on-time ack).
    # Hints come off after that scaling, so asking for help costs the
    # same whether the ack was on time or late.
    return max(0, round(points * ack_credit) - hint_cost)


def _jitter_delay(rng, delay_min: int, jitter_range: tuple) -> int:
    """Shift a delay by a magnitude in ``jitter_range`` with a random sign.

    Drawn from the seeded ``rng``; never below 1 minute, so a step can
    shrink but never vanish or run backwards.
    """
    magnitude = rng.randint(jitter_range[0], jitter_range[1])
    return max(1, delay_min + rng.choice((-1, 1)) * magnitude)


def late_ack_credit(ack_delay_min: int, ack_timeout_min: int) -> float:
    """Partial credit for an ack that arrives after its level's timeout.

    Full credit (1.0) up to the timeout, then linear decay to zero at
    ``LATE_ACK_CREDIT_WINDOW`` times the timeout; an ack later than that
    still counts as an ack but earns no credit.
    """
    if ack_delay_min <= ack_timeout_min:
        return 1.0
    late_min = ack_delay_min - ack_timeout_min
    return max(0.0, 1.0 - late_min / ack_timeout_min)


@dataclass(frozen=True)
class Hint:
    """One hint the operator asked for during the run.

    The text is one of the scenario's known-good next actions, drawn in
    order; ``cost_points`` is what it took off the final score.
    """
    text: str
    cost_points: int = HINT_COST_POINTS

    def to_dict(self) -> dict:
        return {"text": self.text, "cost_points": self.cost_points}


@dataclass(frozen=True)
class Score:
    time_to_ack_min: object  # int, or None if nobody acked
    time_to_mitigate_min: object  # int, or None if never mitigated
    time_to_resolve_min: object  # int, or None if never resolved
    escalations: int
    resolved: bool
    points: int  # 0..100
    # Minutes past the acking level's timeout (0 for an on-time ack),
    # or None if nobody acked.
    ack_late_min: object = None
    # Fraction of the score a late ack keeps: 1.0 on time, decaying
    # linearly to 0.0 at twice the timeout; None if nobody acked.
    ack_credit: object = None
    # Minutes from ack to mitigated, and from mitigated to resolved
    # (the effective delays, after any seeded jitter); None if the
    # incident was never mitigated / resolved.
    mitigate_delay_min: object = None
    resolve_delay_min: object = None
    # Hints asked for during the run (0 if none) and what they cost in
    # total; the cost is already deducted from ``points``.
    hints_used: int = 0
    hint_cost_points: int = 0

    def to_dict(self) -> dict:
        """JSON-ready dict; unreached milestones serialize as null."""
        return {
            "time_to_ack_min": self.time_to_ack_min,
            "time_to_mitigate_min": self.time_to_mitigate_min,
            "time_to_resolve_min": self.time_to_resolve_min,
            "escalations": self.escalations,
            "resolved": self.resolved,
            "points": self.points,
            "ack_late_min": self.ack_late_min,
            "ack_credit": self.ack_credit,
            "mitigate_delay_min": self.mitigate_delay_min,
            "resolve_delay_min": self.resolve_delay_min,
            "hints_used": self.hints_used,
            "hint_cost_points": self.hint_cost_points,
        }


@dataclass
class SimulationResult:
    incident: Incident
    policy: EscalationPolicy
    timeline: Timeline
    score: Score
    # RNG seed used to jitter ack delays, or None for the fixed scenario
    # delays. Recorded so a seeded run can be reproduced exactly.
    seed: object = None
    # Jitter range (lo, hi) in minutes applied to ack delays, or None
    # for an unseeded run. Recorded alongside the seed for replay.
    jitter: object = None
    # Difficulty the run was played at, or None for a saved run from
    # before difficulties existed. Recorded in --json output.
    difficulty: object = None
    # Hints the operator asked for during the run, in the order asked
    # (empty when none were asked). Recorded in --json output.
    hints: list = field(default_factory=list)

    def to_dict(self) -> dict:
        """JSON-ready dict of the whole result: incident, policy, timeline, score."""
        return {
            "seed": self.seed,
            "jitter": (list(self.jitter) if self.jitter is not None
                       else None),
            "difficulty": (self.difficulty.name
                           if self.difficulty is not None else None),
            "hints": [hint.to_dict() for hint in self.hints],
            "incident": {
                "id": self.incident.id,
                "service": self.incident.service,
                "title": self.incident.title,
                "severity": self.incident.severity,
                "description": self.incident.description,
            },
            "policy": {
                "name": self.policy.name,
                "levels": [
                    {
                        "responder": {
                            "name": level.responder.name,
                            "role": level.responder.role,
                        },
                        "ack_timeout_min": level.ack_timeout_min,
                    }
                    for level in self.policy.levels
                ],
            },
            "timeline": self.timeline.to_dict(),
            "score": self.score.to_dict(),
        }


@dataclass
class BatchResult:
    """The result of running one scenario across several seeds.

    Each run is a full SimulationResult for one seed, so a batch keeps
    every timeline; the score distribution summarises the points across
    runs so a batch answers "how much does this response vary?" at a
    glance.
    """
    incident: Incident
    policy: EscalationPolicy
    runs: list  # one SimulationResult per seed, in seed order
    # Jitter range (lo, hi) in minutes applied to every run, or None
    # for an unseeded batch (a batch always has seeds, so this is the
    # effective range, defaulting to DEFAULT_JITTER_RANGE).
    jitter: object = None
    # Difficulty every run in the batch was played at.
    difficulty: object = None

    @property
    def seeds(self) -> list:
        return [run.seed for run in self.runs]

    @property
    def points(self) -> list:
        return [run.score.points for run in self.runs]

    def score_distribution(self) -> dict:
        """Mean/min/max/median points across runs, plus per-seed scores."""
        points = self.points
        return {
            "mean": statistics.mean(points),
            "min": min(points),
            "max": max(points),
            "median": statistics.median(points),
            "per_seed": [
                {"seed": run.seed, "points": run.score.points}
                for run in self.runs
            ],
        }

    def to_dict(self) -> dict:
        """JSON-ready dict: incident, policy, every run, distribution."""
        # Incident and policy are the same for every run, so reuse the
        # first run's rendering instead of duplicating the shape.
        first = self.runs[0].to_dict()
        return {
            "seeds": self.seeds,
            "jitter": (list(self.jitter) if self.jitter is not None
                       else None),
            "difficulty": (self.difficulty.name
                           if self.difficulty is not None else None),
            "incident": first["incident"],
            "policy": first["policy"],
            "runs": [run.to_dict() for run in self.runs],
            "score_distribution": self.score_distribution(),
        }


def simulate_batch(
    scenario: Scenario,
    policy: EscalationPolicy,
    seeds: list,
    jitter: object = None,
    difficulty: object = None,
) -> BatchResult:
    """Run one scenario against one policy once per seed.

    Each seed gets its own seeded run (same jitter range for all), so
    the batch is reproducible for the same seed list. Raises
    ValueError on an empty seed list.
    """
    seeds = list(seeds)
    if not seeds:
        raise ValueError("batch needs at least one seed")
    runs = [simulate(scenario, policy, seed=seed, jitter=jitter,
                     difficulty=difficulty)
            for seed in seeds]
    first = runs[0]
    return BatchResult(first.incident, first.policy, runs,
                       jitter=first.jitter,
                       difficulty=first.difficulty)


def simulate(
    scenario: Scenario,
    policy: EscalationPolicy,
    max_duration_min: int = 240,
    seed: object = None,
    jitter: object = None,
    difficulty: object = None,
    hints: int = 0,
) -> SimulationResult:
    """Run one scenario against one policy and return the full result.

    With ``seed`` given, each ack delay is jittered by a magnitude drawn
    from ``jitter`` (a ``(lo, hi)`` pair of minutes, defaulting to the
    difficulty's default range — normal: ``DEFAULT_JITTER_RANGE``) with
    a random sign, drawn from a ``random.Random`` seeded with ``seed`` — so
    the same seed replays the same run exactly while different seeds
    vary the response times. A miss (``None`` delay) stays a miss, so the
    scenario's escalation structure is unchanged. The same seeded jitter
    also applies to the mitigate and resolve delays once someone acks,
    drawn from the same RNG after the ack draws, so the whole incident
    varies with the seed and stays reproducible; the effective delays
    are recorded in the score (``mitigate_delay_min`` /
    ``resolve_delay_min``). Without a seed the scenario's fixed delays
    are used and ``jitter`` is ignored.

    ``difficulty`` (a ``Difficulty``, defaulting to normal) scales the
    scoring targets: the ack and mitigate grace minutes come from the
    difficulty, so the same incident scores differently at different
    difficulties. The difficulty is recorded on the result and in its
    ``--json`` output.

    ``hints`` asks for that many hints during the run: the operator asks
    for help while triaging, and the hints are drawn in order from the
    scenario's known-good next actions. Each hint costs
    ``HINT_COST_POINTS`` off the final score (after the late-ack credit
    scaling, floored at 0), and is recorded on the result and in its
    ``--json`` output. Raises ValueError if ``hints`` is negative or
    more than the scenario has.
    """
    if difficulty is None:
        difficulty = NORMAL
    if isinstance(hints, bool) or not isinstance(hints, int) or hints < 0:
        raise ValueError("hints must be a non-negative integer")
    if hints > len(scenario.hints):
        raise ValueError(
            f"scenario {scenario.name!r} has only "
            f"{len(scenario.hints)} hint(s), asked for {hints}")
    used_hints = [Hint(text) for text in scenario.hints[:hints]]
    hint_cost = len(used_hints) * HINT_COST_POINTS
    if seed is not None:
        if jitter is None:
            jitter_range = difficulty.default_jitter_range
        else:
            jitter_range = tuple(jitter)
            if (len(jitter_range) != 2
                    or any(isinstance(v, bool) or not isinstance(v, int)
                           for v in jitter_range)
                    or jitter_range[0] < 0 or jitter_range[1] < 0
                    or jitter_range[0] > jitter_range[1]):
                raise ValueError(
                    "jitter must be a (lo, hi) pair of non-negative "
                    "integers with lo <= hi")
    else:
        jitter_range = None
    incident = Incident(
        id=scenario.name,
        service=scenario.service,
        title=scenario.title,
        severity=scenario.severity,
        description=scenario.description,
    )
    timeline = Timeline()
    timeline.record(0, "declared", "monitor", scenario.title)
    rng = random.Random(seed) if seed is not None else None

    t = 0
    escalations = 0
    acked_at = None
    acked_by = None

    for i, level in enumerate(policy.levels):
        timeline.record(t, "paged", level.responder.name, level.responder.role)
        delay = scenario.ack_delay_min[i] if i < len(scenario.ack_delay_min) else None
        if delay is not None and rng is not None:
            # Jitter the ack delay by a magnitude in the jitter range
            # with a random sign, never below 1 minute.
            delay = _jitter_delay(rng, delay, jitter_range)
        if delay is not None:
            # The responder acks, on time or late. A late ack still counts:
            # the incident proceeds, but the score is scaled by partial
            # credit that decays linearly to zero at twice the timeout.
            ack_late_min = max(0, delay - level.ack_timeout_min)
            ack_credit = late_ack_credit(delay, level.ack_timeout_min)
            acked_at = t + delay
            acked_by = level.responder.name
            detail = ""
            if ack_late_min:
                detail = (f"late by {ack_late_min}m "
                          f"(ack credit {ack_credit:.0%})")
            timeline.record(acked_at, "acked", acked_by, detail)
            break
        # Missed ack: wait out the timeout, then escalate (or give up).
        t += level.ack_timeout_min
        if i + 1 < len(policy.levels):
            escalations += 1
            timeline.record(
                t, "escalated", policy.levels[i + 1].responder.name,
                f"no ack from {level.responder.name} in "
                f"{level.ack_timeout_min}m",
            )
        else:
            timeline.record(t, "unacked", "monitor", "nobody acked the page")
            return SimulationResult(
                incident, policy, timeline,
                Score(None, None, None, escalations, False, 0,
                      hints_used=len(used_hints), hint_cost_points=hint_cost),
                seed=seed, jitter=jitter_range, difficulty=difficulty,
                hints=used_hints,
            )
        if t > max_duration_min:
            timeline.record(t, "unacked", "monitor", "max duration reached")
            return SimulationResult(
                incident, policy, timeline,
                Score(None, None, None, escalations, False, 0,
                      hints_used=len(used_hints), hint_cost_points=hint_cost),
                seed=seed, jitter=jitter_range, difficulty=difficulty,
                hints=used_hints,
            )

    if rng is not None:
        # Jitter the mitigate and resolve delays from the same seeded
        # RNG (after the ack draws), so the whole incident varies with
        # the seed and stays reproducible for it.
        mitigate_delay = _jitter_delay(rng, scenario.mitigate_delay_min,
                                       jitter_range)
        resolve_delay = _jitter_delay(rng, scenario.resolve_delay_min,
                                      jitter_range)
    else:
        mitigate_delay = scenario.mitigate_delay_min
        resolve_delay = scenario.resolve_delay_min
    mitigated_at = acked_at + mitigate_delay
    resolved_at = mitigated_at + resolve_delay
    timeline.record(mitigated_at, "mitigated", acked_by)
    timeline.record(resolved_at, "resolved", acked_by)

    points = compute_points(
        escalations=escalations, acked_at=acked_at, ack_credit=ack_credit,
        mitigated_at=mitigated_at, difficulty=difficulty,
        hint_cost=hint_cost)
    score = Score(
        time_to_ack_min=acked_at,
        time_to_mitigate_min=mitigated_at,
        time_to_resolve_min=resolved_at,
        escalations=escalations,
        resolved=True,
        points=points,
        ack_late_min=ack_late_min,
        ack_credit=ack_credit,
        mitigate_delay_min=mitigate_delay,
        resolve_delay_min=resolve_delay,
        hints_used=len(used_hints),
        hint_cost_points=hint_cost,
    )
    return SimulationResult(incident, policy, timeline, score, seed=seed,
                            jitter=jitter_range, difficulty=difficulty,
                            hints=used_hints)
