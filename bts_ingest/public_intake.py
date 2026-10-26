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
    """Read an explicit public-intake flag without truthiness coercion.

    The native worker constructs a bool; SQLite can represent flags as 0/1.
    A string such as "False" is truthy in Python and would incorrectly
    discard a legacy invitation's assigned production and shoot metadata.
    Absence of this field denotes an original legacy invitation; a present
    but malformed value must be rejected instead of guessed.
    """
    if "is_public_intake" not in invitation.keys():
        return False
    flag = invitation["is_public_intake"]
    if type(flag) is bool:
        return flag
    if type(flag) is int and flag in (0, 1):
        return bool(flag)
    raise ValueError("is_public_intake must be a boolean or SQLite 0/1")

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
