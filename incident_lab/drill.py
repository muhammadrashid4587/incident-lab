"""Interactive drill mode: play an incident step by step.

``run`` plays a scenario's fixed responder behaviour on a simulated
clock. A drill hands the decisions to the operator instead: at each
page the operator chooses when to ack (or lets the timeout lapse and
escalates), then when to mitigate and resolve. The timeouts, the
late-ack credit and the scoring are the same as ``run`` — the drill
uses the same ``compute_points`` — so a drill that makes the same
decisions as a ``run`` scores the same.

A drill reads one decision per line, from stdin (or a file given with
``drill --input``), so a scripted drill is just a list of decisions
and needs no TTY:

    ack 3
    mitigate 20
    resolve 12

``ack N`` acks N minutes after the page (``ack`` alone means now, N
defaults to 0); a delay past the level's timeout is a late ack, with
the same partial credit ``run`` gives. ``wait`` lets the timeout
lapse and escalates to the next level. ``mitigate N`` / ``resolve N``
are minutes after the ack / mitigation. ``quit`` ends the drill
cleanly at any prompt; running out of input (EOF) does the same. A
drill ended early records a partial run — the timeline so far, plus
an ``abandoned`` event — scored on the milestones it reached, in the
same JSON shape as ``run`` so ``replay``, ``debrief``, ``compare``
and ``stats`` all work on drill runs.

Prompts go to the prompt stream (stderr by default), never to stdout,
so ``drill --json`` output stays parseable JSON.
"""

import sys

from .difficulty import NORMAL
from .incident import Incident
from .policy import EscalationPolicy
from .scenarios import Scenario
from .simulator import (
    HINT_COST_POINTS, Hint, Score, SimulationResult, compute_points,
    late_ack_credit,
)
from .timeline import Timeline


def _parse_minutes(text: str, command: str):
    """Parse the optional minute argument of a drill decision.

    Returns the delay in minutes (0 when the command stands alone).
    Raises ValueError on a missing, non-integer or negative value.
    """
    if text == "":
        return 0
    try:
        minutes = int(text)
    except ValueError:
        raise ValueError(
            f"{command} takes a number of minutes (got {text!r})")
    if minutes < 0:
        raise ValueError(
            f"{command} takes a non-negative number of minutes "
            f"(got {minutes})")
    return minutes


def _read_decision(prompt_stream, input_stream, prompt: str,
                   choices: dict) -> tuple:
    """Read one drill decision from the input stream.

    ``choices`` maps a command word to its (argument name, help) — the
    prompt lists them. Returns ``(command, arg)`` where ``arg`` is the
    parsed minute delay (or None for commands that take none), or
    ``("quit", reason)`` when the operator quits or the input ends.
    An unrecognised line prints an error and re-prompts; EOF ends the
    drill the same way ``quit`` does.
    """
    help_text = ", ".join(
        f"{word} {arg}" if arg else word
        for word, (arg, _hint) in choices.items())
    prompt_stream.write(f"{prompt}\n  [{help_text}] > ")
    prompt_stream.flush()
    while True:
        line = input_stream.readline()
        if line == "":
            return ("quit", "input ended")
        stripped = line.strip()
        if not stripped:
            prompt_stream.write("  (say something — or 'quit' to stop)\n  > ")
            prompt_stream.flush()
            continue
        word, _, rest = stripped.partition(" ")
        word = word.lower()
        if word in ("quit", "q", "exit"):
            return ("quit", "ended by operator")
        if word in ("help", "?"):
            for name, (_arg, hint) in choices.items():
                prompt_stream.write(f"  {name}: {hint}\n")
            prompt_stream.write("  > ")
            prompt_stream.flush()
            continue
        if word not in choices:
            prompt_stream.write(
                f"  unknown answer {stripped!r}: expected one of "
                f"{', '.join(sorted(choices))} (or 'quit')\n  > ")
            prompt_stream.flush()
            continue
        arg_name, _hint = choices[word]
        if arg_name is None:
            if rest.strip():
                prompt_stream.write(
                    f"  {word} takes no argument (got {rest.strip()!r})\n  > ")
                prompt_stream.flush()
                continue
            return (word, None)
        try:
            return (word, _parse_minutes(rest.strip(), word))
        except ValueError as exc:
            prompt_stream.write(f"  {exc}\n  > ")
            prompt_stream.flush()


def _partial_result(incident, policy, timeline, *, escalations,
                    acked_at, acked_by, ack_late_min, ack_credit,
                    mitigated_at, mitigate_delay_min, difficulty,
                    used_hints, hint_cost) -> SimulationResult:
    """Build the result for a drill that stopped early.

    The timeline holds the events so far (plus the ``abandoned``
    event the caller recorded); the score covers the milestones
    reached, with the unreached ones as null and ``resolved`` false.
    """
    points = compute_points(
        escalations=escalations, acked_at=acked_at, ack_credit=ack_credit,
        mitigated_at=mitigated_at, difficulty=difficulty,
        hint_cost=hint_cost)
    return SimulationResult(
        incident, policy, timeline,
        Score(
            time_to_ack_min=acked_at,
            time_to_mitigate_min=mitigated_at,
            time_to_resolve_min=None,
            escalations=escalations,
            resolved=False,
            points=points,
            ack_late_min=ack_late_min,
            ack_credit=ack_credit,
            mitigate_delay_min=mitigate_delay_min,
            resolve_delay_min=None,
            hints_used=len(used_hints),
            hint_cost_points=hint_cost,
        ),
        difficulty=difficulty,
        hints=used_hints,
    )


def run_drill(
    scenario: Scenario,
    policy: EscalationPolicy,
    difficulty=None,
    hints: int = 0,
    input_stream=None,
    prompt_stream=None,
) -> SimulationResult:
    """Run one scenario as an interactive drill and return the result.

    The operator plays the responder: at each page they choose when to
    ack (or ``wait`` and escalate), then when to mitigate and resolve,
    one decision per line from ``input_stream`` (stdin when None).
    Prompts and progress go to ``prompt_stream`` (stderr when None).
    The timeouts, late-ack credit and scoring are the same as
    ``simulate``, so the same decisions score the same; the scenario's
    fixed ack/mitigate/resolve delays are the behaviour ``run`` plays,
    not the drill's — the drill's delays are the operator's decisions.

    ``difficulty`` (a ``Difficulty``, defaulting to normal) scales the
    policy timeouts the same way ``run`` does — pass the already-scaled
    policy, as the CLI does. ``hints`` asks for that many hints up
    front, drawn in order from the scenario's known-good next actions,
    each costing ``HINT_COST_POINTS`` off the final score, exactly as
    in ``run``. Raises ValueError if ``hints`` is negative or more
    than the scenario has.
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
    if input_stream is None:
        input_stream = sys.stdin
    if prompt_stream is None:
        prompt_stream = sys.stderr
    incident = Incident(
        id=scenario.name,
        service=scenario.service,
        title=scenario.title,
        severity=scenario.severity,
        description=scenario.description,
    )
    timeline = Timeline()
    timeline.record(0, "declared", "monitor", scenario.title)

    prompt_stream.write(
        f"drill: {incident.title} [{incident.severity}]\n"
        f"service:  {incident.service}\n"
        f"policy:   {policy.name} ("
        + ", ".join(
            f"{level.responder.name} ({level.responder.role}) "
            f"{level.ack_timeout_min}m" for level in policy.levels)
        + ")\n"
        f"difficulty: {difficulty.name}\n"
        f"{incident.description}\n\n")

    t = 0
    escalations = 0
    acked_at = None
    acked_by = None
    ack_late_min = None
    ack_credit = None

    for i, level in enumerate(policy.levels):
        timeline.record(t, "paged", level.responder.name,
                        level.responder.role)
        prompt_stream.write(
            f"[t={t}m] paged {level.responder.name} "
            f"({level.responder.role}) — ack timeout "
            f"{level.ack_timeout_min}m.\n")
        command, arg = _read_decision(
            prompt_stream, input_stream,
            "  ack in how many minutes?",
            {
                "ack": ("N", "ack N minutes after the page "
                             "(or 'ack' alone to ack now); "
                             "past the timeout is a late ack"),
                "wait": (None, "let the timeout lapse and escalate"),
            })
        if command == "quit":
            timeline.record(t, "abandoned", "operator",
                            f"drill {arg}")
            return _partial_result(
                incident, policy, timeline, escalations=escalations,
                acked_at=None, acked_by=None, ack_late_min=None,
                ack_credit=None, mitigated_at=None,
                mitigate_delay_min=None, difficulty=difficulty,
                used_hints=used_hints, hint_cost=hint_cost)
        if command == "wait":
            # Missed ack: wait out the timeout, then escalate (or give
            # up, exactly as `run` does when nobody acks).
            t += level.ack_timeout_min
            if i + 1 < len(policy.levels):
                escalations += 1
                timeline.record(
                    t, "escalated",
                    policy.levels[i + 1].responder.name,
                    f"no ack from {level.responder.name} in "
                    f"{level.ack_timeout_min}m",
                )
                continue
            timeline.record(t, "unacked", "monitor",
                            "nobody acked the page")
            return _partial_result(
                incident, policy, timeline, escalations=escalations,
                acked_at=None, acked_by=None, ack_late_min=None,
                ack_credit=None, mitigated_at=None,
                mitigate_delay_min=None, difficulty=difficulty,
                used_hints=used_hints, hint_cost=hint_cost)
        # The operator acks, on time or late: the same late-ack credit
        # `run` gives for an ack past its level's timeout.
        delay = arg
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

    command, arg = _read_decision(
        prompt_stream, input_stream,
        f"  {acked_by} acked at {acked_at}m. mitigate in how many "
        "minutes?",
        {"mitigate": ("N", "mitigate N minutes after the ack "
                           "(or 'mitigate' alone to mitigate now)")})
    if command == "quit":
        timeline.record(acked_at, "abandoned", "operator",
                        f"drill {arg}")
        return _partial_result(
            incident, policy, timeline, escalations=escalations,
            acked_at=acked_at, acked_by=acked_by,
            ack_late_min=ack_late_min, ack_credit=ack_credit,
            mitigated_at=None, mitigate_delay_min=None,
            difficulty=difficulty, used_hints=used_hints,
            hint_cost=hint_cost)
    mitigate_delay = arg
    mitigated_at = acked_at + mitigate_delay
    timeline.record(mitigated_at, "mitigated", acked_by)

    command, arg = _read_decision(
        prompt_stream, input_stream,
        f"  mitigated at {mitigated_at}m. resolve in how many minutes?",
        {"resolve": ("N", "resolve N minutes after the mitigation "
                         "(or 'resolve' alone to resolve now)")})
    if command == "quit":
        timeline.record(mitigated_at, "abandoned", "operator",
                        f"drill {arg}")
        return _partial_result(
            incident, policy, timeline, escalations=escalations,
            acked_at=acked_at, acked_by=acked_by,
            ack_late_min=ack_late_min, ack_credit=ack_credit,
            mitigated_at=mitigated_at,
            mitigate_delay_min=mitigate_delay, difficulty=difficulty,
            used_hints=used_hints, hint_cost=hint_cost)
    resolve_delay = arg
    resolved_at = mitigated_at + resolve_delay
    timeline.record(resolved_at, "resolved", acked_by)

    points = compute_points(
        escalations=escalations, acked_at=acked_at, ack_credit=ack_credit,
        mitigated_at=mitigated_at, difficulty=difficulty,
        hint_cost=hint_cost)
    return SimulationResult(
        incident, policy, timeline,
        Score(
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
        ),
        difficulty=difficulty,
        hints=used_hints,
    )
