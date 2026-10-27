"""homeharvest-backed comps / listings provider for the REOS underwriting engine.

Standalone module (recon deliverable). Wraps ``homeharvest.scrape_property`` (which calls
Realtor.com's internal GraphQL endpoint ``https://www.realtor.com/frontdoor/graphql``) and
returns plain, JSON-safe, normalized dicts.

    from homeharvest_provider import sold_comps, active_listings
    comps = sold_comps("07095")                                     # zip, last 180 days
    comps = sold_comps("186 Newark Ave, Bloomfield, NJ 07003", radius_miles=0.5)
    listings = active_listings("07095", past_days=30)

Notes (see docs/recon/comps_listings.md for the evidence):
* Tested against homeharvest 0.8.18 (pip), 2026-10-26.
* homeharvest scrapes an undocumented Realtor.com API. Realtor.com's terms of use prohibit
  automated scraping; treat this as a best-effort, low-volume source that can break or be
  blocked at any time. A 403 from Realtor.com is raised as ``ProviderBlocked`` so the caller
  can fall back to another source.
* Calls are throttled in-process (``MIN_INTERVAL_S`` between requests) and run with
  ``parallel=False`` so pagination is sequential.
* Sold results mix MLS closings (``mls`` set, beds/baths/list data present) with public-record
  deed sales (``mls`` is None: no list price/DOM/description, beds usually missing, and some
  non-arm's-length or portfolio transfers). Filter/weight on ``mls`` in the engine.
* Active ("for_sale") results include PENDING / CONTINGENT listings (see ``status``) and the
  same home is often returned once per MLS feed; this module de-duplicates by property_id.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from datetime import date, datetime, timezone
from typing import Any

SOURCE = "homeharvest"
MIN_INTERVAL_S = 2.0  # minimum seconds between homeharvest calls from this process

# Realtor.com description.type values treated as residential when property_types is None.
RESIDENTIAL_TYPES = {
    "single_family", "multi_family", "duplex_triplex", "condos", "condo", "condop", "coop",
    "townhomes", "condo_townhome", "condo_townhome_rowhome_coop", "apartment",
}

# Valid homeharvest property_type filter values (homeharvest.core.scrapers.models.SearchPropertyType).
HH_PROPERTY_TYPES = {
    "single_family", "apartment", "condos", "condo_townhome_rowhome_coop", "condo_townhome",
    "townhomes", "duplex_triplex", "farm", "land", "multi_family", "mobile",
}

DISTRESS_PATTERNS = {
    "as-is": r"\bas[\s-]?is\b",
    "cash only": r"\bcash[\s-]?only\b",
    "investor special": r"\binvestor(?:'s)?\s+special\b",
    "investor": r"\binvestors?\b",
    "needs tlc": r"\btlc\b",
    "estate sale": r"\bestate\s+sale\b",
    "handyman": r"\bhandy\s?man\b",
    "short sale": r"\bshort\s+sale\b",
    "tenant occupied": r"\btenant[\s-]?occupied\b",
    "fixer": r"\bfixer(?:[\s-]?upper)?\b|\bneeds\s+(?:some\s+)?work\b|\brehab\b",
    "bank owned / reo": r"\bbank[\s-]?owned\b|\breo\b|\bforeclos",
}
_DISTRESS_RE = {k: re.compile(v, re.I) for k, v in DISTRESS_PATTERNS.items()}


class ProviderError(RuntimeError):
    """homeharvest call failed (network, parsing, unexpected response)."""


class ProviderBlocked(ProviderError):
    """Realtor.com refused the request (HTTP 403 / auth failure). Back off or fall back."""


_lock = threading.Lock()
_last_call = 0.0


def _ensure_ca_bundle() -> None:
    # In the Claude Code sandbox, outbound HTTPS is re-terminated by a proxy; requests must trust its CA.
    bundle = "/root/.ccr/ca-bundle.crt"
    if not os.environ.get("REQUESTS_CA_BUNDLE") and not os.environ.get("SSL_CERT_FILE") and os.path.exists(bundle):
        os.environ["REQUESTS_CA_BUNDLE"] = bundle


def _scrape(**kwargs) -> list[dict]:
    """Throttled call to homeharvest.scrape_property(return_type='raw')."""
    global _last_call
    _ensure_ca_bundle()
    try:
        from homeharvest import scrape_property
        from homeharvest.exceptions import AuthenticationError
    except ImportError as e:  # pragma: no cover
        raise ProviderError("homeharvest is not installed: pip install homeharvest") from e

    with _lock:
        wait = MIN_INTERVAL_S - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            return scrape_property(return_type="raw", parallel=False, **kwargs) or []
        except AuthenticationError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            raise ProviderBlocked(f"Realtor.com refused the request (status={status}): {e}") from e
        except Exception as e:
            if "403" in str(e):
                raise ProviderBlocked(str(e)) from e
            raise ProviderError(f"{type(e).__name__}: {e}") from e
        finally:
            _last_call = time.monotonic()


# ----------------------------------------------------------------------------- helpers

def _clean(v: Any) -> Any:
    """Recursively make a value JSON-safe: NaN/inf -> None, datetimes -> ISO strings."""
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_clean(x) for x in v]
    try:  # pandas NA / numpy scalars, if they ever appear
        if v != v:
            return None
    except Exception:
        pass
    if hasattr(v, "item"):
        return _clean(v.item())
    return str(v)


def _num(v: Any) -> float | int | None:
    v = _clean(v)
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        s = str(v).replace(",", "").replace("$", "").strip()
        f = float(s)
        return int(f) if f.is_integer() else f
    except ValueError:
        return None


def _parse_dt(v: Any) -> datetime | None:
    if not v:
        return None
    s = str(v).strip()
    try:
        if len(s) == 10:
            return datetime.strptime(s, "%Y-%m-%d")
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt
    except ValueError:
        return None


def _iso_date(v: Any) -> str | None:
    dt = _parse_dt(v)
    return dt.date().isoformat() if dt else None


def _details_kv(row: dict) -> dict[str, str]:
    """Flatten Realtor 'details' blocks ("Original List Price: 90000") into a dict."""
    out: dict[str, str] = {}
    for block in row.get("details") or []:
        for t in (block or {}).get("text") or []:
            if isinstance(t, str) and ":" in t:
                k, v = t.split(":", 1)
                out.setdefault(k.strip(), v.strip())
    return out


def _best_estimate(row: dict) -> int | None:
    ests = row.get("current_estimates") or ((row.get("estimates") or {}).get("currentValues")) or []
    best = [e for e in ests if e and e.get("isBestHomeValue")] or ests
    return _num(best[0].get("estimate")) if best and best[0] else None


def _normalize(row: dict, kind: str) -> dict:
    addr = ((row.get("location") or {}).get("address")) or {}
    coord = addr.get("coordinate") or {}
    desc = row.get("description") or {}
    flags = row.get("flags") or {}
    kv = _details_kv(row)

    beds = _num(desc.get("beds"))
    if beds == 0 and (desc.get("type") or "") in ("multi_family", "duplex_triplex"):
        beds = None  # NJ MLS feeds report 0 beds on many 2-4 family listings (= unknown)
    full = _num(desc.get("baths_full"))
    half = _num(desc.get("baths_half"))
    baths = None if full is None and half is None else (full or 0) + 0.5 * (half or 0)
    sqft = _num(desc.get("sqft"))
    list_price = _num(row.get("list_price"))
    list_dt = _parse_dt(row.get("list_date"))
    last_sale_price = _num(row.get("last_sold_price")) or _num(desc.get("sold_price"))
    last_sale_dt = _parse_dt(row.get("last_sold_date"))

    if kind == "sold":
        sold_price, sold_dt = last_sale_price, last_sale_dt
        end = sold_dt
    else:
        sold_price, sold_dt = None, None
        end = datetime.now(timezone.utc).replace(tzinfo=None)
    dom = (end.date() - list_dt.date()).days if (list_dt and end and end >= list_dt) else None

    basis = sold_price if kind == "sold" else list_price
    ppsf = round(basis / sqft, 2) if (basis and sqft) else _num(row.get("price_per_sqft"))

    # "Original List Price" only appears in some MLS feeds' details (in testing: Hudson County MLS
    # (HCNJ), plus a handful of STNY rows). Observed typos (e.g. 789900000 vs list 789900), so require a plausible ratio.
    original = _num(kv.get("Original List Price"))
    if flags.get("is_price_reduced") is not None:
        price_reduced = bool(flags["is_price_reduced"])
    elif original and list_price and 0.5 <= original / list_price <= 2.0:
        price_reduced = list_price < original
    elif "price_reduced" in (row.get("tags") or []):
        price_reduced = True
    else:
        price_reduced = None

    text = desc.get("text")
    hits = [k for k, rx in _DISTRESS_RE.items() if text and rx.search(text)]
    src = row.get("source") or {}
    status = row.get("status")
    if kind != "sold":
        status = "pending" if flags.get("is_pending") else "contingent" if flags.get("is_contingent") else status

    return {
        "address": addr.get("line"),
        "city": addr.get("city"),
        "state": addr.get("state_code"),
        "zip": addr.get("postal_code"),
        "lat": _num(coord.get("lat")),
        "lon": _num(coord.get("lon")),
        "property_type": desc.get("type"),
        "beds": beds,
        "baths": baths,
        "sqft": sqft,
        "lot_sqft": _num(desc.get("lot_sqft")),
        "year_built": _num(desc.get("year_built")),
        "sold_price": sold_price,
        "sold_date": sold_dt.date().isoformat() if sold_dt else None,
        "list_price": list_price,
        "list_date": list_dt.date().isoformat() if list_dt else None,
        "days_on_market": dom,
        "price_per_sqft": ppsf,
        "description": text,
        "url": row.get("href"),
        "mls_status": row.get("mls_status"),
        "source": SOURCE,
        "price_reduced": price_reduced,
        # extras useful to the engine
        "property_id": row.get("property_id"),
        "mls": src.get("id"),  # None => public-record (non-MLS) sale
        "status": status,
        "original_list_price": original,
        "last_sale_price": last_sale_price,
        "last_sale_date": last_sale_dt.date().isoformat() if last_sale_dt else None,
        "estimated_value": _best_estimate(row),
        "distress_keywords": hits,
        "raw": _clean(row),
    }


def _richness(r: dict) -> tuple:
    return (r["description"] is not None, sum(v is not None for k, v in r.items() if k != "raw"),
            (r["raw"].get("last_update_date") or ""))


# ----------------------------------------------------------------------------- public API

def sold_comps(location: str, radius_miles: float | None = None, past_days: int = 180,
               property_types: list[str] | None = None) -> list[dict]:
    """Sold properties for a zip / city / county / address.

    ``radius_miles`` only applies when ``location`` resolves to a street address (homeharvest
    "comps" mode, centred on the address). ``property_types`` uses homeharvest names
    (single_family, multi_family, condos, townhomes, duplex_triplex, ...) and is applied
    server-side; when None, results are filtered client-side to residential types.
    """
    kwargs: dict[str, Any] = {"location": location, "listing_type": "sold", "past_days": past_days}
    if radius_miles:
        kwargs["radius"] = float(radius_miles)
    if property_types:
        bad = [p for p in property_types if p.lower() not in HH_PROPERTY_TYPES]
        if bad:
            raise ValueError(f"unknown property_types {bad}; valid: {sorted(HH_PROPERTY_TYPES)}")
        kwargs["property_type"] = [p.lower() for p in property_types]

    rows = [_normalize(r, "sold") for r in _scrape(**kwargs)]
    if not property_types:
        rows = [r for r in rows if (r["property_type"] or "").lower() in RESIDENTIAL_TYPES]

    # Drop exact duplicates (same address + sale date + price reported under two property ids).
    seen, out = set(), []
    for r in sorted(rows, key=_richness, reverse=True):
        key = (re.sub(r"[^A-Z0-9]", "", (r["address"] or "").upper()), r["zip"], r["sold_date"], r["sold_price"])
        if r["address"] and key in seen:
            continue
        seen.add(key)
        out.append(r)
    out.sort(key=lambda r: r["sold_date"] or "", reverse=True)
    return out


def active_listings(location: str, past_days: int | None = None) -> list[dict]:
    """For-sale listings (includes pending/contingent; see ``status``), de-duplicated by property.

    ``past_days`` filters on list date (listed within the last N days).
    """
    kwargs: dict[str, Any] = {"location": location, "listing_type": "for_sale"}
    if past_days:
        kwargs["past_days"] = int(past_days)
    rows = [_normalize(r, "for_sale") for r in _scrape(**kwargs)]

    best: dict[str, dict] = {}
    for r in rows:  # same home is returned once per MLS feed (GSNJ / MCNJ / HCNJ / ...)
        k = r["property_id"] or r["url"]
        if k not in best or _richness(r) > _richness(best[k]):
            best[k] = r
    return sorted(best.values(), key=lambda r: r["list_date"] or "", reverse=True)


# ----------------------------------------------------------------------------- demo

def _summary(name: str, rows: list[dict], secs: float, n_calls_note: str = "") -> None:
    keys = ["sqft", "beds", "baths", "year_built", "lot_sqft", "sold_price", "sold_date", "list_price",
            "list_date", "days_on_market", "lat", "price_per_sqft", "description", "price_reduced", "mls"]
    print(f"\n=== {name}: {len(rows)} rows in {secs:.2f}s {n_calls_note}")
    if not rows:
        return
    print("fill %: " + ", ".join(f"{k}={100 * sum(r[k] is not None for r in rows) / len(rows):.0f}" for k in keys))
    types: dict[str, int] = {}
    for r in rows:
        types[r["property_type"] or "None"] = types.get(r["property_type"] or "None", 0) + 1
    print("property_type:", types)


if __name__ == "__main__":
    import sys

    loc = sys.argv[1] if len(sys.argv) > 1 else "07095"
    try:
        from importlib.metadata import version
        print(f"homeharvest {version('homeharvest')} | location={loc} | {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%SZ}")
    except Exception:
        pass

    t0 = time.perf_counter()
    sold = sold_comps(loc, past_days=180)
    _summary(f"sold_comps({loc!r}, past_days=180)", sold, time.perf_counter() - t0)
    if sold:
        mls_n = sum(1 for r in sold if r["mls"])
        prices = sorted(r["sold_price"] for r in sold if r["sold_price"])
        print(f"MLS-backed: {mls_n}, public-record only: {len(sold) - mls_n}; median sold_price: {prices[len(prices) // 2]:,}")
        for r in sold[:3]:
            print("  ", json.dumps({k: r[k] for k in ("address", "city", "zip", "sold_price", "sold_date", "sqft", "beds",
                                                       "baths", "year_built", "lat", "lon", "property_type", "mls",
                                                       "days_on_market", "price_per_sqft")}))

    t0 = time.perf_counter()
    act = active_listings(loc)
    _summary(f"active_listings({loc!r})", act, time.perf_counter() - t0)
    if act:
        st: dict[str, int] = {}
        for r in act:
            st[r["status"] or "None"] = st.get(r["status"] or "None", 0) + 1
        red = [r for r in act if r["price_reduced"]]
        print("status:", st, "| price_reduced True/False/None:",
              len(red), sum(r["price_reduced"] is False for r in act), sum(r["price_reduced"] is None for r in act))
        kw: dict[str, int] = {}
        for r in act:
            for k in r["distress_keywords"]:
                kw[k] = kw.get(k, 0) + 1
        print("distress keyword hits:", kw)
        for r in (red[:2] + [r for r in act if r["distress_keywords"]][:2]):
            print("  ", json.dumps({k: r[k] for k in ("address", "zip", "list_price", "original_list_price", "list_date",
                                                       "days_on_market", "status", "price_reduced", "distress_keywords",
                                                       "url")}))
        json.dumps(act[0])  # proves rows (incl. raw) are JSON-serialisable
        print("JSON-serialisable: ok; raw keys:", len(act[0]["raw"]))
