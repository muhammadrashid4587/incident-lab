"""What-if replay: re-run a saved incident with different parameters.

A saved `run --json` run records the whole incident: the policy and its
timeouts, the timeline, the score, and (since 0.11.0) the effective
mitigate and resolve delays. That is enough to rebuild the incident's
actual behaviour — how long each responder took to ack, or that they
never acked — without the original scenario file:

- the acking level's delay is the time-to-ack minus the timeouts of the
  levels that missed before it (a miss stays a miss: a timeout only
  decides how long the page waits, never whether the responder acks),
- the mitigate and resolve delays are the gaps between the ack,
  mitigate and resolve times in the saved score.

The what-if then re-simulates that behaviour with overridden
parameters — different ack timeouts, a different difficulty, or a
different named policy — and shows how the score and grade would
have changed. The recorded delays are replayed exactly as they
happened, so a seeded run's jitter is already baked in and is not
drawn again; with no overrides the what-if reproduces the original
run exactly. The original run file is never modified: the what-if
result lives only in memory (or in the `--json` output).
"""

from dataclasses import dataclass, replace

from .debrief import grade
from .difficulty import NORMAL
from .policy import parse_ack_timeout_overrides
from .scenarios import Scenario
from .simulator import (
    PURGE_VERIFICATION_OFF, parse_purge_verification, simulate,
)


def _scenario_from_result(original) -> Scenario:
    """Rebuild the incident's behaviour from a saved run.

    The ack delay list has one entry per policy level: ``None`` for
    every level that missed, the derived delay for the level that
    acked. Mitigate and resolve delays come from the gaps between the
    saved milestones. Raises ValueError if the saved run is
    inconsistent (for example, a time-to-ack shorter than the
    timeouts it must contain).
    """
    score = original.score
    policy = original.policy
    if score.time_to_ack_min is None:
        # Nobody acked: every level missed, and the mitigate / resolve
        # delays never ran, so they are unused.
        ack_delays = tuple(None for _ in policy.levels)
        mitigate_delay = 0
        resolve_delay = 0
    else:
        missed = score.escalations
        if missed < 0 or missed >= len(policy.levels):
            raise ValueError(
                "what-if: saved run has an impossible escalation count "
                f"({missed}) for a {len(policy.levels)}-level policy")
        waited = sum(level.ack_timeout_min
                     for level in policy.levels[:missed])
        ack_delay = score.time_to_ack_min - waited
        if ack_delay < 1:
            raise ValueError(
                "what-if: saved run's time-to-ack "
                f"({score.time_to_ack_min}m) is shorter than the "
                f"timeouts it must contain ({waited}m)")
        ack_delays = (
            tuple(None for _ in range(missed))
            + (ack_delay,)
            + tuple(None for _ in policy.levels[missed + 1:])
        )
        if score.time_to_mitigate_min is None:
            raise ValueError(
                "what-if: saved run acked but never mitigated")
        if score.time_to_resolve_min is None:
            raise ValueError(
                "what-if: saved run acked but never resolved")
        mitigate_delay = score.time_to_mitigate_min - score.time_to_ack_min
        resolve_delay = score.time_to_resolve_min - score.time_to_mitigate_min
        if mitigate_delay < 0 or resolve_delay < 0:
            raise ValueError(
                "what-if: saved run's milestones run backwards")
        # The recorded mitigate delay includes any per-region
        # verification time the base run spent; the scenario's own
        # mitigate delay is what is left once that is taken out, so
        # the override can re-apply verification (or not) cleanly
        # instead of stacking it on top of the base run's.
        verify_delay = score.verify_delay_min or 0
        mitigate_delay -= verify_delay
        if mitigate_delay < 1:
            raise ValueError(
                "what-if: saved run's mitigate delay "
                f"({score.time_to_mitigate_min - score.time_to_ack_min}m) "
                "is shorter than the verification time it records "
                f"({verify_delay}m)")
    return Scenario(
        name=original.incident.id,
        service=original.incident.service,
        title=original.incident.title,
        severity=original.incident.severity,
        description=original.incident.description,
        ack_delay_min=ack_delays,
        mitigate_delay_min=mitigate_delay,
        resolve_delay_min=resolve_delay,
        hints=tuple(hint.text for hint in original.hints),
        slo_percent=score.slo_percent,
        # The regions the base run's scenario carried, so a
        # per-region verification replay has regions to verify; a
        # run saved before 0.43.0 has none, and a verification
        # override on it fails in simulate() instead of inventing
        # regions.
        regions=tuple(original.regions),
        # The per-region verification costs the base run's scenario
        # carried, so a replay re-verifies at those same costs: a
        # run saved before 0.44.0 has none, and the replay falls
        # back to the default per region, which is what 0.43.0
        # charged.
        verify_delay_min_by_region=dict(
            original.verify_delay_min_by_region),
        # The impact stages the original run walked through, read off
        # its recorded escalation chain: one stage per level paged,
        # in order. The replay walks the same levels (a miss stays a
        # miss), so these are exactly the stages it needs; a run
        # saved before chains existed has none, and a replay under a
        # longer policy keeps the last (worst) stage past the end.
        impact_stages=tuple(
            step.impact for step in original.escalation_chain
            if step.impact),
    )


def _scale_timeouts(policy, factor: float):
    """Scale a policy's ack timeouts by ``factor`` (rounded, at least 1m).

    Mirrors ``difficulty.apply_difficulty`` but takes the factor
    directly, so a what-if can scale the recorded timeouts relative
    to the original run's difficulty instead of from a base policy.
    """
    levels = tuple(
        replace(level, ack_timeout_min=max(
            1, round(level.ack_timeout_min * factor)))
        for level in policy.levels
    )
    return replace(policy, levels=levels)


@dataclass
class WhatIfResult:
    """The original run next to its what-if replay."""

    original: object  # SimulationResult, as saved
    replayed: object  # SimulationResult, re-simulated with overrides
    # Human-readable descriptions of the overrides, in the order
    # applied, e.g. "difficulty: normal -> hard".
    overrides: list

    @property
    def points_delta(self) -> int:
        """How many points the what-if gained (or lost) vs the original."""
        return self.replayed.score.points - self.original.score.points

    @property
    def grade_delta(self) -> str:
        """Grade change, e.g. "D -> A", or "A (unchanged)"."""
        before = grade(self.original.score.points)
        after = grade(self.replayed.score.points)
        if before == after:
            return f"{after} (unchanged)"
        return f"{before} -> {after}"

    def to_dict(self) -> dict:
        """JSON-ready dict: both runs, the overrides, and the deltas."""
        return {
            "original": self.original.to_dict(),
            "what_if": self.replayed.to_dict(),
            "overrides": list(self.overrides),
            "points_delta": self.points_delta,
            "grade": grade(self.original.score.points),
            "what_if_grade": grade(self.replayed.score.points),
            "grade_delta": self.grade_delta,
        }

    def render(self) -> str:
        """Human-readable what-if report: the change, then the replay."""
        lines = [
            f"what-if: {self.original.incident.title} "
            f"[{self.original.incident.severity}]",
            f"service:  {self.original.incident.service}",
        ]
        for override in self.overrides:
            lines.append(f"override: {override}")
        lines.append("")
        lines.append("score:")
        lines.append(
            f"  points:           {self.original.score.points}/100 "
            f"(grade {grade(self.original.score.points)}) -> "
            f"{self.replayed.score.points}/100 "
            f"(grade {grade(self.replayed.score.points)})")
        delta = self.points_delta
        lines.append(
            f"  delta:            {'+' if delta >= 0 else ''}{delta} points")
        lines.append(
            f"  time-to-ack:      {_fmt_maybe(self.original.score.time_to_ack_min)}"
            f" -> {_fmt_maybe(self.replayed.score.time_to_ack_min)}")
        lines.append(
            f"  time-to-mitigate: {_fmt_maybe(self.original.score.time_to_mitigate_min)}"
            f" -> {_fmt_maybe(self.replayed.score.time_to_mitigate_min)}")
        if (self.original.score.verify_delay_min
                or self.replayed.score.verify_delay_min):
            lines.append(
                f"  verify delay:     "
                f"{self.original.score.verify_delay_min}m"
                f" -> {self.replayed.score.verify_delay_min}m")
        lines.append(
            f"  escalations:      {self.original.score.escalations} "
            f"-> {self.replayed.score.escalations}")
        if (self.original.score.error_budget_burn_pct is not None
                or self.replayed.score.error_budget_burn_pct is not None):
            lines.append(
                "  error budget:     "
                f"{_fmt_burn(self.original.score.error_budget_burn_pct)}"
                f" -> {_fmt_burn(self.replayed.score.error_budget_burn_pct)}"
                " of the monthly budget")
        lines.append("")
        lines.append("what-if timeline:")
        lines.append(self.replayed.timeline.render())
        return "\n".join(lines)


def _fmt_maybe(value) -> str:
    return f"{value}m" if value is not None else "-"


def _fmt_burn(value) -> str:
    return f"{value:.1f}%" if value is not None else "-"


def what_if(original, ack_timeout=None, ack_timeouts=None,
            difficulty=None, policy=None,
            purge_verification=None) -> WhatIfResult:
    """Re-simulate a saved run with overridden parameters.

    ``ack_timeout`` sets every level's timeout to that many minutes;
    ``ack_timeouts`` is the ``--ack-timeouts`` token list (a positional
    list in level order, or named overrides such as ``primary=120``);
    ``difficulty`` replays the run at that difficulty; ``policy`` swaps
    in a different escalation policy (an EscalationPolicy, resolved
    from a name by the caller); ``purge_verification`` ("off" or
    "per-region") replays the run with the fix verified in every
    region before the incident is declared mitigated, or without
    that verification — the recorded mitigate delay is split back
    into the mitigation work and the verification time the base run
    spent, so the override re-applies verification cleanly instead
    of stacking it. At most one of ``ack_timeout`` and
    ``ack_timeouts`` may be given. Difficulty scales the recorded
    timeouts relative to the original run's difficulty (the saved
    timeouts already carry the original difficulty's scaling), and the
    scoring targets come from the new difficulty; an explicit ack
    timeout override still wins for the levels it sets, exactly as in
    ``run``.

    A swapped-in policy's timeouts are base values, so they are first
    scaled by the original run's difficulty: the replay stays at the
    difficulty the run was played at unless ``difficulty`` is also
    given (the same as ``run --policy X --difficulty D``). Behaviour
    is replayed by chain position — a miss stays a miss — so if the
    new chain is shorter than the level that acked, nobody acks.

    The original run's hints are kept: they were asked for during the
    run, so they cost the same in the replay. The recorded delays are
    replayed exactly, so a seeded run's jitter is not drawn again.
    Raises ValueError on conflicting overrides or an inconsistent
    saved run.
    """
    if ack_timeout is not None and ack_timeouts is not None:
        raise ValueError(
            "use only one of --ack-timeout and --ack-timeouts")
    if ack_timeout is not None:
        if (isinstance(ack_timeout, bool)
                or not isinstance(ack_timeout, int)
                or ack_timeout <= 0):
            raise ValueError(
                "ack timeout must be a positive integer number of minutes")
    if purge_verification is not None:
        purge_verification = parse_purge_verification(purge_verification)
    scenario = _scenario_from_result(original)
    original_difficulty = original.difficulty or NORMAL
    overrides = []
    policy_swapped = policy is not None
    if policy is not None:
        # Swap in the named policy, scaled to the original run's
        # difficulty first (see the docstring); the difficulty block
        # below re-scales it again if a new difficulty is given.
        policy = _scale_timeouts(policy,
                                 original_difficulty.timeout_factor)
        overrides.append(
            f"policy: {original.policy.name} -> {policy.name}")
    else:
        policy = original.policy
    if difficulty is not None:
        if difficulty.name != original_difficulty.name:
            factor = (difficulty.timeout_factor
                      / original_difficulty.timeout_factor)
            policy = _scale_timeouts(policy, factor)
            overrides.append(
                f"difficulty: {original_difficulty.name} -> "
                f"{difficulty.name}")
        # The replay scores against the new difficulty either way.
        replay_difficulty = difficulty
    else:
        replay_difficulty = original.difficulty
    if ack_timeout is not None:
        levels = tuple(
            replace(level, ack_timeout_min=ack_timeout)
            for level in policy.levels
        )
        policy = replace(policy, levels=levels)
        overrides.append(f"ack timeout: every level {ack_timeout}m")
    elif ack_timeouts is not None:
        policy = parse_ack_timeout_overrides(ack_timeouts, policy)
        overrides.append(
            "ack timeouts: " + ", ".join(
                f"{level.responder.name} ({level.responder.role}) "
                f"{level.ack_timeout_min}m"
                for level in policy.levels))
    # Purge verification is a mode, not a number: the replay uses the
    # override when it differs from the base run, and keeps the base
    # run's mode otherwise — the same "only record a change" rule as
    # --difficulty above. simulate() raises when per-region
    # verification is asked of a run whose scenario has no regions.
    replay_purge_verification = original.purge_verification
    if purge_verification is not None:
        replay_purge_verification = purge_verification
        if purge_verification != original.purge_verification:
            overrides.append(
                "purge verification: "
                f"{original.purge_verification} -> {purge_verification}")
    replayed = simulate(scenario, policy, difficulty=replay_difficulty,
                        hints=len(original.hints),
                        # A replay under the original policy keeps the
                        # rota week the run was played for; a swapped-in
                        # policy has its own responders, so the week no
                        # longer applies.
                        rota_week=(original.rota_week
                                   if not policy_swapped else None),
                        purge_verification=replay_purge_verification)
    return WhatIfResult(original=original, replayed=replayed,
                        overrides=overrides)
