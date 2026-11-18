"""Pure-logic tests for draft editing rules, password hashing and the TUI profile."""

import pytest

from src.backend.auth import hash_password, token_hash, verify_password
from src.triage_skill import apply_edit, plan_actions
from src.tui.profile import Profile

EMAIL = {"id": "e-1", "from": "dana@meridian.com", "subject": "Charged twice", "body": "..."}


@pytest.mark.parametrize("label,idx,field", [("billing", 0, "to"), ("billing", 0, "in_reply_to"),
                                             ("bug_report", 0, "channel"), ("sales_lead", 1, "email")])
def test_destination_fields_not_editable(label, idx, field):
    a = plan_actions(label, EMAIL)[idx]
    with pytest.raises(ValueError):
        apply_edit(a.kind, a.payload, {field: "evil@example.com"})


def test_wording_is_editable_and_original_untouched():
    a = plan_actions("billing", EMAIL)[0]
    new = apply_edit(a.kind, a.payload, {"body": "edited"})
    assert new["body"] == "edited" and new["to"] == EMAIL["from"]
    assert a.payload["body"] != "edited"


def test_edit_rejects_non_string_values():
    a = plan_actions("bug_report", EMAIL)[0]
    with pytest.raises(ValueError):
        apply_edit(a.kind, a.payload, {"message": {"nested": 1}})


def test_password_hash_roundtrip_and_salted():
    h1, h2 = hash_password("s3cret-pass"), hash_password("s3cret-pass")
    assert h1 != h2  # random salt
    assert verify_password("s3cret-pass", h1)
    assert not verify_password("wrong", h1)
    assert not verify_password("s3cret-pass", "garbage")


def test_token_hash_is_not_the_token():
    assert token_hash("abc") != "abc" and len(token_hash("abc")) == 64


def test_profile_roundtrip_stores_no_secrets(tmp_path):
    path = tmp_path / "config.toml"
    Profile(backend_url="http://x:8000", username="alice").save(path)
    text = path.read_text()
    assert "password" not in text and "token" not in text
    assert Profile.load(path).username == "alice"


def test_profile_missing_or_corrupt_falls_back_to_defaults(tmp_path):
    assert Profile.load(tmp_path / "nope.toml").backend_url.startswith("http://")
    bad = tmp_path / "bad.toml"
    bad.write_text("not = [valid")
    assert Profile.load(bad).username == ""
