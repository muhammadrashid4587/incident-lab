# incident-lab

An SRE incident simulator with escalation policies, timeline replay,
and postmortem debriefs.
Pure Python 3, standard library only, no dependencies.

## What it is

A training and planning tool for on-call incident response. You define a
service and an escalation policy (primary, secondary, manager, each with an
ack timeout), pick a scenario from a small library of realistic incidents,
and the simulator plays the whole incident out on a simulated clock:

- **page** the primary, **ack** (or time out and **escalate**),
- **mitigate**, then **resolve**.

Every event is recorded with a timestamp into a timeline you can replay and
print, and the response is scored on time-to-ack, time-to-mitigate and
escalations.

## What it is not

Not a real pager. Nothing here pages a real person, sends a real alert, or
talks to PagerDuty, Opsgenie or any incident tool. The clock is simulated
(the whole incident runs instantly), and responder behaviour is fixed by
the scenario, not by real humans.

## Quickstart

```sh
python3 -m incident_lab list
python3 -m incident_lab run disk-full
python3 -m incident_lab run latency-spike
python3 -m incident_lab run disk-full --json
python3 -m incident_lab run latency-spike --policy fast
python3 -m incident_lab run disk-full --difficulty easy
python3 -m incident_lab run disk-full --difficulty hard
python3 -m incident_lab run latency-spike --difficulty 3 --seed 7 --json
python3 -m incident_lab run latency-spike --ack-timeouts 120,300,600
python3 -m incident_lab run latency-spike --ack-timeouts primary=120 L2=300
python3 -m incident_lab run disk-full --policy-file examples/custom-policy.json
python3 -m incident_lab run dns-outage --json > /tmp/dns-outage.json
python3 -m incident_lab run disk-full --seed 7 --json
python3 -m incident_lab run disk-full --seed 7 --jitter 0-3 --json
python3 -m incident_lab run disk-full --seeds 1,2,3
python3 -m incident_lab run disk-full --seeds 1-5 --json
python3 -m incident_lab replay /tmp/dns-outage.json
python3 -m incident_lab run latency-spike --json > /tmp/latency-spike.json
python3 -m incident_lab debrief /tmp/latency-spike.json
python3 -m incident_lab debrief /tmp/latency-spike.json --json
python3 -m incident_lab run disk-full --seed 1 --json > /tmp/run-seed-1.json
python3 -m incident_lab run disk-full --seed 7 --json > /tmp/run-seed-7.json
python3 -m incident_lab compare /tmp/run-seed-1.json /tmp/run-seed-7.json
python3 -m incident_lab compare /tmp/run-seed-1.json /tmp/run-seed-7.json --json
python3 -m incident_lab run disk-full --hints 1
python3 -m incident_lab run disk-full --hints 2 --json
python3 -m incident_lab template disk-full > my-disk-full.json
python3 -m incident_lab run disk-full --scenario-file my-disk-full.json
python3 -m incident_lab run cache-down --scenario-file examples/custom-scenario.json
python3 scripts/demo.py
```

## JSON output

`run --json` prints the whole result as machine-readable JSON instead of
the human replay: incident, policy (levels with responders and ack
timeouts), the timeline in time order, the score, and any hints asked for
(each with its text and cost). The score carries
the acking level's lateness (`ack_late_min`, 0 for an on-time ack) and
the partial credit it kept (`ack_credit`, 1.0 on time). The score also
carries the effective mitigate and resolve delays
(`mitigate_delay_min`, `resolve_delay_min`, `null` when the incident
was never mitigated or resolved), which vary with `--seed`. Unreached milestones
serialize as `null` (for example, an incident nobody acks has
`"time_to_ack_min": null` and `"points": 0`). The top-level `seed` records
the `--seed` value used for the run, or `null` for an unseeded run, and the
top-level `jitter` records the `--jitter` range as `[lo, hi]`, or `null`
for an unseeded run. The top-level `difficulty` records the difficulty
the run was played at (`easy`, `normal` or `hard`; see below).

A batch run (`--seeds`, below) prints a different JSON shape: `seeds`
(the seed list), `jitter`, `difficulty`, the shared `incident` and
`policy`, every run in full under `runs` (each one a single-run result
as above), and `score_distribution` with the mean, min, max and median
points plus a `per_seed` list of `{"seed": N, "points": P}` entries.

## Replay

`replay <path>` reads a JSON file saved from `run --json` and re-prints
the timeline and score in the same human-readable format `run` uses, so
you can review a past incident without re-running it:

```sh
python3 -m incident_lab run dns-outage --json > dns-outage.json
python3 -m incident_lab replay dns-outage.json
```

A missing file, bad JSON, or a file that is not a saved run fails with
exit code 2 and an error message.

## Debrief

`debrief <path>` reads a JSON file saved from `run --json` and writes
the postmortem for the incident as Markdown: a summary (score and
grade — A at 90, B at 75, C at 50, else D — plus time-to-ack,
time-to-mitigate and time-to-resolve), the timeline, what went well,
what went badly, and action items. Every finding is derived from the
run itself, never invented: a missed page names the responder and
becomes an action to check their paging delivery, a late ack names the
lateness and the credit it kept, and slow mitigation becomes an action
to write or rehearse a runbook. An incident nobody acked lists every
missed level and adds a fallback-page action. Action items carry an
owner — the responder whose page was missed or late, or the on-call
lead for the systemic ones. With `--json` the same postmortem prints
as JSON instead of Markdown.

```sh
python3 -m incident_lab run latency-spike --json > latency-spike.json
python3 -m incident_lab debrief latency-spike.json
```

## Compare

`compare <run-a.json> <run-b.json>` diffs two saved `run --json` runs
side by side: a score table with the delta on each metric (points,
time-to-ack, time-to-mitigate, time-to-resolve, escalations, lateness,
ack credit) and a timeline diff — the events both runs share, with
how each event's time shifted, plus the events only one run has. With
`--json` the same comparison prints as JSON instead. A comparison is
most useful for two runs of the same scenario (for example, two seeds,
or the same incident before and after a policy change); comparing
different scenarios still works, but the report says plainly that the
scores are not directly comparable, and a different policy is flagged
the same way. A milestone one run never reached (nobody acked, never
mitigated) shows as `-` with no delta.

```sh
python3 -m incident_lab run disk-full --seed 1 --json > run-seed-1.json
python3 -m incident_lab run disk-full --seed 7 --json > run-seed-7.json
python3 -m incident_lab compare run-seed-1.json run-seed-7.json
```

A missing file, bad JSON, or a file that is not a saved run fails with
exit code 2 and an error message.

## Incident templates

`template <scenario>` prints a scenario as a shareable JSON template —
the same shape a scenario file uses — so you can save it, edit it, and
hand it to someone else:

```sh
python3 -m incident_lab template disk-full > my-disk-full.json
```

`run <name> --scenario-file <path>` loads named scenarios from a JSON
file, the same way `--policy-file` loads policies. A file scenario with
the same name as a built-in one wins. The file holds a `"scenarios"`
mapping, each entry with `service`, `title`, `severity` (`low`,
`medium`, `high` or `critical`), `description`, `ack_delay_min` (a list
of minutes per escalation level, `null` where a responder never acks),
and `mitigate_delay_min` / `resolve_delay_min`. An entry can also carry
an optional `hints` list of known-good next actions, asked for with
`run --hints N` (see Hints):

```json
{"scenarios": {"cache-down": {
  "service": "sessions-cache",
  "title": "Cache cluster unreachable",
  "severity": "high",
  "description": "The sessions cache stopped accepting writes.",
  "ack_delay_min": [2, null, null],
  "mitigate_delay_min": 15,
  "resolve_delay_min": 10
}}}
```

See `examples/custom-scenario.json`. A missing file, bad JSON, or an
invalid scenario fails the run with exit code 2 and an error message.

## Scenarios

| name | service | severity | what happens |
| ---- | ------- | -------- | ------------ |
| disk-full | payments-db | high | primary acks in 3m, mitigates in 20m |
| latency-spike | checkout-api | critical | primary misses the page, secondary acks in 4m |
| cert-expiry | edge-tls | high | primary acks in 2m, quick fix |
| deploy-regression | search-api | critical | primary and secondary miss, manager acks in 6m |
| queue-backlog | orders-queue | high | primary acks in 4m, scales consumers to drain backlog |
| dns-outage | edge-dns | critical | primary misses the page, secondary acks in 3m, fails over |

## Escalation policy

The default policy (`standard`) is: primary, 5 minute ack timeout;
secondary, 5 minutes; manager, 10 minutes. Pick a different built-in
policy with `run <scenario> --policy <name>`:

| policy | primary | secondary | manager | for |
| ------ | ------- | --------- | ------- | --- |
| standard | 5m | 5m | 10m | the default chain |
| fast | 2m | 2m | 5m | critical services where paging delay costs more than a hasty escalation |

Override the timeout for every level with `run <scenario> --ack-timeout <minutes>`.
`--ack-timeout` applies on top of whichever policy you picked.

Override timeouts per level with `run <scenario> --ack-timeouts <spec>...`,
either as a list in level order (`--ack-timeouts 120,300,600`) or by name
(`--ack-timeouts primary=120 L2=300`, naming a role or a 1-based level
number). Named overrides may cover any subset and leave the rest of the
policy alone; a positional list must cover every level. Values must be
positive integers in minutes, and a bad value, an unknown level, or the
wrong number of timeouts fails the run with exit code 2 and an error
message. `--ack-timeout` and `--ack-timeouts` cannot be combined. The
effective timeouts are printed in the run output and in the policy
section of `run --json`.

Define your own named policies in a JSON file and load them with
`run <scenario> --policy-file <path> --policy <name>`. A file policy with
the same name as a built-in one wins. If the file holds exactly one
policy, `--policy` is optional. See `examples/custom-policy.json`:

```json
{"policies": {"relaxed": {"levels": [
  {"name": "ana", "role": "primary", "ack_timeout_min": 10},
  {"name": "ben", "role": "secondary", "ack_timeout_min": 10},
  {"name": "carla", "role": "manager", "ack_timeout_min": 20}
]}}}
```

Levels are validated on load: each needs a non-empty `name` and `role`
and a positive integer `ack_timeout_min`, and a policy needs at least
one level. A missing file, bad JSON, or an invalid policy fails the run
with exit code 2 and an error message.

## Scoring

Points start at 100 and are subtracted:

- 15 points per escalation
- 1 point per minute of time-to-ack past the ack grace (5 minutes on
  normal; the difficulty sets it)
- 1 point per 2 minutes of time-to-mitigate past the mitigate grace
  (15 minutes on normal; the difficulty sets it)
- an incident nobody acks scores 0
- 5 points per hint asked for (`run --hints N`)

A late ack still counts, but it only keeps part of the score. The ack
credit is 1.0 up to the acking level's timeout, then decays linearly to
0.0 at twice the timeout, and the effective score is the base score
above scaled by that credit. The acking level's lateness
(`ack_late_min`) and credit (`ack_credit`) are shown in the run output
and in the score section of `run --json`.

Example: `disk-full` acks in 3m and mitigates in 23m with no escalations,
so 100 - 0 - (23-15)//2 = 96. On the `fast` policy the primary's 2m
timeout makes that a 1m-late ack, so the credit is 1 - 1/2 = 0.5 and the
effective score is 96 × 0.5 = 48.

## Hints

`run <scenario> --hints N` asks for N hints during the run: the operator
is stuck while triaging and asks for help. Each hint is one of the
scenario's known-good next actions, drawn in order (the first hint is
the most useful thing to check first), and each one costs 5 points off
the final score, after the late-ack credit scaling, floored at 0. The
hints used are printed after the score, recorded in the `run --json`
output (a top-level `hints` list of `{"text", "cost_points"}` entries,
plus `score.hints_used` and `score.hint_cost_points`), and shown again
by `replay`. Asking for more hints than the scenario has fails the run
with exit code 2 and an error message, as does a negative count;
`--hints` cannot be combined with `--seeds`, since hints are asked for
during one run. Custom scenarios loaded with `--scenario-file` can
carry their own `hints` list; `template` prints a scenario's hints
with it.

```sh
python3 -m incident_lab run disk-full --hints 1
python3 -m incident_lab run disk-full --hints 2 --json
```

Example: `disk-full` with one hint scores 96 - 5 = 91, and the hint
("Check WAL archiving...") is printed with the run.

## Difficulty ladder

`run <scenario> --difficulty <level>` plays the same scenario harder or
easier. Levels are `easy`, `normal` (the default) and `hard`, or the
numbers `1`, `2`, `3`; an unknown level fails the run with exit code 2
and an error message. A difficulty scales three things at once:

| | easy | normal | hard |
| - | ---- | ------ | ---- |
| ack timeouts | ×1.5 (5m → 8m) | ×1.0 | ×0.6 (5m → 3m) |
| ack grace | 8m | 5m | 3m |
| mitigate grace | 20m | 15m | 10m |
| default seeded jitter | 0-0 | 0-1 | 0-3 |

Tighter timeouts mean faster escalation on hard (the secondary is paged
sooner), and the stricter graces mean the same incident scores lower:
`disk-full` scores 99 on easy, 96 on normal and 94 on hard. The
difficulty is printed in the human-readable output, recorded in the
`run --json` output (top-level `difficulty`), and shown again by
`replay` and `debrief` — a debrief also judges mitigation against the
difficulty's own target, so a hard run is not marked against the
normal 15m target. An explicit `--ack-timeout` or `--ack-timeouts`
still wins for the timeouts it sets, but the difficulty's scoring
targets keep applying. With `--seeds` the same difficulty applies to
every run in the batch.

## Layout

```
incident_lab/
  policy.py      escalation chain: responders, ack timeouts
  incident.py    incident model
  timeline.py    append-only, time-ordered event log
  scenarios.py   built-in incident scenarios + scenario files/templates
  simulator.py   deterministic simulation + scoring
  difficulty.py  difficulty ladder: easy / normal / hard scaling
  replay.py      rebuild a saved --json run for re-printing
  debrief.py     structured postmortem from a saved run
  compare.py     side-by-side diff of two saved runs: score deltas,
                 shared-event time shifts, events only one run has
  cli.py         list / run / replay / debrief / compare
tests/
  test_incident_lab.py
scripts/
  demo.py        run every scenario, one-line summary each
examples/
  custom-policy.json   sample file for --policy-file
  custom-scenario.json sample file for --scenario-file
```

## Tests

```sh
make test
```

## Seeded runs

`run <scenario> --seed N` jitters each ack delay by up to 1 minute either
way, drawn from a random generator seeded with `N`, so the same seed
replays the same run exactly (same timeline, same score, byte-identical
`--json` output) while different seeds vary the response times. A miss
stays a miss, so the seed never changes who acks or how many escalations
happen — only how quickly the acking responder answers. The same seeded
jitter also applies to the mitigate and resolve delays once someone
acks, drawn from the same generator after the ack draws, so the whole
incident varies with the seed while staying reproducible; the effective
delays are recorded in the `run --json` output as
`score.mitigate_delay_min` and `score.resolve_delay_min` (or `null` for
an incident nobody acks). The seed is recorded in the `run --json`
output (top-level `seed`) and shown in the human-readable output, and
`replay` re-prints it from a saved run.

`run <scenario> --seed N --jitter LO-HI` widens or narrows that jitter:
each ack, mitigate and resolve delay shifts by a magnitude drawn from
`LO` to `HI` minutes, with a random sign, from the same seeded
generator, so reproducibility holds for any range. `--jitter 0-0` means
no jitter at all. The default is `0-1`, the same as `--seed` alone.
`--jitter` requires `--seed` or `--seeds`, and a bad range (negative bounds, or `LO`
greater than `HI`) fails the run with exit code 2 and an error
message. The range is recorded in the `run --json` output (top-level
`jitter`, `[lo, hi]`) and shown in the human-readable output, and
`replay` re-prints it from a saved run.

## Multi-seed batch mode

`run <scenario> --seeds 1,2,3` (or `--seeds 1-5` for an inclusive
range) runs the same scenario once per seed and reports the score
distribution instead of a single timeline: the mean, min, max and
median points across runs, plus the score for each seed. `--seeds`
cannot be combined with `--seed`; `--jitter` works with `--seeds`
and applies the same range to every run. A bad spec (empty entries,
non-integer or negative seeds, a reversed range) fails the run with
exit code 2 and an error message. With `--json` the batch prints
`seeds`, the shared incident and policy, every run in full under
`runs`, and `score_distribution` (mean/min/max/median and a
`per_seed` list); without it the same distribution is printed in
human-readable form.

## Version

0.17.0 — hints: `run --hints N` asks for N hints during a run, drawn in
order from the scenario's known-good next actions (each scenario carries
its own, and scenario files can add theirs); each hint costs 5 points
off the final score and is recorded in `run --json` and shown again by
`replay`.

## Roadmap

- [x] Escalation policies and timeline replay (0.1.0)
- [x] `run --json` prints the full result as JSON (0.2.0)
- [x] Named policies: `--policy fast` with tighter ack timeouts (0.3.0)
- [x] Custom policies from a JSON file (`--policy-file`) (0.4.0)
- [x] More scenarios (queue backlog, DNS outage) (0.5.0)
- [x] `replay` command: re-print a timeline from a saved `--json` run (0.6.0)
- [x] Per-level ack timeouts on the command line (0.7.0)
- [x] Late-ack partial credit: linear decay to zero at 2x the timeout (0.8.0)
- [x] `--seed N` for randomized ack delays: reproducible runs (0.9.0)
- [x] `--jitter LO-HI` for a configurable seeded ack-delay jitter range (0.10.0)
- [x] Seeded jitter for mitigate and resolve delays, recorded in JSON, replay round-trips (0.11.0)
- [x] Multi-seed batch mode: `--seeds 1,2,3` or `--seeds 1-5` runs the same scenario per seed and reports the score distribution (mean/min/max/median, per-seed scores) in human output and `--json` (0.12.0)
- [x] Debrief/postmortem report generator: `debrief <saved-run.json>` writes a structured postmortem (summary with score and grade, timeline, what went well / badly, action items with owners) as Markdown or `--json`, every finding derived from the run (0.13.0)
- [x] Shareable incident templates: `template <scenario>` prints a scenario as JSON, `run --scenario-file` loads named scenarios from a JSON file (file wins over built-in names), validated like `--policy-file` (0.14.0)
- [x] Difficulty ladder: `run --difficulty easy|normal|hard` (or 1/2/3) scales ack timeouts (×1.5 / ×1.0 / ×0.6), scoring targets (ack grace 8/5/3m, mitigate grace 20/15/10m) and the default seeded jitter (0-0 / 0-1 / 0-3), records the difficulty in `run --json`, and `replay`/`debrief` show it (0.15.0)
- [x] Run comparison: `compare <run-a.json> <run-b.json>` diffs two saved runs side by side (score deltas per metric, timeline diff of shared-event time shifts plus events only one run has), human form or `--json`, flags different scenarios/policies, unreached milestones show as `-` (0.16.0)
- [x] Hints: `run --hints N` asks for N hints during a run, drawn in order from the scenario's known-good next actions (each built-in scenario carries 2-3, scenario files can add their own, `template` prints them); each hint costs 5 points off the final score and is recorded in `run --json` and shown again by `replay` (0.17.0)
