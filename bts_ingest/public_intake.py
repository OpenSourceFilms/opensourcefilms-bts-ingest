"""Public, private-capability BTS intake contract.

Routing partition values are explicitly NOT shooting metadata. They are used
solely for stable immutable B2 object keys while context is unresolved.
"""
import re
import unicodedata

PUBLIC_PRODUCTION = "Unclassified"
PUBLIC_SHOOT_DATE = "0000-00-00"  # sentinel, NOT capture or shoot date
PUBLIC_SHOOT_SLUG = "intake-pending"
PUBLIC_NOTE = "bts_public_intake/1"

def clean_name(value):
    """Optional uploader claim, never an assertion of original authorship."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("name must be a string")
    # Reject control/format chars, bound output and ensure no long tracking payload.
    v = unicodedata.normalize("NFC", value).strip()
    if len(v) > 120:
        raise ValueError("name too long")
    if any(unicodedata.category(c).startswith("C") for c in v):
        raise ValueError("name contains control characters")
    return v or None

def field(invitation, key):
    # Invitation rows may be sqlite3.Row or plain worker job dicts.
    return invitation[key] if key in invitation.keys() else None

def is_public(invitation):
    return bool(field(invitation, "is_public_intake"))

def actual_shoot_date(invitation):
    return None if is_public(invitation) else field(invitation, "shoot_date")

def uploader_claim(invitation):
    return field(invitation, "contributor_label") or None

def provenance(invitation):
    if is_public(invitation):
        return {
            "intake_kind": "public_unclassified",
            "shoot_date": None, "shoot_date_status": "unknown",
            "shoot_slug": None, "production": None,
            "uploader_name_claimed": uploader_claim(invitation),
            "creator_identity": None,
        }
    return {
        "intake_kind": "invitation",
        "shoot_date": field(invitation, "shoot_date"),
        "shoot_date_status": "invitation_assigned",
        "shoot_slug": field(invitation, "shoot_slug"),
        "production": field(invitation, "production"),
        "uploader_name_claimed": uploader_claim(invitation),
        "creator_identity": None,
    }
