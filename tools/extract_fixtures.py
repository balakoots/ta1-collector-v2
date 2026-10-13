"""Extract recorded fixtures from the preserved 14D.1 escalation archive (read-only; originals untouched).

usage: python -I tools/extract_fixtures.py ESCALATION_ZIP OUT_DIR
Verifies: container SHA-256, each capture ZIP hash against the original ledger, every raw body
against its meta digest and the ledger's per-source raw_sha256. Writes PROVENANCE.json.
"""
import datetime as dt
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

EXPECTED_CONTAINER = "fc41ba326f5f8691b75768c23f35d9b42902125346d1d1266139f74014cb7de6"
T0 = dt.datetime(2026, 10, 9, 3, tzinfo=dt.timezone.utc)
sha = lambda b: hashlib.sha256(b).hexdigest()


def main(zpath, out):
    raw = Path(zpath).read_bytes()
    assert sha(raw) == EXPECTED_CONTAINER, "escalation container hash mismatch"
    outer = zipfile.ZipFile(io.BytesIO(raw))
    ledger = {}
    for line in outer.read("originals/Trading_Agent_1_Cost_Run_Ledger.jsonl").splitlines():
        if line.strip():
            r = json.loads(line); ledger[r["slot_index"]] = r
    out = Path(out); (out / "recorded").mkdir(parents=True, exist_ok=True)
    prov = {"source_container": Path(zpath).name, "source_container_sha256": EXPECTED_CONTAINER,
            "kind": "RECORDED historical public HTTP entity bodies from TA1-COST-14D.1 (not live calls)", "captures": []}
    for name in sorted(n for n in outer.namelist() if n.startswith("originals/Trading_Agent_1_Cost_Capture_")):
        zb = outer.read(name); inner = zipfile.ZipFile(io.BytesIO(zb))
        mn = next(n for n in inner.namelist() if n.endswith("SHA256.json")); d = mn[: -len("SHA256.json")]
        man = json.loads(inner.read(mn))
        reg = json.loads(inner.read(d + "registration.json"))
        cu = dt.datetime.fromisoformat(reg["capture_utc"]); slot = int((cu - T0).total_seconds() // 3600) + 1
        lr = ledger[slot]
        assert lr["evidence_bundle"]["sha256"] == sha(zb), name
        cid = d.rstrip("/").split("/")[-1]
        dst = out / "recorded" / cid; dst.mkdir(exist_ok=True)
        files = {}
        for f, h in man.items():
            b = inner.read(d + f); assert sha(b) == h, f
            if f.endswith(".raw"):
                assert lr["sources"][f[:-4]]["raw_sha256"] == h, f
            target = {"derived.json": "derived_14D1.json", "registration.json": "registration_14D1.json"}.get(f, f)
            (dst / target).write_bytes(b); files[target] = h
        prov["captures"].append({"capture_id": cid, "slot_14d1": slot, "archive": name.split("/")[-1], "archive_sha256": sha(zb), "files": files})
    (out / "PROVENANCE.json").write_text(json.dumps(prov, indent=1, sort_keys=True))
    print(f"extracted {len(prov['captures'])} captures")


if __name__ == "__main__":
    main(*sys.argv[1:3])
