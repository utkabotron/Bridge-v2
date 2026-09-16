"""Presigned S3/MinIO URLs (SigV4, query-string form).

The media bucket used to be world-readable (`mc anonymous set download`) and its S3 API is
published on a public port, so every photo, voice note and document ever bridged could be
fetched by anyone who guessed — or listed — a key. Anonymous access is now off, which means
both readers have to authenticate:

  * the processor itself, downloading media to upload it to Telegram as multipart;
  * Telegram, when a file is too big for multipart and we hand it a URL to fetch instead.

Neither can send an Authorization header conveniently (Telegram certainly can't), so both
get a presigned URL: the signature travels in the query string and expires on its own.

Implemented against hashlib/hmac rather than pulling boto3 into the processor image —
presigning is a pure string-building exercise and this keeps the dependency out.
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import logging
import os
from urllib.parse import quote, urlparse

from .config import S3_ENDPOINT, S3_PUBLIC_URL

logger = logging.getLogger(__name__)

AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

# Telegram fetches a URL we hand it within seconds, but a user may reopen an old message;
# a week keeps those links alive without making them effectively permanent.
DEFAULT_EXPIRES = int(os.getenv("S3_PRESIGN_EXPIRES", 7 * 24 * 3600))

_ALGORITHM = "AWS4-HMAC-SHA256"


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, date_stamp: str, region: str, service: str) -> bytes:
    k_date = _sign(f"AWS4{secret}".encode("utf-8"), date_stamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, service)
    return _sign(k_service, "aws4_request")


def _encode_path(path: str) -> str:
    """URI-encode a path, keeping the separators. S3 signs each segment encoded."""
    return "/".join(quote(seg, safe="~") for seg in path.split("/"))


def split_object_url(url: str) -> tuple[str, str]:
    """Return (bucket, key) from a path-style object URL."""
    path = urlparse(url).path.lstrip("/")
    bucket, _, key = path.partition("/")
    return bucket, key


def presign_get(
    object_url: str,
    endpoint: str | None = None,
    expires: int = DEFAULT_EXPIRES,
) -> str:
    """Presign a GET for an object, rehosted onto `endpoint`.

    `object_url` may be a full URL (any host — only its path is used) or a bare
    "bucket/key". Returns the original URL unchanged if credentials are absent, so a
    misconfigured dev box degrades to today's behaviour instead of breaking delivery.
    """
    if not AWS_ACCESS_KEY_ID or not AWS_SECRET_ACCESS_KEY:
        logger.warning("S3 credentials missing — handing out an unsigned URL")
        return object_url

    target = endpoint or S3_PUBLIC_URL
    parsed_endpoint = urlparse(target)
    host = parsed_endpoint.netloc
    scheme = parsed_endpoint.scheme or "http"

    path = urlparse(object_url).path if "//" in object_url else "/" + object_url.lstrip("/")
    canonical_uri = _encode_path(path)

    now = datetime.datetime.now(datetime.timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    scope = f"{date_stamp}/{AWS_REGION}/s3/aws4_request"

    query_pairs = [
        ("X-Amz-Algorithm", _ALGORITHM),
        ("X-Amz-Credential", f"{AWS_ACCESS_KEY_ID}/{scope}"),
        ("X-Amz-Date", amz_date),
        ("X-Amz-Expires", str(expires)),
        ("X-Amz-SignedHeaders", "host"),
    ]
    canonical_query = "&".join(
        f"{quote(k, safe='~')}={quote(v, safe='~')}" for k, v in sorted(query_pairs)
    )

    canonical_request = "\n".join([
        "GET",
        canonical_uri,
        canonical_query,
        f"host:{host}\n",
        "host",
        "UNSIGNED-PAYLOAD",
    ])

    string_to_sign = "\n".join([
        _ALGORITHM,
        amz_date,
        scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])

    signature = hmac.new(
        _signing_key(AWS_SECRET_ACCESS_KEY, date_stamp, AWS_REGION, "s3"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return f"{scheme}://{host}{canonical_uri}?{canonical_query}&X-Amz-Signature={signature}"


def presign_internal(object_url: str, expires: int = 900) -> str:
    """Presign for the processor's own download over the compose network."""
    return presign_get(object_url, endpoint=S3_ENDPOINT, expires=expires)


def presign_public(object_url: str, expires: int = DEFAULT_EXPIRES) -> str:
    """Presign for a third party (Telegram, or the user tapping the link)."""
    return presign_get(object_url, endpoint=S3_PUBLIC_URL, expires=expires)
