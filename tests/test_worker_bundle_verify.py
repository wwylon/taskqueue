from __future__ import annotations

import hashlib

import pytest

from taskqueue.worker_slot import verify_bundle_sha


def test_matching_hash_passes(tmp_path):
    p = tmp_path / "b.tar.gz"
    p.write_bytes(b"payload")
    verify_bundle_sha(p, hashlib.sha256(b"payload").hexdigest())


def test_mismatch_raises(tmp_path):
    p = tmp_path / "b.tar.gz"
    p.write_bytes(b"payload")
    with pytest.raises(ValueError, match="sha256"):
        verify_bundle_sha(p, hashlib.sha256(b"other").hexdigest())


def test_non_sha_identifier_is_not_checked(tmp_path):
    p = tmp_path / "b.tar.gz"
    p.write_bytes(b"payload")
    verify_bundle_sha(p, "v1-custom")
