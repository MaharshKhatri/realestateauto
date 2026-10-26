"""Equity estimation from public records.

NJ public records don't expose mortgage balances. We estimate the remaining balance on the
purchase-money mortgage assuming 80% LTV at the average 30-year fixed rate for the purchase
year, amortized to today. Refinances and HELOCs make the true balance unknowable from here, so
the estimate is labeled "likely" and only used for ranking, never shown to a seller as fact.
Purchases older than ~30 years are treated as likely free and clear.
"""
from __future__ import annotations

from datetime import date

# Freddie Mac PMMS 30-year fixed, annual averages (%), rounded.
PMMS_30Y = {
    1985: 12.43, 1986: 10.19, 1987: 10.21, 1988: 10.34, 1989: 10.32, 1990: 10.13, 1991: 9.25,
    1992: 8.39, 1993: 7.31, 1994: 8.38, 1995: 7.93, 1996: 7.81, 1997: 7.60, 1998: 6.94, 1999: 7.44,
    2000: 8.05, 2001: 6.97, 2002: 6.54, 2003: 5.83, 2004: 5.84, 2005: 5.87, 2006: 6.41, 2007: 6.34,
    2008: 6.03, 2009: 5.04, 2010: 4.69, 2011: 4.45, 2012: 3.66, 2013: 3.98, 2014: 4.17, 2015: 3.85,
    2016: 3.65, 2017: 3.99, 2018: 4.54, 2019: 3.94, 2020: 3.11, 2021: 2.96, 2022: 5.34, 2023: 6.81,
    2024: 6.72, 2025: 6.6, 2026: 6.4,
}


def remaining_balance(principal: float, annual_rate_pct: float, years: int, months_paid: int) -> float:
    n = years * 12
    if months_paid >= n:
        return 0.0
    r = annual_rate_pct / 100 / 12
    if r == 0:
        return principal * (1 - months_paid / n)
    payment = principal * r / (1 - (1 + r) ** -n)
    balance = principal * (1 + r) ** months_paid - payment * ((1 + r) ** months_paid - 1) / r
    return max(balance, 0.0)


def estimate_mortgage_balance(last_sale_price: float | None, last_sale_date: date | None,
                              today: date | None = None, ltv: float = 0.80) -> float | None:
    if not last_sale_price or not last_sale_date or last_sale_price < 10000:
        return None
    today = today or date.today()
    rate = PMMS_30Y.get(last_sale_date.year, 7.0 if last_sale_date.year < 1985 else 6.5)
    months = (today.year - last_sale_date.year) * 12 + (today.month - last_sale_date.month)
    return round(remaining_balance(last_sale_price * ltv, rate, 30, max(months, 0)), -2)


def likely_free_and_clear(last_sale_date: date | None, today: date | None = None) -> bool:
    if not last_sale_date:
        return False
    today = today or date.today()
    return (today - last_sale_date).days / 365.25 >= 30


def estimate_equity(value: float | None, last_sale_price: float | None, last_sale_date: date | None,
                    known_debt: float | None = None) -> float | None:
    """Equity = value - max(known debt, estimated purchase-money balance)."""
    if not value:
        return None
    est_balance = estimate_mortgage_balance(last_sale_price, last_sale_date) or 0.0
    debt = max(known_debt or 0.0, est_balance)
    return round(value - debt, -2)
