"""Fixture provenance and bit-exact regression against frozen 14D.1 results."""
import hashlib
import json
import unittest

from helpers import CAPTURES, FIX, PROV, cap_dir, cap_start, NetworkBlocked  # noqa: F401  (installs guard)
from ta1c2.analysis import analyse
from ta1c2.core import VirtualClock
from ta1c2.sources import SOURCE_KEYS, FixtureTransport


class Fixtures(unittest.TestCase):
    def test_provenance_hashes(self):
        self.assertEqual(PROV["source_container_sha256"], "fc41ba326f5f8691b75768c23f35d9b42902125346d1d1266139f74014cb7de6")
        self.assertEqual(len(CAPTURES), 24)
        n = 0
        for c in CAPTURES:
            for name, h in c["files"].items():
                self.assertEqual(hashlib.sha256((cap_dir(c) / name).read_bytes()).hexdigest(), h, name); n += 1
        self.assertEqual(n, 24 * 16)

    def test_echo_fixture_is_slot1_coinbase_btc(self):
        b = (cap_dir(CAPTURES[0]) / "coinbase_BTC.raw").read_bytes()
        self.assertEqual(hashlib.sha256(b).hexdigest(), "002ca94d652ed938d68adcd0883a341a1ae6681bc09079022757576dff80c6ca")


class Regression14D1(unittest.TestCase):
    def test_bit_exact_derived_all_24(self):
        """Replaying each recorded capture at its recorded time reproduces every frozen 14D.1 figure exactly."""
        compared = 0
        for c in CAPTURES:
            resp = FixtureTransport(cap_dir(c)).fetch_all(VirtualClock(cap_start(c)))
            got = analyse(resp, [500, 1000, 2500], 60, {})
            old = json.loads((cap_dir(c) / "derived_14D1.json").read_text())
            self.assertEqual(got["fx"], old["fx"])
            for mk, om in old["markets"].items():
                gm = got["markets"][mk]
                self.assertEqual(gm["status"], om["status"], (c["capture_id"], mk))
                for size, ov in om.get("sizes", {}).items():
                    self.assertEqual(gm["sizes"][size]["liquidity_loss_bps"], ov["liquidity_loss_bps"])
                    self.assertEqual(gm["sizes"][size]["approx_fee_scenarios_bps"], ov["approx_fee_scenarios_bps"])
                    compared += 1
                if "server_age_seconds" in om:
                    self.assertAlmostEqual(gm["server_age_seconds"], om["server_age_seconds"], places=5)
            self.assertTrue(all(got["source_checks"][k]["valid"] for k in SOURCE_KEYS), c["capture_id"])
        self.assertEqual(compared, 288)


if __name__ == "__main__":
    unittest.main()
