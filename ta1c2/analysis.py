"""Validation and derived calculations.

The book walk (`levels`, `sweep`), ECB conversion, fee scenarios and per-market checks are a
line-for-line port of frozen TA1-COST-14D.1 capture.py (code SHA-256 f83e610c…7536f8), so v2
results are directly comparable. Regression tests prove bit-identical liquidity_loss_bps on all
24 recorded captures. Additions: explicit per-source validity, freshness and duplicate flags.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import json
import math
import xml.etree.ElementTree as ET

from .core import parse_utc, sha256

BOOK_SOURCES = ("coinbase_BTC", "coinbase_ETH", "coinspot_depth_BTC", "coinspot_depth_ETH")


def levels(seq, p, s, reverse=False):
    rows = [(float(x[p]), float(x[s])) for x in seq]
    if not rows or any(not math.isfinite(a) or not math.isfinite(b) or a <= 0 or b <= 0 for a, b in rows):
        raise ValueError("invalid depth")
    return sorted(rows, reverse=reverse)


def sweep(asks, bids, budget):
    remain = budget; base = 0.0
    for p, q in asks:
        spent = min(remain, p * q); base += spent / p; remain -= spent
        if remain <= budget * 1e-10: break
    if remain > budget * 1e-10: raise ValueError("insufficient asks")
    remain = base; proceeds = 0.0
    for p, q in bids:
        sold = min(remain, q); proceeds += sold * p; remain -= sold
        if remain <= base * 1e-10: break
    if remain > base * 1e-10: raise ValueError("insufficient bids")
    return {"base_bought": base, "sale_proceeds": proceeds, "liquidity_loss_bps": 10000 * (budget - proceeds) / budget}


def analyse(responses: dict, sizes_aud: list, freshness_limit_s: int, prior_book_hashes: dict) -> dict:
    """Returns {'fx':…, 'markets':…, 'source_checks':{key:{valid,reason,…}}}. Never raises on bad data."""
    checks = {}
    for k, r in responses.items():
        checks[k] = {"http_status": r.status, "sha256": sha256(r.body) if r.body is not None else None,
                     "bytes": len(r.body) if r.body is not None else 0, "valid": False, "reason": None,
                     "duplicate_of_prior": False}
        if r.body is not None and k in BOOK_SOURCES and checks[k]["sha256"] in prior_book_hashes.get(k, set()):
            checks[k]["duplicate_of_prior"] = True

    result = {"fx": {"status": "UNKNOWN"}, "markets": {}, "source_checks": checks,
              "unknowns": ["account fees", "AUD-USDC executable conversion", "latency fills", "USDC-USD basis", "account eligibility"]}
    fx = None
    r = responses["ecb_fx"]
    try:
        assert r.status == 200, "HTTP request failed"
        tree = ET.fromstring(r.body)
        rates = {x.attrib["currency"]: float(x.attrib["rate"]) for x in tree.iter() if "currency" in x.attrib}
        date = next(x.attrib["time"] for x in tree.iter() if "time" in x.attrib)
        fx = rates["USD"] / rates["AUD"]; assert math.isfinite(fx) and fx > 0
        result["fx"] = {"status": "daily indicative sizing only", "publication_date": date, "usd_per_aud": fx, "USDC_parity": "assumed unverified"}
        checks["ecb_fx"].update(valid=True, publication_date=date)
    except Exception as e:
        result["fx"]["error"] = str(e) or type(e).__name__
        checks["ecb_fx"]["reason"] = result["fx"]["error"]

    for coin in ("BTC", "ETH"):
        for venue in ("coinbase", "coinspot_depth", "coinspot_quote"):
            key = f"{venue}_{coin}"; resp = responses[key]
            m = {"status": "INCOMPLETE", "http_status": resp.status}; result["markets"][key] = m
            try:
                assert resp.status == 200, "HTTP request failed"
                j = json.loads(resp.body)
                if venue == "coinspot_quote":
                    assert j["status"] == "ok"; p = j["prices"]; bid = float(p["bid"]); ask = float(p["ask"]); assert 0 < bid <= ask
                    m.update(status="indicative quote; product-fee mapping unresolved", bid=bid, ask=ask, quote_loss_bps=10000 * (1 - bid / ask))
                    checks[key]["valid"] = True
                    continue
                if venue == "coinbase":
                    book = j["pricebook"]; assert book["product_id"] == coin + "-USDC"
                    bids = levels(book["bids"], "price", "size", True); asks = levels(book["asks"], "price", "size")
                    server = dt.datetime.fromisoformat(book["time"].replace("Z", "+00:00")); receipt = parse_utc(resp.receipt_utc)
                    age = (receipt - server).total_seconds(); m.update(server_time=book["time"], server_age_seconds=age)
                    checks[key]["server_age_s"] = age
                    assert abs(age) <= freshness_limit_s, "stale server timestamp"
                else:
                    assert j["status"] == "ok"
                    for order in j["buyorders"] + j["sellorders"]:
                        assert order["market"] == coin + "/AUD" and order["coin"] == coin
                    bids = levels(j["buyorders"], "rate", "amount", True); asks = levels(j["sellorders"], "rate", "amount")
                    m["freshness"] = "server time unavailable; receipt time only"
                assert asks[0][0] >= bids[0][0], "crossed book"
                m.update(status="valid displayed depth; hypothetical liquidity only", bid=bids[0][0], ask=asks[0][0], sizes={})
                for aud in sizes_aud:
                    if venue == "coinbase" and fx is None:
                        m["sizes"][str(aud)] = {"status": "UNKNOWN FX"}; continue
                    budget = aud * fx if venue == "coinbase" else aud
                    try:
                        v = sweep(asks, bids, budget); v["budget_quote"] = budget
                        v["approx_fee_scenarios_bps"] = {str(f): v["liquidity_loss_bps"] + 20000 * f for f in ([.001, .004, .006] if venue == "coinbase" else [.001])}
                        v["fee_status"] = "hypothetical account scenarios" if venue == "coinbase" else "Markets public fee; no observed fill"
                        m["sizes"][str(aud)] = v
                    except Exception as e:
                        m["sizes"][str(aud)] = {"status": "UNKNOWN", "error": str(e)}
                depth_ok = all("liquidity_loss_bps" in m["sizes"].get(str(a), {}) for a in sizes_aud)
                checks[key]["depth_valid_all_sizes"] = depth_ok
                if checks[key]["duplicate_of_prior"]:
                    checks[key]["reason"] = "duplicate body of a prior captured book (not independent)"
                elif not depth_ok:
                    checks[key]["reason"] = "depth/FX insufficient for one or more sizes"
                else:
                    checks[key]["valid"] = True
            except Exception as e:
                m["error"] = str(e) or type(e).__name__
                checks[key]["reason"] = m["error"]
    for k, c in checks.items():
        if not c["valid"] and c["reason"] is None:
            c["reason"] = responses[k].error or "invalid"
        c["http_date_skew_s"] = http_date_skew(responses[k])
    return result


def http_date_skew(resp) -> float | None:
    """Runner receipt time minus the server's HTTP Date header (1 s resolution). Audits the runner
    clock, which is the trust root for slot assignment. Informational; the watchdog alerts on >120 s."""
    hdr = {str(k).lower(): v for k, v in (resp.headers or {}).items()}.get("date")
    if not hdr or not resp.receipt_utc:
        return None
    try:
        d = email.utils.parsedate_to_datetime(hdr)
        return round((parse_utc(resp.receipt_utc) - d).total_seconds(), 3)
    except (TypeError, ValueError):
        return None
