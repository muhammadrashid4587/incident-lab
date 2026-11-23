# incident-lab

An SRE incident simulator with escalation policies and timeline replay.
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
python3 -m incident_lab replay /tmp/dns-outage.json
python3 scripts/demo.py
```

## JSON output

`run --json` prints the whole result as machine-readable JSON instead of
the human replay: incident, policy (levels with responders and ack
timeouts), the timeline in time order, and the score. The score carries
the acking level's lateness (`ack_late_min`, 0 for an on-time ack) and
the partial credit it kept (`ack_credit`, 1.0 on time). Unreached milestones
serialize as `null` (for example, an incident nobody acks has
`"time_to_ack_min": null` and `"points": 0`). The top-level `seed` records
the `--seed` value used for the run, or `null` for an unseeded run.

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
  scenarios.py   built-in incident scenarios
  simulator.py   deterministic simulation + scoring
  replay.py      rebuild a saved --json run for re-printing
  cli.py         list / run / replay
tests/
  test_incident_lab.py
scripts/
  demo.py        run every scenario, one-line summary each
examples/
  custom-policy.json   sample file for --policy-file
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
happen — only how quickly the acking responder answers. The seed is
recorded in the `run --json` output (top-level `seed`) and shown in the
human-readable output, and `replay` re-prints it from a saved run.

## Version

0.9.0 — `--seed N` for randomized ack delays: each ack delay is jittered
by up to 1 minute from a seeded RNG, so a run is reproducible (same seed,
same output) and the seed is recorded in the `run --json` output.

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
