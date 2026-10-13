"""Independent verification of a stored evidence set: ledger chain, every artifact hash, and full
recomputation of derived results from the preserved raw bytes."""
from __future__ import annotations

import json

from . import ledger as L
from .analysis import BOOK_SOURCES, analyse
from .core import Registration, canonical, sha256
from .sources import Response


class EvidenceInvalid(Exception):
    pass


def verify_artifacts(backend, rows) -> int:
    """Re-hash every file referenced by every ATTEMPT row. Raises EvidenceInvalid on any mismatch."""
    n = 0
    for r in rows:
        if r["kind"] != "ATTEMPT" or not r.get("artifact_dir"):
            continue
        mbytes = backend.read(r["artifact_dir"] + "/MANIFEST.json")
        if mbytes is None or sha256(mbytes) != r["manifest_sha256"]:
            raise EvidenceInvalid(f"manifest missing/mismatch for seq {r['seq']}")
        for name, h in json.loads(mbytes).items():
            b = backend.read(f"{r['artifact_dir']}/{name}")
            if b is None or sha256(b) != h:
                raise EvidenceInvalid(f"artifact missing/mismatch: {r['artifact_dir']}/{name}")
            n += 1
    return n


def _responses_from_store(backend, adir, keys):
    out = {}
    for k in keys:
        m = json.loads(backend.read(f"{adir}/{k}.meta.json"))
        body = backend.read(f"{adir}/{k}.raw")
        out[k] = Response(k, m["url"], m["status"], m["headers"], body, m["request_utc"], m["receipt_utc"], m.get("error"))
    return out


def verify_store(reg: Registration, backend, anchor=None, recompute: bool = True) -> dict:
    raw = backend.read(L.LEDGER_PATH) or b""
    rows = L.parse(raw)
    L.validate(reg, rows, anchor)
    files = verify_artifacts(backend, rows)
    referenced = {L.LEDGER_PATH}
    prior = {}
    recomputed = 0
    for r in rows:
        if r["kind"] != "ATTEMPT" or not r.get("artifact_dir"):
            continue
        adir = r["artifact_dir"]
        man = json.loads(backend.read(adir + "/MANIFEST.json"))
        referenced |= {f"{adir}/{n}" for n in man} | {adir + "/MANIFEST.json"}
        if recompute:
            keys = sorted(r["sources"])
            resp = _responses_from_store(backend, adir, keys)
            again = analyse(resp, reg.sizes_aud, reg.freshness_limit_s, prior)
            stored = json.loads(backend.read(adir + "/derived.json"))
            if canonical(again) != canonical(stored):
                raise EvidenceInvalid(f"derived recomputation mismatch at seq {r['seq']}")
            recomputed += 1
        for k in BOOK_SOURCES:
            h = r["sources"].get(k, {}).get("sha256")
            if h:
                prior.setdefault(k, set()).add(h)
    orphans = [f for f in backend.list_files() if f.startswith("evidence/") and f not in referenced]
    return {"ledger_rows": len(rows), "ledger_sha256": sha256(raw), "head": (rows[-1]["seq"], rows[-1]["row_sha256"]) if rows else None,
            "artifacts_verified": files, "derived_recomputed": recomputed, "orphan_files": orphans,
            "slot_status": {str(k): v for k, v in sorted(L.slot_status(rows).items())}}
