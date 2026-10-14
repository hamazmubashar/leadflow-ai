import dataclasses

import pytest

from leadflow_ai.leads import Lead, LeadSource


def test_lead_source_values() -> None:
    assert [source.value for source in LeadSource] == [
        "web_form",
        "referral",
        "event",
        "import",
        "other",
    ]
    assert LeadSource("web_form") is LeadSource.WEB_FORM


def test_lead_defaults() -> None:
    lead = Lead(full_name="Ayesha Khan", email="ayesha@example.com")
    assert lead.phone is None
    assert lead.company is None
    assert lead.notes is None
    assert lead.source is LeadSource.OTHER


def test_lead_is_immutable() -> None:
    lead = Lead(full_name="Ayesha Khan", email="ayesha@example.com")
    with pytest.raises(dataclasses.FrozenInstanceError):
        lead.email = "other@example.com"  # type: ignore[misc]


def test_identity_keys_with_both_contacts() -> None:
    lead = Lead(full_name="A", email="ayesha@example.com", phone="+923001234567")
    assert lead.identity_keys() == frozenset({"email:ayesha@example.com", "phone:+923001234567"})


def test_identity_keys_with_one_contact() -> None:
    assert Lead(full_name="A", email="a@example.com").identity_keys() == {"email:a@example.com"}
    assert Lead(full_name="A", phone="+923001234567").identity_keys() == {"phone:+923001234567"}


def test_identity_keys_are_namespaced() -> None:
    # An email and a phone can never produce the same key.
    keys = Lead(full_name="A", email="x@example.com", phone="+923001234567").identity_keys()
    assert all(key.startswith(("email:", "phone:")) for key in keys)


def test_repr_hides_personal_data() -> None:
    lead = Lead(
        full_name="Ayesha Khan",
        email="ayesha@example.com",
        phone="+923001234567",
        company="Acme Traders",
        source=LeadSource.REFERRAL,
        notes="Call after 5pm",
    )
    text = repr(lead)
    assert text == "Lead(source='referral', has_email=True, has_phone=True)"
    for secret in ("Ayesha", "ayesha@", "3001234567", "Acme", "5pm"):
        assert secret not in text
        assert secret not in str(lead)
