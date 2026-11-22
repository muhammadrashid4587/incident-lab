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
python3 scripts/demo.py
```

## JSON output

`run --json` prints the whole result as machine-readable JSON instead of
the human replay: incident, policy (levels with responders and ack
timeouts), the timeline in time order, and the score. Unreached milestones
serialize as `null` (for example, an incident nobody acks has
`"time_to_ack_min": null` and `"points": 0`).

## Scenarios

| name | service | severity | what happens |
| ---- | ------- | -------- | ------------ |
| disk-full | payments-db | high | primary acks in 3m, mitigates in 20m |
| latency-spike | checkout-api | critical | primary misses the page, secondary acks in 4m |
| cert-expiry | edge-tls | high | primary acks in 2m, quick fix |
| deploy-regression | search-api | critical | primary and secondary miss, manager acks in 6m |

## Escalation policy

The default policy (`standard`) is: primary, 5 minute ack timeout;
secondary, 5 minutes; manager, 10 minutes. Override the timeout for every
level with `run <scenario> --ack-timeout <minutes>`.

## Scoring

Points start at 100 and are subtracted:

- 15 points per escalation
- 1 point per minute of time-to-ack past 5 minutes
- 1 point per 2 minutes of time-to-mitigate past 15 minutes
- an incident nobody acks scores 0

Example: `disk-full` acks in 3m and mitigates in 23m with no escalations,
so 100 - 0 - (23-15)//2 = 96.

## Layout

```
incident_lab/
  policy.py      escalation chain: responders, ack timeouts
  incident.py    incident model
  timeline.py    append-only, time-ordered event log
  scenarios.py   built-in incident scenarios
  simulator.py   deterministic simulation + scoring
  cli.py         list / run
tests/
  test_incident_lab.py
scripts/
  demo.py        run every scenario, one-line summary each
```

## Tests

```sh
make test
```

## Version

0.2.0 — `run --json` prints the full result (incident, policy, timeline,
score) as JSON.
