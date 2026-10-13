"""Source endpoints and transports. The live transport is inert unless explicitly armed."""
from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import VERSION
from .core import ConfigRefused, Registration, parse_utc, utc_iso

# Same seven unauthenticated public GET endpoints as frozen TA1-COST-14D.1 capture.py.
SOURCES: list[tuple[str, str]] = []
for _coin in ("BTC", "ETH"):
    SOURCES += [
        (f"coinbase_{_coin}", f"https://api.coinbase.com/api/v3/brokerage/market/product_book?product_id={_coin}-USDC&limit=50"),
        (f"coinspot_quote_{_coin}", f"https://www.coinspot.com.au/pubapi/v2/latest/{_coin}"),
        (f"coinspot_depth_{_coin}", f"https://www.coinspot.com.au/pubapi/v2/orders/open/{_coin}"),
    ]
SOURCES.append(("ecb_fx", "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"))
SOURCE_KEYS = [k for k, _ in SOURCES]


@dataclass
class Response:
    key: str
    url: str
    status: int | None
    headers: dict
    body: bytes | None
    request_utc: str
    receipt_utc: str
    error: str | None = None
    provenance: dict = field(default_factory=dict)


class LiveDisarmed(ConfigRefused):
    pass


class LiveTransport:
    """One unauthenticated GET per endpoint. Construction fails unless the registration is
    approved, in live mode, and the arming token equals the registration SHA-256."""

    def __init__(self, reg: Registration, arm_token: str | None):
        if not (reg.data.get("approved") is True and reg.mode == "live" and arm_token and arm_token == reg.sha256):
            raise LiveDisarmed("live transport disarmed: requires approved live registration and matching arm token")
        self.timeout = reg.request_timeout_s

    def fetch_all(self, clock) -> dict[str, Response]:
        def one(item):
            key, url = item
            req_t = utc_iso(clock.now())
            req = urllib.request.Request(url, headers={"Accept-Encoding": "identity", "User-Agent": f"TA1-ReadOnly-Cost-Evidence/{VERSION}"})
            try:
                try:
                    with urllib.request.urlopen(req, timeout=self.timeout) as r:
                        body = r.read()
                        return Response(key, url, r.status, dict(r.headers), body, req_t, utc_iso(clock.now()))
                except urllib.error.HTTPError as e:
                    return Response(key, url, e.code, dict(e.headers or {}), e.read(), req_t, utc_iso(clock.now()), error=str(e))
            except Exception as e:  # network failure is recorded, never raised
                return Response(key, url, None, {}, None, req_t, utc_iso(clock.now()), error=f"{type(e).__name__}: {e}")

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
            return {r.key: r for r in pool.map(one, SOURCES)}


class FixtureTransport:
    """Replays a recorded 14D.1 capture (raw bytes + recorded status/headers/timing).

    Request/receipt times are re-based on the virtual clock using the recorded offsets from the
    recorded run start, so replaying at the original time reproduces original server ages exactly."""

    def __init__(self, capture_dir: Path):
        self.dir = Path(capture_dir)
        self.metas = {k: json.loads((self.dir / f"{k}.meta.json").read_bytes()) for k in SOURCE_KEYS}
        self.t0 = min(parse_utc(m["request_utc"]) for m in self.metas.values())

    def fetch_all(self, clock) -> dict[str, Response]:
        base = clock.now()
        out = {}
        for key, url in SOURCES:
            m = self.metas[key]
            if m["url"] != url:
                raise ConfigRefused(f"fixture URL mismatch for {key}")
            raw_p = self.dir / f"{key}.raw"
            body = raw_p.read_bytes() if raw_p.exists() else None
            rq = base + (parse_utc(m["request_utc"]) - self.t0)
            rc = base + (parse_utc(m["receipt_utc"]) - self.t0)
            out[key] = Response(key, url, m.get("status"), m.get("headers", {}), body, utc_iso(rq), utc_iso(rc),
                                error=m.get("error"), provenance={"fixture_kind": "RECORDED_14D1", "fixture_dir": self.dir.name})
        return out


class FaultTransport:
    """Wraps another transport and injects SYNTHETIC faults for named sources (clearly labelled)."""

    def __init__(self, inner, faults: dict[str, str]):
        self.inner, self.faults = inner, faults

    def fetch_all(self, clock) -> dict[str, Response]:
        out = self.inner.fetch_all(clock)
        for key, fault in self.faults.items():
            r = out[key]
            prov = {"fixture_kind": "SYNTHETIC_FAULT", "fault": fault}
            if fault == "HTTP_500":
                out[key] = Response(key, r.url, 500, {"Content-Type": "application/json"}, b'{"synthetic":"HTTP 500 fault fixture"}', r.request_utc, r.receipt_utc, "HTTP Error 500 (synthetic)", prov)
            elif fault == "TIMEOUT":
                out[key] = Response(key, r.url, None, {}, None, r.request_utc, r.receipt_utc, "TimeoutError: synthetic", prov)
            elif fault == "TRUNCATED":
                out[key] = Response(key, r.url, r.status, r.headers, (r.body or b"")[: max(1, len(r.body or b"") // 2)], r.request_utc, r.receipt_utc, None, prov)
            elif fault == "LATE_RECEIPT":
                late = parse_utc(r.receipt_utc) + dt.timedelta(minutes=45)
                out[key] = Response(key, r.url, r.status, r.headers, r.body, r.request_utc, utc_iso(late), None, prov)
            else:
                raise ValueError("unknown fault " + fault)
        return out
