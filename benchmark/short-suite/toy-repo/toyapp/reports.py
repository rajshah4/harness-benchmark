from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable


def money(value: Decimal) -> str:
    return f"${value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"


def build_monthly_report(rows: Iterable[dict[str, object]], month: str) -> dict[str, object]:
    normalized = []
    for row in rows:
        if row.get("month") != month:
            continue
        item = {
            "account": str(row.get("account", "unknown")),
            "region": str(row.get("region", "unknown")),
            "product": str(row.get("product", "unknown")),
            "revenue": Decimal(str(row.get("revenue", "0"))),
            "cost": Decimal(str(row.get("cost", "0"))),
            "tickets": int(row.get("tickets", 0)),
            "satisfaction": Decimal(str(row.get("satisfaction", "0"))),
        }
        item["margin"] = item["revenue"] - item["cost"]
        normalized.append(item)

    totals = {
        "revenue": Decimal("0"),
        "cost": Decimal("0"),
        "margin": Decimal("0"),
        "tickets": 0,
        "satisfaction": Decimal("0"),
    }
    for item in normalized:
        totals["revenue"] += item["revenue"]
        totals["cost"] += item["cost"]
        totals["margin"] += item["margin"]
        totals["tickets"] += item["tickets"]
        totals["satisfaction"] += item["satisfaction"]

    count = len(normalized)
    average_satisfaction = totals["satisfaction"] / count if count else Decimal("0")
    margin_rate = totals["margin"] / totals["revenue"] if totals["revenue"] else Decimal("0")

    by_region: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {
            "revenue": Decimal("0"),
            "cost": Decimal("0"),
            "margin": Decimal("0"),
            "tickets": 0,
            "satisfaction": Decimal("0"),
            "count": 0,
        }
    )
    for item in normalized:
        bucket = by_region[item["region"]]
        bucket["revenue"] += item["revenue"]
        bucket["cost"] += item["cost"]
        bucket["margin"] += item["margin"]
        bucket["tickets"] += item["tickets"]
        bucket["satisfaction"] += item["satisfaction"]
        bucket["count"] += 1

    region_rows = []
    for region, bucket in sorted(by_region.items()):
        avg_sat = bucket["satisfaction"] / bucket["count"] if bucket["count"] else Decimal("0")
        region_rows.append(
            {
                "region": region,
                "revenue": money(bucket["revenue"]),
                "cost": money(bucket["cost"]),
                "margin": money(bucket["margin"]),
                "tickets": bucket["tickets"],
                "average_satisfaction": str(avg_sat.quantize(Decimal("0.01"))),
            }
        )

    by_product: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {
            "revenue": Decimal("0"),
            "cost": Decimal("0"),
            "margin": Decimal("0"),
            "tickets": 0,
            "satisfaction": Decimal("0"),
            "count": 0,
        }
    )
    for item in normalized:
        bucket = by_product[item["product"]]
        bucket["revenue"] += item["revenue"]
        bucket["cost"] += item["cost"]
        bucket["margin"] += item["margin"]
        bucket["tickets"] += item["tickets"]
        bucket["satisfaction"] += item["satisfaction"]
        bucket["count"] += 1

    product_rows = []
    for product, bucket in sorted(by_product.items()):
        avg_sat = bucket["satisfaction"] / bucket["count"] if bucket["count"] else Decimal("0")
        product_rows.append(
            {
                "product": product,
                "revenue": money(bucket["revenue"]),
                "cost": money(bucket["cost"]),
                "margin": money(bucket["margin"]),
                "tickets": bucket["tickets"],
                "average_satisfaction": str(avg_sat.quantize(Decimal("0.01"))),
            }
        )

    by_account: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {
            "revenue": Decimal("0"),
            "cost": Decimal("0"),
            "margin": Decimal("0"),
            "tickets": 0,
            "satisfaction": Decimal("0"),
            "count": 0,
        }
    )
    for item in normalized:
        bucket = by_account[item["account"]]
        bucket["revenue"] += item["revenue"]
        bucket["cost"] += item["cost"]
        bucket["margin"] += item["margin"]
        bucket["tickets"] += item["tickets"]
        bucket["satisfaction"] += item["satisfaction"]
        bucket["count"] += 1

    account_rows = []
    for account, bucket in sorted(by_account.items()):
        avg_sat = bucket["satisfaction"] / bucket["count"] if bucket["count"] else Decimal("0")
        account_rows.append(
            {
                "account": account,
                "revenue": money(bucket["revenue"]),
                "cost": money(bucket["cost"]),
                "margin": money(bucket["margin"]),
                "tickets": bucket["tickets"],
                "average_satisfaction": str(avg_sat.quantize(Decimal("0.01"))),
            }
        )

    alerts = []
    for account in account_rows:
        if Decimal(account["margin"].replace("$", "")) < Decimal("0"):
            alerts.append(f"{account['account']} has negative margin")
        if account["tickets"] > 10:
            alerts.append(f"{account['account']} has high support volume")

    top_accounts = sorted(
        account_rows,
        key=lambda item: Decimal(item["revenue"].replace("$", "")),
        reverse=True,
    )[:3]
    top_products = sorted(
        product_rows,
        key=lambda item: Decimal(item["revenue"].replace("$", "")),
        reverse=True,
    )[:3]
    top_regions = sorted(
        region_rows,
        key=lambda item: Decimal(item["revenue"].replace("$", "")),
        reverse=True,
    )[:3]

    narrative = []
    narrative.append(f"Report for {month}")
    narrative.append(f"Revenue: {money(totals['revenue'])}")
    narrative.append(f"Cost: {money(totals['cost'])}")
    narrative.append(f"Margin: {money(totals['margin'])}")
    narrative.append(f"Margin rate: {(margin_rate * Decimal('100')).quantize(Decimal('0.01'))}%")
    narrative.append(f"Average satisfaction: {average_satisfaction.quantize(Decimal('0.01'))}")
    if alerts:
        narrative.append("Alerts: " + "; ".join(alerts))
    else:
        narrative.append("Alerts: none")

    return {
        "month": month,
        "row_count": count,
        "totals": {
            "revenue": money(totals["revenue"]),
            "cost": money(totals["cost"]),
            "margin": money(totals["margin"]),
            "tickets": totals["tickets"],
            "average_satisfaction": str(average_satisfaction.quantize(Decimal("0.01"))),
            "margin_rate": str(margin_rate.quantize(Decimal("0.0001"))),
        },
        "regions": region_rows,
        "products": product_rows,
        "accounts": account_rows,
        "top_accounts": top_accounts,
        "top_products": top_products,
        "top_regions": top_regions,
        "alerts": alerts,
        "narrative": "\n".join(narrative),
    }
