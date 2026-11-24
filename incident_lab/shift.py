"""Multi-incident shifts against one shared error budget.

A shift runs several incidents back-to-back — the scenarios listed on
the command line, in order — against a single shared error budget
instead of each incident's own SLO. The budget comes from one SLO
target (``--slo``, or the first scenario's SLO by default): the
monthly error budget is the downtime that SLO allows in a 30-day
month, and every incident's downtime burns part of that one budget.
Burn accumulates across incidents in the shift, so the second
incident starts with whatever the first one left.

When the cumulative burn reaches 100% the budget is exhausted and
the shift ends early: the remaining incidents are skipped, and the
shift score counts them as 0 — the shift scores down for spending
its whole month's budget part-way through. A shift that finishes
without exhausting the budget runs every incident it was given.

A shift's score is the mean of the points of every scheduled
incident, with skipped incidents counting as 0. Each incident is
still simulated exactly as `run` would simulate it — same policy,
same difficulty, same escalation chain — so a shift's per-incident
runs are ordinary SimulationResults, and `replay`, `debrief`,
`compare` and `stats` all understand a saved shift file.
"""

import statistics
from dataclasses import dataclass

from .difficulty import NORMAL
from .simulator import (
    PURGE_VERIFICATION_OFF, error_budget_minutes, simulate,
)


def _validate_slo(slo_percent) -> float:
    """A shift SLO must be a number from 90 up to (but not including)
    100: an SLO of 100% allows no downtime at all, so there is no
    budget to burn against, and anything below 90% is not an SLO
    worth tracking. Raises ValueError otherwise."""
    if (isinstance(slo_percent, bool)
            or not isinstance(slo_percent, (int, float))
            or not 90 <= slo_percent < 100):
        raise ValueError(
            "shift SLO must be a number from 90 up to (but not "
            f"including) 100, got {slo_percent!r}")
    return float(slo_percent)


@dataclass
class ShiftResult:
    # The incidents that actually ran, in shift order — each one a
    # full SimulationResult, exactly as `run` would produce it.
    runs: list
    # Scenario names that were scheduled but never ran because the
    # error budget ran out first.
    skipped: list
    # The shared SLO target in percent, and the monthly error budget
    # in minutes it allows (a 99.9% SLO allows 43.2m in a 30-day
    # month).
    slo_percent: float
    budget_minutes: float
    # True when the cumulative burn reached 100% and the shift ended
    # early with incidents still scheduled.
    budget_exhausted: bool
    # Cumulative burn against the shared budget, in percent, rounded
    # to one decimal; over 100 means the shift spent more than the
    # whole month's budget.
    total_burn_pct: float
    # Mean points over every scheduled incident, with skipped
    # incidents counting as 0 — so a shift that ends early scores
    # down for the incidents it never got to run.
    shift_points: float
    # One entry per incident that ran, in shift order: the scenario,
    # its points, its downtime in minutes (None when never
    # mitigated), the share of the shared budget it burned, and the
    # cumulative share burned after it.
    incidents: list
    # The policy every incident in the shift ran under.
    policy: object
    # Difficulty every incident was played at, or None when the shift
    # predates difficulties.
    difficulty: object = None
    # The --seed the shift was run with, or None for an unseeded
    # shift. Each incident uses seed + its position in the shift, so
    # the shift is reproducible and the incidents still vary.
    seed: object = None
    # Jitter range (lo, hi) in minutes applied to every incident, or
    # None for an unseeded shift.
    jitter: object = None
    # The rota week every incident was played for, or None when no
    # rota file was used.
    rota_week: object = None
    # The purge-verification mode every incident in the shift was
    # played with ("off" or "per-region").
    purge_verification: str = PURGE_VERIFICATION_OFF

    @property
    def scheduled(self) -> list:
        """Scenario names the shift was given, run or skipped."""
        return ([run.incident.id for run in self.runs]
                + list(self.skipped))

    def to_dict(self) -> dict:
        """JSON-ready dict: the shift summary, the policy, every run.

        The per-incident burn is recorded against the shared budget
        here, not against each scenario's own SLO — each run's own
        score still carries its own-SLO burn, unchanged.
        """
        return {
            "shift": {
                "slo_percent": self.slo_percent,
                "budget_minutes": self.budget_minutes,
                "budget_exhausted": self.budget_exhausted,
                "total_burn_pct": self.total_burn_pct,
                "shift_points": self.shift_points,
                "incidents": [dict(entry) for entry in self.incidents],
                "skipped": list(self.skipped),
                "difficulty": (self.difficulty.name
                               if self.difficulty is not None else None),
                "seed": self.seed,
                "jitter": (list(self.jitter)
                           if self.jitter is not None else None),
                "rota_week": self.rota_week,
                "purge_verification": self.purge_verification,
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
            "runs": [run.to_dict() for run in self.runs],
        }

    def render(self) -> str:
        """Human-readable summary of the shift."""
        lines = [
            f"shift: {len(self.runs)} incident(s) ran, "
            f"{len(self.skipped)} skipped",
            f"SLO:    {self.slo_percent}% "
            f"({self.budget_minutes:.1f}m monthly error budget)",
        ]
        if self.difficulty is not None:
            lines.append(f"difficulty: {self.difficulty.name}")
        if self.purge_verification != PURGE_VERIFICATION_OFF:
            lines.append(
                f"purge verification: {self.purge_verification}")
        if self.rota_week is not None:
            lines.append(f"rota week: {self.rota_week}")
        if self.seed is not None:
            lines.append(f"seed:     {self.seed}")
        lines.append("")
        for index, entry in enumerate(self.incidents):
            downtime = (f"{entry['downtime_min']}m"
                        if entry["downtime_min"] is not None else "-")
            lines.append(
                f"  {index + 1}. {entry['scenario']:<20} "
                f"{entry['points']:>3}/100  downtime {downtime:<5} "
                f"burn {entry['burn_pct']:>6.1f}%  "
                f"(cumulative {entry['cumulative_burn_pct']:.1f}%)")
        if self.skipped:
            lines.append("")
            lines.append("  skipped (error budget exhausted): "
                         + ", ".join(self.skipped))
        lines.append("")
        lines.append(
            f"shift points: {self.shift_points}/100 "
            f"(skipped incidents score 0)")
        if self.budget_exhausted:
            lines.append(
                f"error budget: EXHAUSTED — burned "
                f"{self.total_burn_pct:.1f}% of the monthly budget "
                f"(SLO {self.slo_percent}%)")
        else:
            lines.append(
                f"error budget: burned {self.total_burn_pct:.1f}% of "
                f"the monthly budget (SLO {self.slo_percent}%)")
        return "\n".join(lines)


def simulate_shift(scenarios, policy, slo_percent=None, difficulty=None,
                   seed=None, jitter=None, rota_week=None,
                   purge_verification=PURGE_VERIFICATION_OFF) -> ShiftResult:
    """Run several scenarios back-to-back against one shared budget.

    ``scenarios`` is the list of scenarios in shift order; ``policy``
    is the escalation policy every incident runs under. ``slo_percent``
    is the shared SLO target — when omitted it comes from the first
    scenario that carries one, and the shift fails when no scenario
    carries an SLO (a shared budget needs an SLO to measure against).
    ``difficulty`` defaults to normal. With ``seed`` given, incident
    ``i`` uses seed ``seed + i``, so the shift replays exactly while
    the incidents still vary. ``purge_verification`` ("off" or
    "per-region") is the mode every incident runs under: with
    "per-region" each incident verifies its fix in every region
    before it is declared mitigated. Raises ValueError on an empty
    scenario list, on a missing or out-of-range SLO, or on a bad
    seed/jitter (``simulate`` validates the jitter itself), and on
    "per-region" when any scheduled scenario has no regions to
    verify.
    """
    scenarios = list(scenarios)
    if not scenarios:
        raise ValueError("shift needs at least one scenario")
    if difficulty is None:
        difficulty = NORMAL
    if slo_percent is None:
        for scenario in scenarios:
            if scenario.slo_percent is not None:
                slo_percent = scenario.slo_percent
                break
        if slo_percent is None:
            raise ValueError(
                "shift needs an SLO: pass --slo, or use scenarios "
                "that carry one")
    slo_percent = _validate_slo(slo_percent)
    # Rounded to one decimal so the JSON stays readable: 99.9%
    # allows 43.2m, not 43.199999999997544m.
    budget_minutes = round(error_budget_minutes(slo_percent), 1)

    runs = []
    incidents = []
    skipped = []
    cumulative_burn = 0.0
    budget_exhausted = False
    for index, scenario in enumerate(scenarios):
        incident_seed = seed + index if seed is not None else None
        result = simulate(scenario, policy, seed=incident_seed,
                          jitter=jitter, difficulty=difficulty,
                          rota_week=rota_week,
                          purge_verification=purge_verification)
        runs.append(result)
        downtime = result.score.downtime_min
        burn = (round(downtime / budget_minutes * 100, 1)
                if downtime is not None else 0.0)
        cumulative_burn += (downtime / budget_minutes * 100
                            if downtime is not None else 0.0)
        incidents.append({
            "scenario": scenario.name,
            "points": result.score.points,
            "downtime_min": downtime,
            "burn_pct": burn,
            "cumulative_burn_pct": round(cumulative_burn, 1),
        })
        if cumulative_burn >= 100:
            # The budget is spent: the shift ends early, and the
            # incidents still scheduled are skipped, scoring 0.
            budget_exhausted = True
            skipped = [later.name for later in scenarios[index + 1:]]
            break
    points = [entry["points"] for entry in incidents]
    points.extend(0 for _ in skipped)
    shift_points = round(statistics.mean(points), 1)
    return ShiftResult(
        runs=runs,
        skipped=skipped,
        slo_percent=slo_percent,
        budget_minutes=budget_minutes,
        budget_exhausted=budget_exhausted,
        total_burn_pct=round(cumulative_burn, 1),
        shift_points=shift_points,
        incidents=incidents,
        policy=policy,
        difficulty=difficulty,
        seed=seed,
        jitter=(tuple(jitter) if jitter is not None else None),
        rota_week=rota_week,
        purge_verification=purge_verification,
    )
