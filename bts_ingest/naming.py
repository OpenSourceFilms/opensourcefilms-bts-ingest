"""Object-key and slug rules. Keys are server-assigned; nothing the browser sends picks a path."""
import re
import unicodedata

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
MAX_NAME = 180


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
    year = shoot_date[:4]
    return (f"{slug(production)}/{year}/{shoot_date}_{slug(shoot_slug)}/"
            f"{slug(contributor_slug)}/{asset_id}/{safe_filename(filename)}")


def derived_prefix(original_b2_key):
    """Derived outputs mirror the original's production/shoot/asset path, minus the contributor."""
    prod, year, shoot, _contrib, asset_id, _name = original_b2_key.split("/", 5)
    return f"{prod}/{year}/{shoot}/{asset_id}"
