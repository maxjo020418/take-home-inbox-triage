"""Generate a deterministic sender-impersonation dataset -> dataset/impersonation.jsonl

Three kinds of rows:
  * lookalike   - phishing from a dnstwist permutation of a protected brand
                  (homoglyph, transposition, omission, ...). label=spam,
                  expected_domain_verdict=impersonation_suspected
  * crafted     - techniques dnstwist doesn't generate: TLD swap, the brand domain
                  embedded in another domain, brand name + extra words
  * control     - ordinary customer mail from genuine domains, so the checker's
                  false-positive rate is measured too. expected_domain_verdict=ok

Some lookalike rows deliberately read like normal business mail (a vendor bank
change, a partnership inquiry): the content alone looks fine, only the domain
gives it away. The domains are synthetic test data; nothing is sent anywhere.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import dnstwist

OUT = Path(__file__).resolve().parent.parent / "dataset" / "impersonation.jsonl"

# brand -> (local part, subject, body) phishing theme in that brand's voice
THEMES: dict[str, tuple[str, str, str]] = {
    "microsoft.com": ("account-security", "Your Microsoft 365 password expires today",
                      "Your password for this mailbox expires in 2 hours. Keep your current password by verifying at the link below, or you will lose access to Outlook and Teams."),
    "paypal.com": ("service", "Payment on hold: action required",
                   "We placed a temporary hold on a $2,480.00 payment to your business account. Confirm your identity and bank details within 24 hours to release the funds."),
    "google.com": ("drive-shares-noreply", "Q4 invoice.pdf has been shared with you",
                   "A document has been shared with you: 'Q4 invoice.pdf'. Sign in with your work account to view it. This link expires after one use."),
    "amazon.com": ("aws-billing", "AWS account suspended for unpaid balance",
                   "Your AWS account has been suspended due to an outstanding balance of $1,312.44. Update your payment method now to avoid permanent deletion of your resources."),
    "github.com": ("security", "[GitHub] A new SSH key was added to your account",
                   "A new public key was added to your account. If you did not do this, revoke it immediately by signing in at the link below and re-entering your 2FA code."),
    "stripe.com": ("payouts", "Payout failed: update your bank account",
                   "Your latest payout of $8,920.10 could not be completed. Please re-enter your bank account and routing number so we can retry within 1 business day."),
    "okta.com": ("no-reply", "MFA re-enrollment required for your organization",
                 "Your administrator requires all users to re-enroll multi-factor authentication today. Scan the new code at the link below using your current credentials."),
    "docusign.com": ("dse", "Please DocuSign: Contract Amendment #2",
                     "You have received a document to review and sign: 'Contract Amendment #2'. Review the document using your email password to confirm your identity."),
}

# Lookalike mail that reads like ordinary business correspondence.
BUSINESS_LURES: list[tuple[str, str, str, str]] = [
    ("stripe.com", "finance", "Updated remittance details for open invoices",
     "Hello, please note our banking details have changed effective immediately. Kindly route payment for all open invoices to the new account in the attached form. Thank you."),
    ("microsoft.com", "partners", "Partnership inquiry: co-selling opportunity",
     "We'd like to explore a co-sell partnership and onboard roughly 400 seats this quarter. Please send your pricing sheet and a contact in finance so we can set up the vendor record."),
    ("paypal.com", "merchant-support", "Duplicate charge refund for your customer",
     "One of your customers filed a dispute for a duplicate charge. To process the refund on your behalf we need you to confirm the account owner's login."),
    ("github.com", "enterprise", "Bug in your GitHub integration",
     "Our enterprise team noticed your GitHub app fails webhook verification. Please re-authorize the app using the admin token form linked here so we can debug."),
]

# dnstwist techniques to sample, in order; one lookalike per (brand, technique).
TECHNIQUES = ("homoglyph", "transposition", "omission", "addition", "replacement",
              "bitsquatting", "hyphenation", "vowel-swap", "insertion", "repetition")

CRAFTED: list[tuple[str, str, str, str]] = [
    # (sender, technique, impersonated_domain, expected_domain_verdict)
    ("security@microsoft.co", "tld-swap", "microsoft.com", "impersonation_suspected"),
    ("billing@paypal.co", "tld-swap", "paypal.com", "impersonation_suspected"),
    ("verify@microsoft.com.account-verify.net", "embedded", "microsoft.com", "impersonation_suspected"),
    ("resolution@paypal.com-resolution.center", "embedded", "paypal.com", "impersonation_suspected"),
    ("help@microsoft-support-desk.com", "brand-contained", "microsoft.com", "check_needed"),
    ("alerts@github-security-team.net", "brand-contained", "github.com", "check_needed"),
]

# Genuine senders with ordinary support mail: should all be `ok`.
CONTROLS: list[tuple[str, str, str, str]] = [
    ("ap@shopify.com", "billing", "Invoice 2291 shows the wrong tax rate",
     "Hi, invoice 2291 applies 10% tax but our account is tax-exempt. Could you reissue it? Thanks."),
    ("eng-tools@cloudflare.com", "bug_report", "Webhook retries never stop",
     "Your webhook retry loop keeps resending event evt_8812 even after we return 200. Started this morning."),
    ("procurement@microsoft.com", "sales_lead", "Pricing for a 300-seat rollout",
     "We're evaluating tools for an internal team of about 300. Could someone share enterprise pricing and set up a demo?"),
    ("finance@atlassian.com", "billing", "Need a receipt for last month's payment",
     "Could you send the official receipt for the payment made on the 3rd? Our auditors need it."),
    ("ops@figma.com", "bug_report", "CSV export truncates long rows",
     "Exports with rows longer than 4 KB are cut off mid-field. Repro: any report with a long notes column."),
    ("it@hubspot.com", "sales_lead", "Interested in an annual plan",
     "We'd like to move 45 users to an annual plan. Who can walk us through options?"),
    ("someone@gmail.com", "billing", "Charged after cancelling",
     "I cancelled my plan last week but was charged again today. Please refund."),
    ("team@notion.so", "sales_lead", "Partner pricing question",
     "We're a partner agency with five clients interested in your product. Do you offer partner pricing?"),
]


def _pick(original: str, technique: str) -> str | None:
    """Deterministically choose one plain-ASCII permutation (plus one punycode homoglyph)."""
    f = dnstwist.Fuzzer(original)
    f.generate()
    candidates = sorted(dict(p)["domain"] for p in f.domains if dict(p)["fuzzer"] == technique)
    ascii_only = [d for d in candidates if "xn--" not in d]
    pool = ascii_only or candidates
    if not pool:
        return None
    digest = int(hashlib.sha256(f"{original}:{technique}".encode()).hexdigest(), 16)
    return pool[digest % len(pool)]


def _idn(original: str) -> str | None:
    f = dnstwist.Fuzzer(original)
    f.generate()
    idn = sorted(dict(p)["domain"] for p in f.domains if dict(p)["domain"].startswith("xn--"))
    return idn[0] if idn else None


def main() -> None:
    rows: list[dict] = []

    def add(kind: str, sender: str, subject: str, body: str, label: str, verdict: str,
            impersonated: str | None, technique: str | None) -> None:
        rows.append({
            "id": f"imp-{len(rows) + 1:03d}-{kind}",
            "from": sender, "subject": subject, "body": body,
            "label": label, "expected_domain_verdict": verdict,
            "impersonated_domain": impersonated, "technique": technique,
            "kind": kind, "source": "synthetic_dnstwist_fixture",
        })

    for i, (brand, (local, subject, body)) in enumerate(THEMES.items()):
        # 3 techniques per brand, rotated so every technique appears across brands
        for technique in (TECHNIQUES[(i + k) % len(TECHNIQUES)] for k in (0, 3, 6)):
            if domain := _pick(brand, technique):
                add("lookalike", f"{local}@{domain}", subject, body, "spam", "impersonation_suspected", brand, technique)
        if i < 2 and (idn := _idn(brand)):  # a couple of punycode homoglyphs
            add("lookalike", f"{local}@{idn}", subject, body, "spam", "impersonation_suspected", brand, "homoglyph-idn")

    for brand, local, subject, body in BUSINESS_LURES:
        domain = _pick(brand, "homoglyph")
        add("lookalike-business", f"{local}@{domain}", subject, body, "spam", "impersonation_suspected", brand, "homoglyph")

    for sender, technique, brand, verdict in CRAFTED:
        _, subject, body = THEMES[brand]
        add("crafted", sender, subject, body, "spam", verdict, brand, technique)

    for sender, label, subject, body in CONTROLS:
        add("control", sender, subject, f"Hello support,\n\n{body}\n\nBest regards", label, "ok", None, None)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    print(f"Wrote {len(rows)} examples to {OUT}")


if __name__ == "__main__":
    main()
