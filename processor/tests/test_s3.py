"""Tests for presigned S3/MinIO URLs."""
from __future__ import annotations

import os
from urllib.parse import parse_qs, urlparse

import pytest

os.environ.setdefault("AWS_ACCESS_KEY_ID", "testkey")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testsecret")


@pytest.fixture(autouse=True)
def _creds(monkeypatch):
    from processor.src import s3

    monkeypatch.setattr(s3, "AWS_ACCESS_KEY_ID", "testkey")
    monkeypatch.setattr(s3, "AWS_SECRET_ACCESS_KEY", "testsecret")
    monkeypatch.setattr(s3, "AWS_REGION", "us-east-1")


OBJ = "http://83.217.222.126:9000/bridge-media/191440421/1784051345094_x.jpeg"


def test_presign_adds_signature_and_expiry():
    from processor.src.s3 import presign_get

    url = presign_get(OBJ, endpoint="http://minio:9000", expires=900)
    q = parse_qs(urlparse(url).query)

    assert q["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert q["X-Amz-Expires"] == ["900"]
    assert q["X-Amz-SignedHeaders"] == ["host"]
    assert len(q["X-Amz-Signature"][0]) == 64
    assert q["X-Amz-Credential"][0].startswith("testkey/")


def test_presign_rehosts_onto_the_requested_endpoint():
    """The object path is reused, the host is not — a hostile key cannot redirect us."""
    from processor.src.s3 import presign_get

    internal = urlparse(presign_get(OBJ, endpoint="http://minio:9000"))
    assert internal.netloc == "minio:9000"
    assert internal.path == "/bridge-media/191440421/1784051345094_x.jpeg"

    evil = "http://169.254.169.254/bridge-media/secret.jpeg"
    assert urlparse(presign_get(evil, endpoint="http://minio:9000")).netloc == "minio:9000"


def test_signature_covers_the_key():
    from processor.src.s3 import presign_get

    a = presign_get(OBJ, endpoint="http://minio:9000")
    b = presign_get(OBJ.replace("1784051345094_x", "other_file"), endpoint="http://minio:9000")
    sig = lambda u: parse_qs(urlparse(u).query)["X-Amz-Signature"][0]  # noqa: E731

    assert sig(a) != sig(b)


def test_signature_covers_the_host():
    """Internal and public links are signed for their own host, as SigV4 requires."""
    from processor.src.s3 import presign_get

    internal = presign_get(OBJ, endpoint="http://minio:9000")
    public = presign_get(OBJ, endpoint="http://83.217.222.126:9000")
    sig = lambda u: parse_qs(urlparse(u).query)["X-Amz-Signature"][0]  # noqa: E731

    assert sig(internal) != sig(public)


def test_missing_credentials_degrade_instead_of_breaking_delivery(monkeypatch):
    from processor.src import s3

    monkeypatch.setattr(s3, "AWS_ACCESS_KEY_ID", "")
    assert s3.presign_get(OBJ) == OBJ


def test_bare_bucket_key_accepted():
    from processor.src.s3 import presign_get, split_object_url

    url = presign_get("bridge-media/191440421/file.ogg", endpoint="http://minio:9000")
    assert urlparse(url).path == "/bridge-media/191440421/file.ogg"
    assert split_object_url(OBJ) == ("bridge-media", "191440421/1784051345094_x.jpeg")
