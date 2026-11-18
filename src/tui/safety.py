"""Combined safety rating for a dashboard row: every check, worst one wins.

Levels (worst first):
  danger  ⛔  sender impersonation suspected (lookalike domain)
  warn    ⚠  prompt-injection attempt detected (even if ignored and a reply was drafted)
  check   ?   sender domain needs checking (doesn't exist, no SPF/DMARC, brand name, punycode)
              or classification failed
  safe    ✓   all checks passed
  pending …   checks haven't run yet
"""

from __future__ import annotations

from dataclasses import dataclass, field

LEVELS = {  # rank -> (word, icon, style)
    -1: ("pending", "…", "dim"),
    0: ("safe", "✓", "green"),
    1: ("check", "?", "bold yellow"),
    2: ("warn", "⚠", "bold dark_orange"),
    3: ("danger", "⛔", "bold red"),
}


@dataclass
class Check:
    name: str
    rank: int
    detail: str


@dataclass
class Safety:
    rank: int
    checks: list[Check] = field(default_factory=list)

    @property
    def word(self) -> str:
        return LEVELS[self.rank][0]

    @property
    def icon(self) -> str:
        return LEVELS[self.rank][1]

    @property
    def style(self) -> str:
        return LEVELS[self.rank][2]

    @property
    def worst(self) -> Check | None:
        flagged = [c for c in self.checks if c.rank > 0]
        return max(flagged, key=lambda c: c.rank) if flagged else None

    @property
    def needs_confirmation(self) -> bool:
        """Approving something rated warn or worse requires an explicit confirm."""
        return self.rank >= 2

    def cell(self) -> str:
        """Short table text: icon + the worst check's name (+N more flagged)."""
        worst = self.worst
        if worst is None:
            return f"{self.icon} {self.word}"
        others = sum(1 for c in self.checks if c.rank > 0) - 1
        return f"{self.icon} {worst.name}" + (f" +{others}" if others else "")


def assess(row: dict) -> Safety:
    checks: list[Check] = []

    verdict = row.get("domain_verdict")
    detail = row.get("domain_detail") or {}
    reasons = "; ".join(detail.get("reasons", [])) or verdict or ""
    if verdict == "impersonation_suspected":
        checks.append(Check("impersonation", 3, reasons))
    elif verdict == "check_needed":
        checks.append(Check("domain", 1, reasons))
    elif verdict == "ok":
        checks.append(Check("domain", 0, reasons or "ok"))

    inj = row.get("injection_detail") or {}
    if row.get("injection_detected"):
        sources = (["model"] if inj.get("llm") else []) + list(inj.get("patterns", []))
        checks.append(Check("injection", 2, "instructions aimed at the AI were found and ignored"
                                            + (f" (detected by: {', '.join(sources)})" if sources else "")))
    elif row.get("injection_detected") is False:
        checks.append(Check("injection", 0, "none detected"))

    if row.get("row_type") == "failed" or row.get("status") == "failed":
        checks.append(Check("classification", 1, row.get("last_error") or "classification failed"))

    if not checks:
        return Safety(-1, [])
    return Safety(max(c.rank for c in checks), checks)
