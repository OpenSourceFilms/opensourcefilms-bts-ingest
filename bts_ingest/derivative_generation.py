"""Opt-in immutable derivative-generation references for fenced BTS leases.

Only a v2 worker's private lease token can derive its public, non-secret
generation namespace. No storage is accessed here. Readers must resolve
accepted objects through the registry and pin a B2 version ID; fetching the
latest object at a canonical key is not safe against late-worker writes.
"""
from __future__ import annotations

import hashlib
import re

from . import naming

_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
_COMPONENT = re.compile(r"[A-Za-z0-9._-]{1,180}\Z")
_SHA256 = re.compile(r"[A-Fa-f0-9]{64}\Z")
MAX_DERIVATIVES = 512
PROTOCOL = "generation-scoped-v1"


def _opaque_generation_id(lease_token):
    if type(lease_token) is not str or _TOKEN.fullmatch(lease_token) is None:
        raise ValueError("invalid fenced lease token")
    # Domain separation: do not publish the lease credential, or its actual
    # database-stored SHA-256, in B2 keys or local paths.
    material = b"osf-bts-derivative-generation-v1\x00" + lease_token.encode("ascii")
    return hashlib.sha256(material).hexdigest()[:32]


def generation_prefix(original_b2_key, lease_token):
    return naming.derived_prefix(original_b2_key) + "/_generations/" + _opaque_generation_id(lease_token)


def generation_key(original_b2_key, lease_token, filename):
    if (type(filename) is not str or _COMPONENT.fullmatch(filename) is None
            or filename in (".", "..")):
        raise ValueError("derivative filename must be one safe component")
    return generation_prefix(original_b2_key, lease_token) + "/" + filename


def validate_manifest(original_b2_key, lease_token, result):
    """Enforce v2 successful derivative references, before terminal SQL update.

    This validates the *claims* only, not B2 contents or version existence.
    The API must separately HEAD/GET-verify the pinned versions and local
    generation before making results available to consumers.
    """
    expected_prefix = generation_prefix(original_b2_key, lease_token) + "/"
    if not isinstance(result, dict) or result.get("publication_protocol") != PROTOCOL:
        raise ValueError("fenced derivative result requires generation-scoped protocol")
    if "published_path" in result or "publish_state" in result:
        raise ValueError("fenced result must not advertise pre-accepted canonical local publication")
    entries = result.get("derivatives")
    if type(entries) is not list or len(entries) > MAX_DERIVATIVES:
        raise ValueError("derivatives must be a bounded list")
    used = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("derivative entry must be an object")
        key = entry.get("b2_key")
        if type(key) is not str or not key.startswith(expected_prefix):
            raise ValueError("derivative object is outside this lease generation")
        filename = key[len(expected_prefix):]
        if (_COMPONENT.fullmatch(filename) is None or filename in (".", "..")
                or key in used):
            raise ValueError("derivative key is unsafe or duplicated")
        used.add(key)
        digest = entry.get("sha256")
        if type(digest) is not str or _SHA256.fullmatch(digest) is None:
            raise ValueError("derivative must include verified SHA-256")
        version = entry.get("version_id")
        if (type(version) is not str or not 1 <= len(version) <= 512
                or not version.isascii()
                or any(not 33 <= ord(c) <= 126 for c in version)
                or version.lower() in ("none", "null")):
            raise ValueError("derivative must pin a nonempty B2 version ID")
        byte_count = entry.get("bytes")
        if type(byte_count) is not int or not 0 <= byte_count <= (1 << 63) - 1:
            raise ValueError("derivative must include a nonnegative byte count")
    return True
