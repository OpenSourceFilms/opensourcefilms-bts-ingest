"""Object-key and slug rules. Keys are server-assigned; nothing the browser sends picks a path."""
import re
import unicodedata
from datetime import date

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_KEY_COMPONENT_RE = re.compile(r"[A-Za-z0-9._-]+\\Z")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\\Z")
MAX_NAME = 180
MAX_ASSET_ID = 128
_UNKNOWN_DATE = "0000-00-00"


def _shoot_date(value):
    """Preserve the public-intake unknown-date sentinel, reject unsafe dates."""
    if not isinstance(value, str) or _DATE_RE.fullmatch(value) is None:
        raise ValueError("shoot_date must be YYYY-MM-DD")
    if value != _UNKNOWN_DATE:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("shoot_date is not a calendar date") from exc
    return value


def _component(value, label, max_length):
    """Require a single ASCII object-key component without path semantics."""
    if (not isinstance(value, str) or not 1 <= len(value) <= max_length
            or value in (".", "..") or _KEY_COMPONENT_RE.fullmatch(value) is None):
        raise ValueError("{} must be a safe single object-key component".format(label))
    return value


def slug(text, fallback="x"):
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    s = _SLUG_RE.sub("-", text).strip("-.")
    return (s or fallback)[:80]


def safe_filename(name):
    """Keep a recognisable, collision-irrelevant filename (the asset id folder is the identity)."""
    base = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    stem, dot, ext = base.rpartition(".")
    if not dot:
        stem, ext = base, ""
    stem = slug(stem, "file")
    ext = slug(ext, "")[:16]
    out = f"{stem}.{ext}" if ext else stem
    return out[:MAX_NAME]


def original_key(production, shoot_date, shoot_slug, contributor_slug, asset_id, filename):
    """Build a six-component key; untrusted date/asset ID cannot inject paths."""
    shoot_date = _shoot_date(shoot_date)
    asset_id = _component(asset_id, "asset_id", MAX_ASSET_ID)
    year = shoot_date[:4]
    return (f"{slug(production)}/{year}/{shoot_date}_{slug(shoot_slug)}/"
            f"{slug(contributor_slug)}/{asset_id}/{safe_filename(filename)}")


def derived_prefix(original_b2_key):
    """Return the derived prefix only for structurally valid six-part keys."""
    if not isinstance(original_b2_key, str):
        raise ValueError("original_b2_key must be a string")
    parts = original_b2_key.split("/")
    if len(parts) != 6:
        raise ValueError("original_b2_key must contain exactly six path components")
    prod, year, shoot, contrib, asset_id, name = parts
    _component(prod, "production", 80)
    _component(year, "year", 4)
    _component(contrib, "contributor", 80)
    _component(asset_id, "asset_id", MAX_ASSET_ID)
    _component(name, "filename", MAX_NAME)
    _component(shoot, "shoot", 100)
    # The shoot path must reflect its declared year, including the explicit
    # unknown-date sentinel; don't silently invent or alter shooting dates.
    if len(shoot) < 12 or shoot[10] != "_":
        raise ValueError("shoot component must begin with a shoot date")
    shoot_date = _shoot_date(shoot[:10])
    if year != shoot_date[:4]:
        raise ValueError("year must match shoot date")
    return f"{prod}/{year}/{shoot}/{asset_id}"
