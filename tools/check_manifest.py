"""Verify every file against SHA256SUMS (run from repo root: python -I tools/check_manifest.py [--write])."""
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {"SHA256SUMS"}


def files():
    for p in sorted(ROOT.rglob("*")):
        rel = p.relative_to(ROOT).as_posix()
        if p.is_file() and rel not in SKIP and "__pycache__" not in rel and not rel.startswith("evidence_out/"):
            yield rel, hashlib.sha256(p.read_bytes()).hexdigest()


if "--write" in sys.argv:
    (ROOT / "SHA256SUMS").write_text("".join(f"{h}  {r}\n" for r, h in files()))
    print("written")
    sys.exit(0)
want = {}
for line in (ROOT / "SHA256SUMS").read_text().splitlines():
    h, r = line.split("  ", 1); want[r] = h
have = dict(files())
bad = sorted(r for r in want if have.get(r) != want[r])
extra = sorted(set(have) - set(want))
print(f"checked {len(want)} files; mismatched/missing: {bad or 'none'}; unlisted: {extra or 'none'}")
sys.exit(1 if bad or extra else 0)
