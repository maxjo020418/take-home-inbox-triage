"""Sender-domain reputation check: lookalikes (dnstwist) + DNS mail posture.

Verdicts:
  ok                       nothing suspicious found
  check_needed             weak signals: brand name inside an unrelated domain, punycode,
                           domain that can't receive mail / doesn't exist, no SPF and no DMARC
  impersonation_suspected  the domain is a lookalike of a protected domain (dnstwist
                           permutation, TLD swap, or the protected domain embedded in another)

What this can and can't say about SPF/DKIM/DMARC: verifying them needs the raw
message (DKIM-Signature header, connecting IP for SPF). The mock inbox has
neither, so we only check what the sender domain *publishes* in DNS (SPF record
present? DMARC policy?). In production you'd read the receiving server's
`Authentication-Results` header (Gmail computes spf/dkim/dmarc pass/fail) instead.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import asdict, dataclass, field
from functools import cache
from typing import Literal, Protocol

import dns.exception
import dns.resolver
import dnstwist

Verdict = Literal["ok", "check_needed", "impersonation_suspected"]

# Brands commonly impersonated in B2B phishing, plus our own domain if configured.
DEFAULT_PROTECTED = (
    "microsoft.com", "office.com", "outlook.com", "google.com", "gmail.com", "apple.com", "amazon.com",
    "paypal.com", "stripe.com", "github.com", "okta.com", "slack.com", "dropbox.com", "docusign.com",
    "linkedin.com", "salesforce.com", "zoom.us", "atlassian.com",
)
# RFC 2606 / 6761 reserved names: used by test fixtures, never real senders, so skip DNS.
RESERVED_DOMAINS = ("example.com", "example.org", "example.net")
RESERVED_TLDS = (".example", ".test", ".invalid", ".localhost")


def _reserved(domain: str) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in RESERVED_DOMAINS) or domain.endswith(RESERVED_TLDS)


@dataclass
class DomainCheck:
    domain: str
    verdict: Verdict = "ok"
    reasons: list[str] = field(default_factory=list)
    lookalike_of: str | None = None
    technique: str | None = None
    has_mx: bool | None = None  # None = not checked / DNS unavailable
    spf: str | None = None  # the v=spf1 record, if published
    dmarc_policy: str | None = None  # none | quarantine | reject

    def escalate(self, verdict: Verdict, reason: str) -> None:
        rank = {"ok": 0, "check_needed": 1, "impersonation_suspected": 2}
        if rank[verdict] > rank[self.verdict]:
            self.verdict = verdict
        self.reasons.append(reason)

    def summary(self) -> str:
        """One line for the LLM prompt and logs."""
        return f"{self.verdict}: " + ("; ".join(self.reasons) or "no issues found")

    def to_dict(self) -> dict:
        return asdict(self)


class Resolver(Protocol):
    def txt(self, name: str) -> list[str] | None: ...  # None = NXDOMAIN
    def has_mail_host(self, name: str) -> bool | None: ...  # None = NXDOMAIN


class DNSResolver:
    """dnspython with short timeouts and a 1h in-memory cache."""

    def __init__(self, lifetime: float = 3.0, ttl_s: float = 3600):
        self._r = dns.resolver.Resolver()
        self._r.lifetime = lifetime
        self._ttl = ttl_s
        self._cache: dict[tuple[str, str], tuple[float, object]] = {}
        self._lock = threading.Lock()

    def _resolve(self, name: str, rtype: str):
        key = (name, rtype)
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < self._ttl:
                return hit[1]
        try:
            value: object = [r.to_text() for r in self._r.resolve(name, rtype)]
        except dns.resolver.NXDOMAIN:
            value = None
        except dns.resolver.NoAnswer:
            value = []
        # Timeouts / no nameservers propagate: "unknown" must not look like "missing".
        with self._lock:
            self._cache[key] = (time.monotonic(), value)
        return value

    def txt(self, name: str) -> list[str] | None:
        records = self._resolve(name, "TXT")
        return None if records is None else [r.strip('"').replace('" "', "") for r in records]

    def has_mail_host(self, name: str) -> bool | None:
        mx = self._resolve(name, "MX")
        if mx is None:
            return None
        if mx:  # "0 ." is a null MX (RFC 7505): explicitly accepts no mail
            return not all(r.strip().endswith(" .") for r in mx)
        a = self._resolve(name, "A")  # RFC 5321 implicit MX
        return None if a is None else bool(a)


def protected_domains() -> tuple[str, ...]:
    extra = [d.strip().lower() for d in os.environ.get("PROTECTED_DOMAINS", "").split(",") if d.strip()]
    return tuple(dict.fromkeys([*DEFAULT_PROTECTED, *extra]))


@cache
def _lookalikes(protected: tuple[str, ...]) -> dict[str, tuple[str, str]]:
    """permutation domain -> (protected domain, dnstwist fuzzer). Built once (~0.5s)."""
    table: dict[str, tuple[str, str]] = {}
    for original in protected:
        fuzzer = dnstwist.Fuzzer(original)
        fuzzer.generate()
        for perm in fuzzer.domains:
            p = dict(perm)
            if p["fuzzer"] != "*original":
                table.setdefault(p["domain"], (original, p["fuzzer"]))
    return table


def _split(domain: str) -> tuple[str, str, str]:
    sub, name, tld = dnstwist.domain_tld(domain)
    return sub, name, tld


def check_domain(domain: str, *, resolver: Resolver | None = None, dns_enabled: bool | None = None) -> DomainCheck:
    domain = domain.strip().lower().rstrip(".")
    try:  # dnstwist tables (and DNS) use A-labels: "аррӏе.com" -> "xn--80ak6aa92e.com"
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        pass  # not IDNA-encodable; keep the original string
    res = DomainCheck(domain=domain)
    protected = protected_domains()
    _, name, tld = _split(domain)
    registered = f"{name}.{tld}"

    # -- lookalikes (offline) -----------------------------------------------------------
    if registered in protected or any(domain == p or domain.endswith("." + p) for p in protected):
        res.reasons.append(f"genuine {registered}")
    else:
        if hit := _lookalikes(protected).get(registered):
            res.lookalike_of, res.technique = hit
            res.escalate("impersonation_suspected", f"lookalike of {hit[0]} ({hit[1]})")
        for p in protected:
            p_name = _split(p)[1]
            if res.lookalike_of:
                break
            if name == p_name:
                res.lookalike_of, res.technique = p, "tld-swap"
                res.escalate("impersonation_suspected", f"same name as {p} with a different TLD (.{tld})")
            elif (f"{p}." in f"{domain}." and not domain.endswith("." + p)) or f"{p}-" in domain:
                res.lookalike_of, res.technique = p, "embedded"
                res.escalate("impersonation_suspected", f"embeds {p} inside an unrelated domain")
            elif len(p_name) >= 4 and p_name in name:
                res.escalate("check_needed", f"contains brand name '{p_name}' but isn't {p}")
        if "xn--" in domain:
            res.escalate("check_needed", "internationalized (punycode) domain")

    # -- DNS mail posture ---------------------------------------------------------------------
    if dns_enabled is None:
        dns_enabled = os.environ.get("DOMAIN_DNS_CHECK", "1") != "0"
    if _reserved(domain):
        res.reasons.append("reserved test domain (DNS not checked)")
        return res
    if not dns_enabled:
        res.reasons.append("DNS checks disabled")
        return res
    resolver = resolver or _default_resolver()
    # Posture of the actual sender domain (a nonexistent subdomain of a real org must not
    # look fine); DMARC falls back to the organizational domain per RFC 7489 §6.6.3.
    try:
        mail = resolver.has_mail_host(domain)
        if mail is None:
            res.escalate("check_needed", f"{domain} does not exist in DNS")
            return res
        res.has_mx = mail
        if not mail:
            res.escalate("check_needed", "domain cannot receive mail (no MX/A, or null MX)")
        spf = next((t for t in (resolver.txt(domain) or []) if t.lower().startswith("v=spf1")), None)
        res.spf = spf
        dmarc_txt = None
        for name in dict.fromkeys((domain, registered)):
            dmarc_txt = next((t for t in (resolver.txt(f"_dmarc.{name}") or []) if t.upper().startswith("V=DMARC1")), None)
            if dmarc_txt:
                break
        if dmarc_txt:
            tags = dict(kv.strip().split("=", 1) for kv in dmarc_txt.split(";") if "=" in kv)
            res.dmarc_policy = tags.get("p", "none").strip().lower()
        if not spf and not dmarc_txt:
            res.escalate("check_needed", "no SPF and no DMARC published (anyone can spoof this domain)")
        else:
            res.reasons.append(f"SPF {'published' if spf else 'missing'}, DMARC p={res.dmarc_policy or 'missing'}")
    except dns.exception.DNSException as exc:
        res.reasons.append(f"DNS unavailable ({type(exc).__name__}); posture unknown")
    return res


@cache
def _default_resolver() -> DNSResolver:
    return DNSResolver()


def check_sender(address: str, **kw) -> DomainCheck:
    domain = address.rsplit("@", 1)[-1] if "@" in address else address
    return check_domain(domain, **kw)


def warm_up() -> None:
    _lookalikes(protected_domains())
