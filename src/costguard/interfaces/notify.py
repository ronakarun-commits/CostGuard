"""Alerting: decide whether anything needs a human, and post it to Slack.

Slack *incoming webhooks* are used (a URL that accepts a JSON POST): no bot token, no SDK, and the
URL can only post to one channel. It is a secret (anyone with it can post), so it lives in
``COSTGUARD_SLACK_WEBHOOK_URL`` as a ``SecretStr`` and is never logged.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from costguard.core.analytics.anomaly import Incident
from costguard.core.analytics.risk import MonthEndRisk
from costguard.errors import CostGuardError

log = logging.getLogger(__name__)


class NotifyError(CostGuardError):
    exit_code = 7


@dataclass(frozen=True, slots=True)
class Alert:
    title: str
    lines: list[str] = field(default_factory=list)
    severity: str = "warning"  # warning | critical

    @property
    def text(self) -> str:
        return "\n".join([f"*{self.title}*", *self.lines])


def build_alerts(
    incidents: list[Incident],
    risk: MonthEndRisk | None,
    *,
    breach_threshold: float = 0.5,
    currency: str = "USD",
    fx: float = 1.0,
) -> list[Alert]:
    """Turn analysis results into zero or more alerts. Pure function: easy to test."""
    sym = {"USD": "$", "INR": "₹"}.get(currency, "")
    alerts: list[Alert] = []
    if incidents:
        lines = [
            f"• {i.sub_account_name} / {i.service_name}: {i.peak_ratio:.1f}x usual, "
            f"{sym}{i.excess_cost * fx:,.0f} extra ({i.start_date}"
            f"{'' if i.end_date == i.start_date else f' → {i.end_date}'})"
            for i in incidents[:10]
        ]
        if len(incidents) > 10:
            lines.append(f"…and {len(incidents) - 10} more")
        total = sum(i.excess_cost for i in incidents) * fx
        severity = "critical" if total >= 500 * fx else "warning"
        alerts.append(Alert(f":rotating_light: {len(incidents)} cost anomal"
                            f"{'y' if len(incidents) == 1 else 'ies'} ({sym}{total:,.0f} excess)",
                            lines, severity))  # fmt: skip
    if risk and risk.breach_probability is not None and risk.breach_probability >= breach_threshold:
        assert risk.budget is not None  # noqa: S101 - breach_probability implies a budget
        alerts.append(
            Alert(
                f":chart_with_upwards_trend: {risk.breach_probability:.0%} chance of exceeding "
                f"the {sym}{risk.budget * fx:,.0f} budget",
                [
                    f"Month to date: {sym}{risk.month_to_date * fx:,.0f} "
                    f"({risk.remaining_days} days left)",
                    f"Expected month-end: {sym}{risk.expected * fx:,.0f}; "
                    f"{risk.level:.0%} Cost-at-Risk: {sym}{risk.car * fx:,.0f}",
                ],
                "critical" if risk.breach_probability >= 0.8 else "warning",
            )
        )
    return alerts


def slack_payload(alerts: list[Alert]) -> dict[str, object]:
    blocks: list[dict[str, object]] = []
    for a in alerts:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": a.text}})
        blocks.append({"type": "divider"})
    return {"text": " | ".join(a.title for a in alerts), "blocks": blocks[:-1]}


def post_slack(webhook_url: str, alerts: list[Alert], timeout: float = 10.0) -> None:
    if not webhook_url.startswith("https://hooks.slack.com/"):
        raise NotifyError("Slack webhook URL must start with https://hooks.slack.com/")
    body = json.dumps(slack_payload(alerts)).encode()
    req = urllib.request.Request(  # noqa: S310 - scheme checked above
        webhook_url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            if resp.status != 200:
                raise NotifyError(f"Slack returned HTTP {resp.status}")
    except urllib.error.URLError as exc:
        raise NotifyError(f"Could not post to Slack: {exc}") from exc
    log.info("slack alert sent", extra={"alerts": len(alerts)})
