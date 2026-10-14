"""Suspicious/malicious activity detection over the audit log.

The rules here are a *first pass*, not the only judgement: they raise findings
with the evidence that triggered them so a human (or the agent, on request) can
decide. Two families live side by side:

* **signature rules** match a command/payload shape (a reverse shell, a pipe
  from ``curl`` into a shell, credential access, a destructive command);
* **behavioural rules** count an event shape over a window (a burst of failed
  logins, CSRF rejections, or 5xx responses).

The analyzer is deliberately conservative about *authorised* tooling: the VAPT
skill runs reconnaissance tools by design, so the recon rule ignores events whose
tool is a ``vapt_*`` tool, while the very same command through ``terminal_command``
still raises. Everything else is surfaced, and a false positive costs an
operator one glance.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass(frozen=True)
class Rule:
    id: str
    severity: str
    description: str
    pattern: re.Pattern
    ignore_tools: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()


SIGNATURE_RULES: tuple[Rule, ...] = (
    Rule(
        "reverse_shell",
        "critical",
        "A reverse-shell / interactive remote shell pattern was invoked.",
        re.compile(
            r"(/dev/tcp/|(?:^|\s)(?:nc|ncat|netcat)\s+(?:-\w+\s+)*-[a-z]*e\b|"
            r"bash\s+-i\s*(?:>&|>|&>)|sh\s+-i\s*(?:>&|>|&>)|socat\s+.*\bexec\b)",
            re.IGNORECASE | re.MULTILINE,
        ),
    ),
    Rule(
        "remote_exec_pipe",
        "critical",
        "Remote content was piped straight into a shell/interpreter.",
        re.compile(
            r"(?:curl|wget|fetch)\b[^\n|]*\|\s*(?:sudo\s+)?(?:ba|z|k)?sh\b|"
            r"(?:curl|wget)\b[^\n|]*\|\s*(?:python3?|perl|ruby|node)\b",
            re.IGNORECASE,
        ),
    ),
    Rule(
        "base64_shell",
        "high",
        "A base64-encoded payload was decoded and executed.",
        re.compile(
            r"(?:base64\s+(?:-d|--decode)|echo\s+[A-Za-z0-9+/=]{40,})\s*\|?\s*"
            r"(?:sudo\s+)?(?:ba|z)?sh\b",
            re.IGNORECASE,
        ),
    ),
    Rule(
        "recon_scan",
        "medium",
        "A network/host reconnaissance tool was run outside the VAPT skill.",
        re.compile(
            r"(?:^|\s)(?:nmap|masscan|netdiscover|arp-scan|nikto|gobuster|"
            r"feroxbuster|dirb|dirsearch|sqlmap|hydra|medusa|patator|msfconsole|"
            r"metasploit|responder|enum4linux)(?:\s|$)",
            re.IGNORECASE | re.MULTILINE,
        ),
        ignore_tools=("vapt_",),
    ),
    Rule(
        "credential_access",
        "high",
        "A command touched credential material (env files, shadow, ssh/aws keys).",
        re.compile(
            r"(?:cat|less|more|head|tail|strings|grep|cp|scp|tar|zip|base64)\b[^\n]*"
            r"(?:/etc/shadow|/etc/passwd|\.env\b|credentials\.json|id_rsa|id_ed25519|"
            r"\.ssh/|\.aws/|\.gnupg/|session\.key|settings\.db|integrations\.db)",
            re.IGNORECASE,
        ),
    ),
    Rule(
        "privilege_escalation",
        "high",
        "A privilege-escalation technique was attempted.",
        re.compile(
            r"(chmod\s+777\b|chown\s+root\b|sudo\s+su\b|sudo\s+-i\b|"
            r"usermod\s+-aG\s+(?:sudo|wheel)|visudo|/etc/sudoers|"
            r"setcap\s+cap_setuid|pkexec\s)",
            re.IGNORECASE,
        ),
    ),
    Rule(
        "destructive_command",
        "critical",
        "A destructive filesystem/device command was invoked.",
        re.compile(
            r"(?:rm\s+-rf?\s+/(?:\s|$|\*)|dd\s+if=/dev/(?:zero|urandom)\s+of=/dev/|"
            r"mkfs(?:\.\w+)?\s|>\s*/dev/sd[a-z]|:\(\)\s*\{\s*:\|:&\s*\}\s*;:)",
            re.IGNORECASE,
        ),
    ),
    Rule(
        "persistence",
        "medium",
        "A persistence mechanism was modified (cron, rc.local, authorized_keys).",
        re.compile(
            r"(crontab\s+[-\w]|/etc/cron\.|/etc/rc\.local|systemctl\s+enable\s|"
            r"update-rc\.d|>>\s*~/\.bashrc|authorized_keys)",
            re.IGNORECASE,
        ),
    ),
    Rule(
        "exfiltration",
        "high",
        "A command addressed a known data-drop / tunneling endpoint.",
        re.compile(
            r"(?:curl|wget|scp|rsync|nc|ncat)\b[^\n]*"
            r"(?:pastebin\.com|transfer\.sh|0x0\.st|webhook\.site|termbin\.com|"
            r"ngrok|/dev/tcp/)",
            re.IGNORECASE,
        ),
    ),
    Rule(
        "crypto_miner",
        "high",
        "A cryptocurrency-mining indicator was found.",
        re.compile(r"\b(?:xmrig|minerd|minergate|stratum\+tcp|cryptonight)\b", re.IGNORECASE),
    ),
    Rule(
        "anti_forensics",
        "high",
        "The audit log itself was targeted for deletion or truncation.",
        re.compile(
            r"(?:rm|shred|truncate|>\s*)\b[^\n]*(?:apex\.jsonl|/logs/|OBSERVABILITY)",
            re.IGNORECASE,
        ),
    ),
    Rule(
        "firewall_evasion",
        "high",
        "Host firewall / MAC enforcement was disabled.",
        re.compile(
            r"(iptables\s+-F\b|ufw\s+disable|setenforce\s+0|"
            r"iptables\s+-P\s+\w+\s+ACCEPT|systemctl\s+(?:stop|disable)\s+firewalld)",
            re.IGNORECASE,
        ),
    ),
)

# Behavioural thresholds. Kept here so a deployment can see the policy in one
# place; the scanner and the on-demand tool share them.
AUTH_FAILURE_THRESHOLD = 5
CSRF_REJECTION_THRESHOLD = 5
SERVER_ERROR_THRESHOLD = 10


@dataclass
class Finding:
    id: str
    severity: str
    description: str
    count: int = 0
    evidence: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "severity": self.severity,
            "description": self.description,
            "count": self.count,
            "evidence": self.evidence,
        }


def _event_text(record: dict[str, Any]) -> str:
    """Flatten an event's payload to newline-separated strings.

    Matching against ``json.dumps(args)`` would put a quote before a command
    (``"command": "nmap ..."``), so a rule anchored with ``(?:^|\\s)`` would miss
    it. Flattening every string leaf onto its own line keeps those anchors - and
    word boundaries - working.
    """
    parts: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            if value:
                parts.append(value)

    for key in ("args", "request_body", "response_body", "result", "command", "message", "error", "path", "query"):
        walk(record.get(key))
    return "\n".join(parts)


def _evidence(record: dict[str, Any]) -> dict:
    return {
        "ts": record.get("ts"),
        "category": record.get("category"),
        "event": record.get("event"),
        "tool": record.get("tool"),
        "path": record.get("path"),
        "user_id": record.get("user_id"),
        "level": record.get("level"),
    }


def _matches_tool_ignore(record: dict, rule: Rule) -> bool:
    tool = str(record.get("tool") or "")
    return any(tool.startswith(prefix) for prefix in rule.ignore_tools)


def _record_matches_category(record: dict, rule: Rule) -> bool:
    if not rule.categories:
        return True
    return str(record.get("category") or "") in rule.categories


def analyze(events: Iterable[dict], *, max_evidence: int = 5) -> list[dict]:
    """Run every rule over ``events`` and return findings, most severe first.

    ``events`` may be oldest- or newest-first; the result is order-independent.
    """
    records = [e for e in events if isinstance(e, dict)]
    findings: dict[str, Finding] = {}

    def bump(rule_id: str, severity: str, description: str, record: dict) -> None:
        finding = findings.get(rule_id)
        if finding is None:
            finding = Finding(rule_id, severity, description)
            findings[rule_id] = finding
        finding.count += 1
        if len(finding.evidence) < max_evidence:
            finding.evidence.append(_evidence(record))

    for record in records:
        text = _event_text(record)
        if text:
            for rule in SIGNATURE_RULES:
                if _matches_tool_ignore(record, rule) or not _record_matches_category(record, rule):
                    continue
                if rule.pattern.search(text):
                    bump(rule.id, rule.severity, rule.description, record)

    # -- behavioural rules -------------------------------------------------
    auth_failures = csrf_rejections = server_errors = 0
    last_auth: dict | None = None
    last_csrf: dict | None = None
    last_err: dict | None = None
    for record in records:
        event = str(record.get("event") or "")
        status = record.get("status")
        body = json.dumps(record.get("request_body"), default=str) if record.get("request_body") else ""
        if event in ("login_failed", "auth_failed") or (
            isinstance(status, int) and status == 401
        ):
            auth_failures += 1
            last_auth = record
        if isinstance(status, int) and status == 403 and "csrf" in (str(record.get("response_body") or "") + body).lower():
            csrf_rejections += 1
            last_csrf = record
        if isinstance(status, int) and status >= 500:
            server_errors += 1
            last_err = record

    if auth_failures >= AUTH_FAILURE_THRESHOLD and last_auth is not None:
        bump(
            "auth_bruteforce",
            "high",
            f"{auth_failures} failed authentications in the window - possible credential attack.",
            last_auth,
        )
        findings["auth_bruteforce"].count = auth_failures
    if csrf_rejections >= CSRF_REJECTION_THRESHOLD and last_csrf is not None:
        bump(
            "csrf_abuse",
            "medium",
            f"{csrf_rejections} CSRF rejections in the window - possible cross-site forgery attempts.",
            last_csrf,
        )
        findings["csrf_abuse"].count = csrf_rejections
    if server_errors >= SERVER_ERROR_THRESHOLD and last_err is not None:
        bump(
            "server_error_spike",
            "medium",
            f"{server_errors} server errors in the window - possible fault or probing.",
            last_err,
        )
        findings["server_error_spike"].count = server_errors

    ordered = sorted(
        findings.values(),
        key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), -f.count, f.id),
    )
    return [f.as_dict() for f in ordered]


def summarize(findings: list[dict]) -> str:
    """A one-line-ish plain-text summary for a tool result or a log message."""
    if not findings:
        return "No suspicious or malicious activity detected."
    lines = [f"{len(findings)} finding(s):"]
    for f in findings:
        lines.append(f"- [{f['severity']}] {f['id']} x{f['count']}: {f['description']}")
    return "\n".join(lines)
