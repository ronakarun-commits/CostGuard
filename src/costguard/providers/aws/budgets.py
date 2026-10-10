"""AWS Budgets adapter (read-only: budgets:ViewBudget). Budgets API is free."""

from __future__ import annotations

from typing import TYPE_CHECKING

import boto3

from costguard.core.models import Budget
from costguard.providers.aws.session import aws_errors, client

if TYPE_CHECKING:
    from mypy_boto3_budgets.client import BudgetsClient


def get_budgets(session: boto3.Session, account_id: str) -> list[Budget]:
    budgets: BudgetsClient = client(session, "budgets", region="us-east-1")
    out: list[Budget] = []
    with aws_errors("budgets:DescribeBudgets", permission="budgets:ViewBudget"):
        for page in budgets.get_paginator("describe_budgets").paginate(AccountId=account_id):
            for b in page.get("Budgets", []):
                if b.get("BudgetType") != "COST" or "BudgetLimit" not in b:
                    continue
                out.append(
                    Budget(
                        name=b["BudgetName"],
                        amount=float(b["BudgetLimit"]["Amount"]),
                        currency=b["BudgetLimit"]["Unit"],
                        period=b["TimeUnit"],
                    )
                )
    return out
