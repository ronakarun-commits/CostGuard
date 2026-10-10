# 7. Waste rules: registry of pure functions + static price table

Date: 2026-10-10 · Status: Accepted

## Decision
Each rule is a function `RuleContext -> Iterable[Finding]` registered with `@rule(id, title,
description)`. The engine runs all (or `--rule`-selected) rules, filters by `--min-saving`, and
ranks findings by estimated monthly saving. Rules never call AWS; they read an `Inventory` built by
an adapter (EC2 Describe APIs, or the synthetic inventory for offline demos).

Savings use a **static price table** (us-east-1 list prices × region multiplier, documented date).

Built-in rules: unattached EBS, gp2→gp3, idle Elastic IP, long-stopped instance (EBS still billed),
low-CPU instance (one size down), previous-generation family, orphaned old snapshots.

## Alternatives
| Choice | Alternative | Why not (yet) |
|---|---|---|
| Function registry | `Rule` ABC class hierarchy | More boilerplate, no extra capability |
| Function registry | Entry-point plugins | Natural next step if third parties need to add rules |
| Static prices | AWS Pricing API | Large, slow, needs network; ±10% is enough to rank waste |
| Own rules | Cloud Custodian / Trusted Advisor | Custodian is policy-as-code for *acting*; TA needs Business support for most checks. Here the point is a transparent, testable, explainable engine |

## Consequences
`audit --fail-over N` lets a scheduled job alert when potential waste exceeds a threshold.
