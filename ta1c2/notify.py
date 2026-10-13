"""Dead-man notifications. The collector pings SUCCESS only after a verified readback; silence
(no success ping within period + grace) is what the external service alerts on."""
from __future__ import annotations

import urllib.request

from .core import Registration, canonical


class RecordingNotifier:
    def __init__(self):
        self.events = []

    def start(self, slot):
        self.events.append(("start", slot, None))

    def success(self, slot, anchor):
        self.events.append(("success", slot, anchor))

    def fail(self, slot, reason):
        self.events.append(("fail", slot, reason))


class HealthchecksNotifier:
    """Pings a Healthchecks-style URL (<url>/start, <url>, <url>/fail). Inert unless armed exactly like
    LiveTransport. Notification errors never break the collector; the dead-man covers them."""

    def __init__(self, ping_url: str, reg: Registration, arm_token: str | None):
        if not (reg.data.get("approved") is True and reg.mode == "live" and arm_token == reg.sha256 and ping_url):
            raise PermissionError("notifier disarmed")
        self.url = ping_url.rstrip("/")
        self.errors = []

    def _post(self, suffix, payload):
        try:
            req = urllib.request.Request(self.url + suffix, data=canonical(payload), method="POST")
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:
            self.errors.append(f"{type(e).__name__}: {e}")

    def start(self, slot):
        self._post("/start", {"slot": slot})

    def success(self, slot, anchor):
        self._post("", {"slot": slot, "ledger_head": anchor})

    def fail(self, slot, reason):
        self._post("/fail", {"slot": slot, "reason": reason})
