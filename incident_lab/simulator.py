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
- the escalation chain is recorded on the result: one step per level
  walked, with the responder, the ack timeout, whether they acked or
  missed, and the impact in effect when they were paged — so a SEV-1
  whose impact grew while the page went unanswered shows that growth
  in the run JSON, and `replay`, `debrief`, `compare` and `stats` all
  see it
- error-budget burn: a scenario carries an SLO target (``slo_percent``,
  e.g. 99.9); the monthly error budget is the allowed downtime in a
  30-day month, and the incident's downtime (declaration to mitigated)
  is reported as a percentage of that budget — over 100% means this one
  incident spent the whole month's budget
"""

from dataclasses import dataclass, field
import random
import statistics

from .difficulty import NORMAL
from .incident import Incident
from .policy import EscalationPolicy
from .scenarios import DEFAULT_VERIFY_MIN_PER_REGION, Scenario
from .timeline import Timeline

ACK_GRACE_MIN = 5
MITIGATE_GRACE_MIN = 15
ESCALATION_PENALTY = 15
# A late ack earns partial credit that decays linearly to zero when the
# ack delay reaches this many times the level's ack timeout.
LATE_ACK_CREDIT_WINDOW = 2
# Minutes to verify a remediation in one region: fetch the asset (or
# run the check) from a cold edge node there and confirm the fix
# actually landed, instead of trusting the dashboard. A scenario can
# give a region its own cost (``Scenario.verify_delay_min_by_region``);
# a region with no cost of its own costs this. Verifying every region
# costs the sum of the regions' costs, so a four-region purge at the
# default costs 12m — the price of knowing no region is still serving
# the old state.
VERIFY_MIN_PER_REGION = DEFAULT_VERIFY_MIN_PER_REGION
# Purge-verification modes: "off" declares the incident mitigated as
# soon as the responder acts; "per-region" verifies the fix in every
# region of the scenario first (see Scenario.regions), adding each
# region's own verification cost — the scenario's
# ``verify_delay_min_by_region``, or VERIFY_MIN_PER_REGION where it
# gives none — to the mitigate delay.
PURGE_VERIFICATION_OFF = "off"
PURGE_VERIFICATION_PER_REGION = "per-region"
PURGE_VERIFICATION_MODES = (
    PURGE_VERIFICATION_OFF, PURGE_VERIFICATION_PER_REGION,
)


def parse_purge_verification(value) -> str:
    """Validate a purge-verification mode: "off" or "per-region".

    Raises ValueError on anything else, so a bad --verify-purge value
    fails the run the same way a bad --difficulty does.
    """
    if value not in PURGE_VERIFICATION_MODES:
        raise ValueError(
            "purge verification must be one of "
            f"{', '.join(PURGE_VERIFICATION_MODES)}, got {value!r}")
    return value


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


# Minutes in a 30-day month: the window an SLO's error budget is
# measured over. A 99.9% SLO allows 0.1% of 43,200 minutes — 43.2m
# of downtime a month — before the budget is spent.
MINUTES_PER_MONTH = 30 * 24 * 60


def error_budget_minutes(slo_percent: float) -> float:
    """Monthly error budget in minutes for an SLO target.

    The share of a 30-day month the service is allowed to be down:
    (100 - SLO)% of 43,200 minutes. A 99.9% SLO allows 43.2m, a
    99.99% SLO allows 4.32m.
    """
    return (100.0 - slo_percent) / 100.0 * MINUTES_PER_MONTH


def error_budget_burn_pct(slo_percent, downtime_min) -> object:
    """Share of the monthly error budget an incident burned, in percent.

    ``downtime_min`` is the minutes from declaration to mitigated.
    Returns None when the scenario has no SLO or the incident was
    never mitigated (no downtime to measure); over 100 means this one
    incident spent the whole month's budget. Rounded to one decimal so
    the JSON stays readable.
    """
    if slo_percent is None or downtime_min is None:
        return None
    budget = error_budget_minutes(slo_percent)
    return round(downtime_min / budget * 100, 1)


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
class EscalationStep:
    """One level of the escalation chain as it was actually walked.

    ``level`` is 1-based; ``outcome`` is ``"acked"`` or ``"missed"``;
    ``impact`` is the impact in effect when this level was paged —
    the scenario's impact stage at that escalation count, or ``""``
    when the scenario has no impact stages. A missed step's impact
    is the stage the incident had already reached, so the chain
    shows the impact growing level by level when pages go unanswered.
    """
    level: int
    responder_name: str
    responder_role: str
    ack_timeout_min: int
    paged_at_min: int
    outcome: str
    acked_at_min: object = None  # int, or None when the level missed
    ack_late_min: object = None  # int, or None when the level missed
    impact: str = ""

    def to_dict(self) -> dict:
        """JSON-ready dict; an unacked level serializes its ack fields
        as null."""
        return {
            "level": self.level,
            "responder": {
                "name": self.responder_name,
                "role": self.responder_role,
            },
            "ack_timeout_min": self.ack_timeout_min,
            "paged_at_min": self.paged_at_min,
            "outcome": self.outcome,
            "acked_at_min": self.acked_at_min,
            "ack_late_min": self.ack_late_min,
            "impact": self.impact,
        }


def impact_at(scenario: Scenario, escalations: int) -> str:
    """The impact in effect after ``escalations`` missed levels.

    Stage 0 is the impact when the incident is declared; each missed
    level moves to the next stage, and once the stages run out the
    last (worst) stage stays in effect. Scenarios without stages have
    no impact progression, so this is ``""`` for them.
    """
    stages = scenario.impact_stages
    if not stages:
        return ""
    return stages[min(escalations, len(stages) - 1)]


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
    # The service's SLO target in percent (99.9, say), or None when the
    # scenario carries no SLO.
    slo_percent: object = None
    # Minutes from declaration to mitigated — the service was down or
    # degraded for this long — or None when never mitigated.
    downtime_min: object = None
    # Share of the monthly error budget the downtime burned, in
    # percent (over 100 means the incident alone spent the whole
    # month's budget); None when there is no SLO or no downtime.
    error_budget_burn_pct: object = None
    # Minutes spent verifying the fix in every region before the
    # incident was declared mitigated (0 when purge verification was
    # off, or the scenario has no regions); part of
    # mitigate_delay_min, recorded separately so a what-if replay
    # can tell the verification time apart from the mitigation work.
    verify_delay_min: int = 0

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
            "slo_percent": self.slo_percent,
            "downtime_min": self.downtime_min,
            "error_budget_burn_pct": self.error_budget_burn_pct,
            "verify_delay_min": self.verify_delay_min,
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
    # The escalation chain as it was actually walked: one step per
    # level paged, with the responder, the ack timeout, whether they
    # acked or missed, and the impact in effect when they were paged.
    # Recorded in --json output; runs saved before 0.26.0 have no
    # chain and round-trip as an empty list.
    escalation_chain: list = field(default_factory=list)
    # The rota week the run was played for, or None when no rota
    # file was used. Recorded in --json output so a rota run can be
    # traced back to the week whose on-call rotation it used.
    rota_week: object = None
    # The regions the scenario's remediation must reach (the
    # scenario's `regions`, empty for scenarios with none), and the
    # purge-verification mode the run was played with ("off" or
    # "per-region"). Recorded in --json output so a what-if replay
    # can rebuild the scenario's regions and the verification the
    # base run used.
    regions: tuple = ()
    purge_verification: str = PURGE_VERIFICATION_OFF
    # Minutes verifying the fix cost in each region, by region — the
    # scenario's own per-region cost where it gives one, else the
    # default (see Scenario.verify_delay_min_for). Recorded in
    # --json output alongside the regions, so a what-if replay of a
    # run whose scenario priced regions differently replays those
    # same costs instead of falling back to the default.
    verify_delay_min_by_region: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """JSON-ready dict of the whole result: incident, policy, timeline, score."""
        return {
            "seed": self.seed,
            "jitter": (list(self.jitter) if self.jitter is not None
                       else None),
            "difficulty": (self.difficulty.name
                           if self.difficulty is not None else None),
            "rota_week": self.rota_week,
            "regions": list(self.regions),
            "verify_delay_min_by_region": dict(
                self.verify_delay_min_by_region),
            "purge_verification": self.purge_verification,
            "hints": [hint.to_dict() for hint in self.hints],
            "escalation_chain": [
                step.to_dict() for step in self.escalation_chain
            ],
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
    # The rota week every run in the batch was played for, or None
    # when no rota file was used.
    rota_week: object = None
    # The purge-verification mode every run in the batch was played
    # with ("off" or "per-region").
    purge_verification: str = PURGE_VERIFICATION_OFF

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
            "rota_week": self.rota_week,
            "purge_verification": self.purge_verification,
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
    rota_week: object = None,
    purge_verification: str = PURGE_VERIFICATION_OFF,
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
                     difficulty=difficulty, rota_week=rota_week,
                     purge_verification=purge_verification)
            for seed in seeds]
    first = runs[0]
    return BatchResult(first.incident, first.policy, runs,
                       jitter=first.jitter,
                       difficulty=first.difficulty,
                       rota_week=rota_week,
                       purge_verification=purge_verification)


def simulate(
    scenario: Scenario,
    policy: EscalationPolicy,
    max_duration_min: int = 240,
    seed: object = None,
    jitter: object = None,
    difficulty: object = None,
    hints: int = 0,
    rota_week: object = None,
    purge_verification: str = PURGE_VERIFICATION_OFF,
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

    ``purge_verification`` (``"off"`` or ``"per-region"``) decides
    whether the fix is verified in every region before the incident
    is declared mitigated: with ``"per-region"`` the responder
    confirms the remediation from a cold edge node in each of the
    scenario's regions, at that region's own cost (the scenario's
    ``verify_delay_min_by_region``, or the ``VERIFY_MIN_PER_REGION``
    default where it gives none), so the mitigate delay grows by
    the sum of the regions' costs and the timeline records one
    ``verified`` event per region. The verification time is part
    of the recorded mitigate delay and is also recorded on its own
    (``score.verify_delay_min``), so a what-if replay can tell it
    apart from the mitigation work. Raises ValueError on an unknown
    mode, or on ``"per-region"`` for a scenario with no regions to
    verify.
    """
    if difficulty is None:
        difficulty = NORMAL
    purge_verification = parse_purge_verification(purge_verification)
    if (purge_verification == PURGE_VERIFICATION_PER_REGION
            and not scenario.regions):
        raise ValueError(
            f"scenario {scenario.name!r} has no regions to verify: "
            "per-region purge verification needs a scenario with "
            "regions")
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
    # What verifying the fix costs in each region: the scenario's own
    # per-region cost where it gives one, else the default. Recorded
    # on the result whatever the mode, so a what-if replay of an
    # unverified run replays the same costs when verification is
    # turned on, instead of falling back to the default.
    verify_costs = {region: scenario.verify_delay_min_for(region)
                    for region in scenario.regions}
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
    chain = []

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
            chain.append(EscalationStep(
                level=i + 1,
                responder_name=level.responder.name,
                responder_role=level.responder.role,
                ack_timeout_min=level.ack_timeout_min,
                paged_at_min=t,
                outcome="acked",
                acked_at_min=acked_at,
                ack_late_min=ack_late_min,
                impact=impact_at(scenario, escalations),
            ))
            timeline.record(acked_at, "acked", acked_by, detail)
            break
        # Missed ack: wait out the timeout, then escalate (or give up).
        paged_at = t
        t += level.ack_timeout_min
        # The impact in effect when this level was paged, before the
        # miss made it worse.
        impact_before = impact_at(scenario, escalations)
        chain.append(EscalationStep(
            level=i + 1,
            responder_name=level.responder.name,
            responder_role=level.responder.role,
            ack_timeout_min=level.ack_timeout_min,
            paged_at_min=paged_at,
            outcome="missed",
            impact=impact_before,
        ))
        if i + 1 < len(policy.levels):
            escalations += 1
            detail = (f"no ack from {level.responder.name} in "
                      f"{level.ack_timeout_min}m")
            # A missed page can make the impact worse: say so on the
            # escalation event when the stage actually grew.
            impact_now = impact_at(scenario, escalations)
            if impact_now and impact_now != impact_before:
                detail += f"; impact now: {impact_now}"
            timeline.record(
                t, "escalated", policy.levels[i + 1].responder.name,
                detail,
            )
        else:
            timeline.record(t, "unacked", "monitor", "nobody acked the page")
            return SimulationResult(
                incident, policy, timeline,
                Score(None, None, None, escalations, False, 0,
                      hints_used=len(used_hints), hint_cost_points=hint_cost,
                      slo_percent=scenario.slo_percent),
                seed=seed, jitter=jitter_range, difficulty=difficulty,
                hints=used_hints, escalation_chain=chain,
                rota_week=rota_week,
                regions=tuple(scenario.regions),
                verify_delay_min_by_region=verify_costs,
                purge_verification=purge_verification,
            )
        if t > max_duration_min:
            timeline.record(t, "unacked", "monitor", "max duration reached")
            return SimulationResult(
                incident, policy, timeline,
                Score(None, None, None, escalations, False, 0,
                      hints_used=len(used_hints), hint_cost_points=hint_cost,
                      slo_percent=scenario.slo_percent),
                seed=seed, jitter=jitter_range, difficulty=difficulty,
                hints=used_hints, escalation_chain=chain,
                rota_week=rota_week,
                regions=tuple(scenario.regions),
                verify_delay_min_by_region=verify_costs,
                purge_verification=purge_verification,
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
    # Per-region verification happens inside the mitigation window,
    # before the incident is declared mitigated: one check per
    # region, at that region's own cost (the default where the
    # scenario gives none), recorded in order so each check lands
    # after the ones before it. The verification is a fixed
    # procedure, not jittered — the seed varies the response, not
    # the check — and it lengthens the mitigate delay, so the
    # downtime and the error-budget burn grow with it.
    if purge_verification == PURGE_VERIFICATION_PER_REGION:
        verify_delay = scenario.verify_delay_min_total()
        elapsed = 0
        for region in scenario.regions:
            elapsed += scenario.verify_delay_min_for(region)
            timeline.record(
                acked_at + elapsed,
                "verified", region,
                f"fix confirmed in {region} "
                f"({scenario.verify_delay_min_for(region)}m)")
    else:
        verify_delay = 0
    mitigate_delay += verify_delay
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
        slo_percent=scenario.slo_percent,
        # Downtime runs from declaration (t=0) to mitigated: the
        # service was down or degraded for that whole span.
        downtime_min=mitigated_at,
        error_budget_burn_pct=error_budget_burn_pct(
            scenario.slo_percent, mitigated_at),
        verify_delay_min=verify_delay,
    )
    return SimulationResult(incident, policy, timeline, score, seed=seed,
                            jitter=jitter_range, difficulty=difficulty,
                            hints=used_hints, escalation_chain=chain,
                            rota_week=rota_week,
                            regions=tuple(scenario.regions),
                            verify_delay_min_by_region=verify_costs,
                            purge_verification=purge_verification)
