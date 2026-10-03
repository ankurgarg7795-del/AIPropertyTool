"""Indicative home-loan affordability (FOIR + LTV), the core of instant pre-qualification."""

from __future__ import annotations

from pydantic import BaseModel

DEFAULT_RATE = 0.0875  # annual, floating; refreshed daily from lender feed in production
DEFAULT_TENURE_YEARS = 20
FOIR = 0.50  # max share of net monthly income that can go to EMIs
LTV_BANDS = ((3_000_000, 0.90), (7_500_000, 0.80), (float("inf"), 0.75))  # RBI LTV caps


class Affordability(BaseModel):
    monthly_income_inr: float
    existing_emi_inr: float
    max_emi_inr: float
    max_loan_inr: float
    max_property_price_inr: float
    emi_for_target_inr: float | None = None
    target_price_inr: float | None = None
    verdict: str


def emi(principal: float, annual_rate: float = DEFAULT_RATE, years: int = DEFAULT_TENURE_YEARS) -> float:
    r, n = annual_rate / 12, years * 12
    return principal * r * (1 + r) ** n / ((1 + r) ** n - 1)


def loan_for_emi(monthly_emi: float, annual_rate: float = DEFAULT_RATE, years: int = DEFAULT_TENURE_YEARS) -> float:
    r, n = annual_rate / 12, years * 12
    return monthly_emi * ((1 + r) ** n - 1) / (r * (1 + r) ** n)


def ltv(price: float) -> float:
    return next(ratio for limit, ratio in LTV_BANDS if price <= limit)


def assess(monthly_income: float, existing_emi: float = 0.0, down_payment: float | None = None,
           target_price: float | None = None) -> Affordability:
    max_emi = max(0.0, monthly_income * FOIR - existing_emi)
    max_loan = loan_for_emi(max_emi)
    # Price reachable by the loan alone under LTV caps, optionally lifted by savings.
    by_loan = max_loan / 0.75
    for limit, ratio in LTV_BANDS:
        if max_loan / ratio <= limit:
            by_loan = max_loan / ratio
            break
    max_price = max_loan + down_payment if down_payment is not None else by_loan
    out = Affordability(monthly_income_inr=monthly_income, existing_emi_inr=existing_emi, max_emi_inr=round(max_emi),
                        max_loan_inr=round(max_loan), max_property_price_inr=round(max_price), verdict="")
    if target_price:
        loan_needed = target_price * ltv(target_price)
        if down_payment is not None:
            loan_needed = max(0.0, target_price - down_payment)
        out.target_price_inr = target_price
        out.emi_for_target_inr = round(emi(loan_needed))
        if out.emi_for_target_inr <= max_emi:
            out.verdict = "comfortable"
        elif out.emi_for_target_inr <= max_emi * 1.15:
            out.verdict = "stretch"
        else:
            out.verdict = "over_budget"
    else:
        out.verdict = "estimated"
    return out
