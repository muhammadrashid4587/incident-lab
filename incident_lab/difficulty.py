"""Difficulty levels for incident-lab.

A difficulty scales how hard the incident is to handle well:

- ack timeouts are scaled (easy gives responders more time, hard gives
  them less, so hard escalates faster),
- the scoring targets tighten (the ack and mitigate grace minutes shrink
  as difficulty rises),
- the default seeded jitter range widens (easy runs are predictable,
  hard runs vary more from seed to seed).

The same scenario at a different difficulty is a visibly different run:
timeouts, grace minutes, jitter and points all move, and the difficulty
is recorded in the ``run --json`` output so a saved run says how hard
it was.
"""

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Difficulty:
    """One difficulty level and everything it scales."""

    name: str
    # Ack timeouts are multiplied by this factor (rounded, at least 1m).
    timeout_factor: float
    # Scoring targets: no points lost for acking / mitigating within
    # these minutes (normal: 5 / 15).
    ack_grace_min: int
    mitigate_grace_min: int
    # Jitter range (lo, hi) in minutes used for a seeded run when no
    # explicit --jitter is given.
    default_jitter_range: tuple
    description: str


EASY = Difficulty(
    name="easy",
    timeout_factor=1.5,
    ack_grace_min=8,
    mitigate_grace_min=20,
    default_jitter_range=(0, 0),
    description="relaxed timeouts, generous scoring targets, no jitter",
)

NORMAL = Difficulty(
    name="normal",
    timeout_factor=1.0,
    ack_grace_min=5,
    mitigate_grace_min=15,
    default_jitter_range=(0, 1),
    description="the standard timeouts and scoring targets",
)

HARD = Difficulty(
    name="hard",
    timeout_factor=0.6,
    ack_grace_min=3,
    mitigate_grace_min=10,
    default_jitter_range=(0, 3),
    description="tight timeouts, strict scoring targets, wide jitter",
)

DIFFICULTIES = {
    EASY.name: EASY,
    NORMAL.name: NORMAL,
    HARD.name: HARD,
}

#: Numeric aliases: 1 is the easiest, 3 the hardest.
ALIASES = {"1": EASY, "2": NORMAL, "3": HARD}


def list_difficulties() -> list:
    """Difficulty names, easiest first."""
    return list(DIFFICULTIES)


def parse_difficulty(text: str) -> Difficulty:
    """Parse a difficulty name (or numeric alias 1/2/3).

    Names are case-insensitive. Raises ValueError on an unknown name.
    """
    key = text.strip().lower()
    if key in DIFFICULTIES:
        return DIFFICULTIES[key]
    if key in ALIASES:
        return ALIASES[key]
    raise ValueError(
        f"unknown difficulty {text!r}; difficulties are "
        f"{', '.join(list_difficulties())} (or 1, 2, 3)")


def apply_difficulty(policy, difficulty: Difficulty):
    """Scale a policy's ack timeouts by the difficulty's factor.

    Timeouts are rounded to whole minutes and never below 1 minute, so
    a hard run escalates faster and an easy run gives every responder
    more time before the page moves on.
    """
    levels = tuple(
        replace(
            level,
            ack_timeout_min=max(
                1, round(level.ack_timeout_min
                         * difficulty.timeout_factor)),
        )
        for level in policy.levels
    )
    return replace(policy, levels=levels)
