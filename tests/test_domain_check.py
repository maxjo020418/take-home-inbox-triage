"""Sender-domain checker: lookalike rules offline, DNS posture with a fake resolver."""

import pytest

from src.backend.domain_check import check_domain, check_sender


class FakeDNS:
    """`only` (optional): the set of names that exist; anything else is NXDOMAIN."""

    def __init__(self, mail=True, txt=None, dmarc=None, nx=False, only=None):
        self.mail, self.txt_records, self.dmarc, self.nx, self.only = mail, txt or [], dmarc, nx, only
        self.queried: list[str] = []

    def _nx(self, name):
        self.queried.append(name)
        return self.nx or (self.only is not None and name not in self.only)

    def has_mail_host(self, name):
        return None if self._nx(name) else self.mail

    def txt(self, name):
        if self._nx(name):
            return None
        return [self.dmarc] if name.startswith("_dmarc.") and self.dmarc else ([] if name.startswith("_dmarc.") else self.txt_records)


GOOD = FakeDNS(txt=["v=spf1 include:_spf.example -all"], dmarc="v=DMARC1; p=reject")


@pytest.mark.parametrize("sender,lookalike_of,technique", [
    ("a@micr0soft.com", "microsoft.com", "homoglyph"),
    ("a@rnicrosoft.com", "microsoft.com", "homoglyph"),
    ("a@microsfot.com", "microsoft.com", "transposition"),
    ("a@paypa1.com", "paypal.com", "homoglyph"),
    ("a@mail.micr0soft.com", "microsoft.com", "homoglyph"),  # subdomain of a lookalike
    ("a@microsoft.co", "microsoft.com", "tld-swap"),
    ("a@microsoft.com.account-verify.net", "microsoft.com", "embedded"),
    ("a@paypal.com-resolution.center", "paypal.com", "embedded"),
    ("a@xn--80ak6aa92e.com", "apple.com", "cyrillic"),  # the classic all-Cyrillic "аррӏе.com"
])
def test_impersonation_detected(sender, lookalike_of, technique):
    c = check_sender(sender, resolver=GOOD)
    assert c.verdict == "impersonation_suspected"
    assert (c.lookalike_of, c.technique) == (lookalike_of, technique)


@pytest.mark.parametrize("sender", ["a@microsoft.com", "a@mail.microsoft.com", "a@github.com", "a@shopify.com"])
def test_genuine_domains_ok(sender):
    assert check_sender(sender, resolver=GOOD).verdict == "ok"


def test_good_posture_spoofing_lookalike_still_flagged():
    # A lookalike with perfect SPF/DMARC is still a lookalike.
    assert check_sender("a@micr0soft.com", resolver=GOOD).verdict == "impersonation_suspected"


@pytest.mark.parametrize("sender,dns,needle", [
    ("a@microsoft-support-desk.com", GOOD, "brand name"),
    ("a@xn--mnchen-3ya.de", GOOD, "punycode"),  # münchen.de: IDN, not a lookalike
    ("a@unknown-co.io", FakeDNS(nx=True), "does not exist"),
    ("a@nomail.io", FakeDNS(mail=False, txt=["v=spf1 -all"]), "cannot receive mail"),
    ("a@open.io", FakeDNS(txt=[], dmarc=None), "no SPF and no DMARC"),
])
def test_check_needed_signals(sender, dns, needle):
    c = check_sender(sender, resolver=dns)
    assert c.verdict == "check_needed"
    assert any(needle in r for r in c.reasons), c.reasons


def test_dmarc_policy_parsed():
    c = check_sender("a@shopify.com", resolver=GOOD)
    assert c.spf and c.dmarc_policy == "reject" and c.has_mx is True


def test_reserved_and_disabled_dns_skip_lookups():
    class Boom:
        def has_mail_host(self, n): raise AssertionError("DNS should not be queried")
        txt = has_mail_host
    assert check_sender("a@example.org", resolver=Boom()).verdict == "ok"
    assert check_domain("acme.io", resolver=Boom(), dns_enabled=False).verdict == "ok"


def test_dns_outage_is_unknown_not_suspicious():
    import dns.exception

    class Down:
        def has_mail_host(self, n): raise dns.exception.Timeout()
        txt = has_mail_host
    c = check_sender("a@acme.io", resolver=Down())
    assert c.verdict == "ok" and c.has_mx is None and any("DNS unavailable" in r for r in c.reasons)


def test_unicode_lookalike_normalized_to_a_label():
    unicode_form = b"xn--80ak6aa92e.com".decode("idna")  # "аррӏе.com", all Cyrillic
    assert unicode_form != "xn--80ak6aa92e.com"
    c = check_sender(f"a@{unicode_form}", resolver=GOOD)
    assert c.verdict == "impersonation_suspected" and c.lookalike_of == "apple.com"
    assert c.domain == "xn--80ak6aa92e.com"
    assert any("punycode" in r for r in c.reasons)


def test_unicode_idn_gets_punycode_signal():
    c = check_sender("a@münchen.de", resolver=GOOD)
    assert c.verdict == "check_needed" and any("punycode" in r for r in c.reasons)


def test_nonexistent_subdomain_of_real_org_is_checked_itself():
    dns = FakeDNS(txt=["v=spf1 -all"], dmarc="v=DMARC1; p=reject", only={"microsoft.com", "_dmarc.microsoft.com"})
    c = check_sender("a@missing.microsoft.com", resolver=dns)
    assert c.verdict == "check_needed" and any("missing.microsoft.com does not exist" in r for r in c.reasons)
    assert dns.queried[0] == "missing.microsoft.com"


def test_spf_on_sender_domain_dmarc_falls_back_to_org_domain():
    dns = FakeDNS(txt=["v=spf1 -all"], dmarc="v=DMARC1; p=quarantine",
                  only={"mail.acme.io", "_dmarc.acme.io"})
    c = check_sender("a@mail.acme.io", resolver=dns)
    assert c.verdict == "ok" and c.spf == "v=spf1 -all" and c.dmarc_policy == "quarantine"
    assert dns.queried.index("_dmarc.mail.acme.io") < dns.queried.index("_dmarc.acme.io")
    assert "acme.io" not in dns.queried  # SPF/mail host looked up on the full sender domain
