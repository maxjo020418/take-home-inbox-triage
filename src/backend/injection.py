"""Deterministic backstop for prompt-injection detection.

The LLM reports `injection_detected`, but in evaluation it sometimes described an
injection in its `reason` without setting the flag. These patterns make the flag
reliable for the common shapes. They only ever *add* a warning for the reviewer:
they never change the label or block anything, so a false positive (e.g. a bug
report quoting "ignore previous instructions" as log text) costs a second look.
"""

from __future__ import annotations

import re

PATTERNS: list[tuple[str, str]] = [
    ("ignore-instructions", r"\bignore (all |any |the |your )?(previous|prior|above|earlier|real)\b.{0,40}\b(instructions|intent|prompt|rules)"),
    ("system-spoof", r"\[\s*system( message)?\b|\bsystem (message|prompt|override)\b|\brouter override\b"),
    ("role-spoof", r"<\s*(assistant|system|developer)\b[^>]*>|\brole\s*=\s*['\"]?(assistant|system|developer)"),
    ("forced-label", r"\b(label|classification)\s*[=:]\s*['\"]?(billing|bug_report|sales_lead|spam)\b|\bassign (billing|bug_report|sales_lead|spam)\b|\bmark this as (billing|bug_report|sales_lead|spam)\b"),
    ("tool-command", r"\b(call|execute|perform)\b.{0,30}\b(POST|GET)\s+/\w|\bapproved_calls\b|\"override\"\s*:\s*true"),
    ("skip-review", r"\b(skip|bypass) (all )?(checks|review|approval)\b|\bwithout (human )?review\b"),
    ("addressed-to-ai", r"\b(for|to) (the )?(classifier|assistant|ai|agent|automation)\b.{0,20}(:|instruction|note)|\bagent-only\b|\bhidden instruction\b"),
    ("fake-state", r"\bprevious assistant\b|\bclassification step is already complete\b|\bnext required tool\b"),
    ("reply-steering", r"\bwhen (writing|drafting) the (customer )?(response|reply)\b|\bask (for )?(their|the customer'?s?) (account )?(password|credentials|login)\b"),
    ("policy-forgery", r"\b(internal )?routing (policy|note|rule)\b.{0,60}\b(supersedes|approved|must|apply)\b|\bthis rule supersedes\b"),
]
_COMPILED = [(name, re.compile(rx, re.I | re.S)) for name, rx in PATTERNS]


def detect(*texts: str | None) -> list[str]:
    """Names of the injection patterns found in the given texts (empty = none)."""
    blob = "\n".join(t for t in texts if t)
    return [name for name, rx in _COMPILED if rx.search(blob)]
