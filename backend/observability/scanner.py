"""Autonomous background scanner for the audit log.

A daemon thread wakes on an interval, runs the analyzer over the recent window,
and - when it finds something - appends a persistent ``security`` event with the
evidence. That is what makes detection work "in autonomous mode": nobody has to
be looking at a page for a finding to be recorded and picked up later.

A finding is re-raised at most once per cooldown so a long-running condition
(e.g. a tolerated VAPT scan) does not bury the log; the first alert is the one
that matters and the cooldown keeps it visible without becoming noise.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from observability.analyzer import analyze, summarize


class SecurityScanner:
    def __init__(
        self,
        log,
        *,
        interval: int = 300,
        window_seconds: int = 3600,
        cooldown_seconds: int = 1800,
        scan_limit: int = 5000,
    ) -> None:
        self.log = log
        self.interval = max(10, int(interval))
        self.window_seconds = max(60, int(window_seconds))
        self.cooldown_seconds = max(60, int(cooldown_seconds))
        self.scan_limit = max(100, int(scan_limit))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_alert: dict[str, float] = {}
        self._lock = threading.Lock()
        self.last_findings: list[dict] = []

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="apex-security-scan", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=2)

    def _loop(self) -> None:
        # A short initial settle so startup logging is not scanned mid-write.
        if self._stop.wait(min(10, self.interval)):
            return
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception:
                pass
            if self._stop.wait(self.interval):
                return

    # -- work -------------------------------------------------------------
    def scan_once(self) -> list[dict]:
        since = time.time() - self.window_seconds
        records = self.log.read(limit=self.scan_limit, since=since)
        findings = analyze(records)
        self.last_findings = findings
        now = time.time()
        for finding in findings:
            rule = finding["id"]
            with self._lock:
                last = self._last_alert.get(rule, 0.0)
                if now - last < self.cooldown_seconds:
                    continue
                self._last_alert[rule] = now
            self.log.log(
                "security",
                "security",
                "security_alert",
                rule=rule,
                severity=finding["severity"],
                description=finding["description"],
                count=finding["count"],
                evidence=finding["evidence"],
                source="scanner",
            )
        if findings:
            self.log.log("warning", "system", "scan_summary", message=summarize(findings))
        return findings


_SCANNER_STARTED = False


def start_scanner(log, config: Any) -> SecurityScanner | None:
    """Build and start the scanner if the feature and auto-scan are enabled.

    At most one scanner runs per process: ``create_app`` may be called more than
    once (tests, tooling), and each call must not spawn a new scanning thread.
    """
    global _SCANNER_STARTED
    if not getattr(config, "OBSERVABILITY_ENABLED", True):
        return None
    if not getattr(config, "OBSERVABILITY_AUTO_SCAN", True):
        return None
    if _SCANNER_STARTED:
        return None
    _SCANNER_STARTED = True
    scanner = SecurityScanner(
        log,
        interval=getattr(config, "OBSERVABILITY_SCAN_INTERVAL", 300),
        window_seconds=getattr(config, "OBSERVABILITY_SCAN_WINDOW", 3600),
        cooldown_seconds=getattr(config, "OBSERVABILITY_ALERT_COOLDOWN", 1800),
    )
    log.log("info", "system", "scanner_started", interval=scanner.interval)
    scanner.start()
    return scanner


def reset_scanner() -> None:
    """Allow a new scanner to start (tests only)."""
    global _SCANNER_STARTED
    _SCANNER_STARTED = False
