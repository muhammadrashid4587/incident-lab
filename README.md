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
python3 -m incident_lab template disk-full > my-disk-full.json
python3 -m incident_lab run disk-full --scenario-file my-disk-full.json
python3 -m incident_lab run cache-down --scenario-file examples/custom-scenario.json
python3 scripts/demo.py
```

## JSON output

`run --json` prints the whole result as machine-readable JSON instead of
the human replay: incident, policy (levels with responders and ack
timeouts), the timeline in time order, and the score. The score carries
the acking level's lateness (`ack_late_min`, 0 for an on-time ack) and
the partial credit it kept (`ack_credit`, 1.0 on time). The score also
carries the effective mitigate and resolve delays
(`mitigate_delay_min`, `resolve_delay_min`, `null` when the incident
was never mitigated or resolved), which vary with `--seed`. Unreached milestones
serialize as `null` (for example, an incident nobody acks has
`"time_to_ack_min": null` and `"points": 0`). The top-level `seed` records
the `--seed` value used for the run, or `null` for an unseeded run, and the
top-level `jitter` records the `--jitter` range as `[lo, hi]`, or `null`
for an unseeded run.

A batch run (`--seeds`, below) prints a different JSON shape: `seeds`
(the seed list), `jitter`, the shared `incident` and `policy`, every
run in full under `runs` (each one a single-run result as above), and
`score_distribution` with the mean, min, max and median points plus a
`per_seed` list of `{"seed": N, "points": P}` entries.

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
and `mitigate_delay_min` / `resolve_delay_min`:

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
- 1 point per minute of time-to-ack past 5 minutes
- 1 point per 2 minutes of time-to-mitigate past 15 minutes
- an incident nobody acks scores 0

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

## Layout

```
incident_lab/
  policy.py      escalation chain: responders, ack timeouts
  incident.py    incident model
  timeline.py    append-only, time-ordered event log
  scenarios.py   built-in incident scenarios + scenario files/templates
  simulator.py   deterministic simulation + scoring
  replay.py      rebuild a saved --json run for re-printing
  debrief.py     structured postmortem from a saved run
  cli.py         list / run / replay / debrief
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

0.14.0 — shareable incident templates: `template <scenario>` prints a
scenario as JSON you can save, edit and share, and `run <name>
--scenario-file <path>` runs scenarios from a JSON file (a file scenario
with the same name as a built-in one wins), with the same validation and
exit-code-2 errors as `--policy-file`.

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
