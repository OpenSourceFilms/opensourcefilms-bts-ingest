"""Read-only resolution of accepted v2 derivative generations.

Only an authenticated, authorised BTS reader/API may call this helper.
It does not issue download links, copy objects, choose a latest B2 key, or
promote local directories. Returns pinned object references from the one
fenced generation committed by the service's registry.
"""
from __future__ import annotations

from contextlib import closing
import json
import re
import sqlite3
from urllib.parse import quote

from . import derivative_generation, naming

_GENERATION = re.compile(r"[a-f0-9]{32}\Z")


def accepted_generation(reg, job_id):
    """Return a verified-shaped pointer to the winning v2 generation or None.

    Never use worker-supplied result_json by itself as authority: old v1
    callers can supply arbitrary result metadata. The independent
    accepted_generation_prefix column is set only by a fenced terminal
    transition while holding SQLite's writer lock.
    """
    if type(job_id) is not int or job_id <= 0:
        raise ValueError("job_id must be a positive integer")
    # SQLite mode=ro must not use Registry.tx(), which takes BEGIN IMMEDIATE
    # and blocks the intake writer even though this lookup does not mutate.
    # Quote the path so a filename with '?' or '#' cannot alter URI options.
    uri = "file:" + quote(reg.path, safe="/") + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as c:
        c.row_factory = sqlite3.Row
        row = c.execute(
            "SELECT j.status,j.accepted_generation_prefix,j.result_json,"
            " a.asset_id,a.b2_key FROM jobs j"
            " JOIN assets a ON a.asset_id=j.asset_id WHERE j.id=?",
            (job_id,)
        ).fetchone()
    if row is None or row["status"] != "done":
        return None
    prefix = row["accepted_generation_prefix"]
    if prefix is None:
        return None  # legacy v1 completion; no accepted v2 pointer
    root = naming.derived_prefix(row["b2_key"]) + "/_generations/"
    if not prefix.startswith(root) or _GENERATION.fullmatch(prefix[len(root):]) is None:
        raise ValueError("registry accepted generation is malformed")
    try:
        result = json.loads(row["result_json"])
    except (TypeError, ValueError):
        raise ValueError("registry accepted manifest is malformed") from None
    if not isinstance(result, dict) or result.get("generation_prefix") != prefix:
        raise ValueError("registry accepted generation and result do not agree")
    if result.get("publication_protocol") != derivative_generation.PROTOCOL:
        raise ValueError("unexpected accepted derivative protocol")
    if "published_path" in result or "publish_state" in result:
        raise ValueError("accepted generation contains pre-approved local publication")
    entries = result.get("derivatives")
    if type(entries) is not list or len(entries) > derivative_generation.MAX_DERIVATIVES:
        raise ValueError("invalid accepted derivative list")
    seen = set()
    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("invalid accepted derivative entry")
        key = entry.get("b2_key")
        if type(key) is not str or not key.startswith(prefix+"/"):
            raise ValueError("accepted derivative lies outside winning generation")
        filename = key[len(prefix)+1:]
        if (not re.fullmatch(r"[A-Za-z0-9._-]{1,180}", filename)
                or filename in (".", "..") or key in seen):
            raise ValueError("invalid accepted derivative key")
        seen.add(key)
        version = entry.get("version_id")
        if (type(version) is not str or not 1 <= len(version) <= 512
                or not version.isascii() or any(not 33 <= ord(c) <= 126 for c in version)
                or version.lower() in ("null", "none")):
            raise ValueError("missing pinned derivative version")
        sha = entry.get("sha256")
        if type(sha) is not str or re.fullmatch(r"[a-fA-F0-9]{64}", sha) is None:
            raise ValueError("invalid accepted derivative digest")
        size = entry.get("bytes")
        if type(size) is not int or not 0 <= size <= (1 << 63)-1:
            raise ValueError("invalid accepted derivative length")
        out.append({"b2_key":key, "version_id":version,
                    "sha256":sha.lower(), "bytes":size})
    return {"asset_id":row["asset_id"], "job_id":job_id,
            "generation_prefix":prefix, "derivatives":out}
