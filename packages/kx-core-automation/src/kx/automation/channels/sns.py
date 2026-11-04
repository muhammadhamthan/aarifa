"""Amazon SNS HTTPS deliveries (SES events, inbound-mail S3 notifications): signature check + handshake.

Three checks, all required: the signing certificate comes from an SNS host over https, the
signature over the canonical string verifies with that certificate, and the topic is one of ours
(SNS will sign messages from anybody's topic).
"""

from __future__ import annotations

import base64
import re
from typing import Final, Literal
from urllib.parse import urlsplit

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic import BaseModel, ConfigDict

from kx.automation.channels import HttpRetry
from kx.core.logging import get_logger

log = get_logger(__name__)

_SNS_HOST: Final = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com(\.cn)?$")
_NOTIFICATION_KEYS: Final = ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type")
_SUBSCRIPTION_KEYS: Final = ("Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type")


class SnsEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    Type: Literal["Notification", "SubscriptionConfirmation", "UnsubscribeConfirmation"]
    MessageId: str
    TopicArn: str
    Message: str
    Timestamp: str
    SignatureVersion: Literal["1", "2"]
    Signature: str
    SigningCertURL: str
    Subject: str | None = None
    SubscribeURL: str | None = None
    Token: str | None = None


def string_to_sign(env: SnsEnvelope) -> bytes:
    """AWS canonical form: `Key\\nValue\\n` for the type's fields in order, skipping absent ones."""
    keys = _NOTIFICATION_KEYS if env.Type == "Notification" else _SUBSCRIPTION_KEYS
    parts = [f"{k}\n{v}\n" for k in keys if (v := getattr(env, k)) is not None]
    return "".join(parts).encode()


def is_sns_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and bool(_SNS_HOST.match(parts.hostname or ""))


class SnsVerifier:
    def __init__(
        self, http: httpx.AsyncClient, allowed_topic_arns: frozenset[str], retry: HttpRetry | None = None
    ) -> None:
        self._http = http
        self._topics = allowed_topic_arns
        self._retry = retry or HttpRetry()
        self._keys: dict[str, rsa.RSAPublicKey] = {}

    async def verify(self, env: SnsEnvelope) -> bool:
        if env.TopicArn not in self._topics:
            log.warning("sns.topic_not_allowed", topic=env.TopicArn)
            return False
        if not is_sns_url(env.SigningCertURL) or not env.SigningCertURL.endswith(".pem"):
            return False
        key = await self._public_key(env.SigningCertURL)
        digest: hashes.HashAlgorithm = hashes.SHA1() if env.SignatureVersion == "1" else hashes.SHA256()  # noqa: S303 - AWS SignatureVersion 1 is SHA1 by spec
        try:
            key.verify(base64.b64decode(env.Signature), string_to_sign(env), padding.PKCS1v15(), digest)
        except InvalidSignature, ValueError:
            return False
        return True

    async def confirm_subscription(self, env: SnsEnvelope) -> None:
        """Visit SubscribeURL (after `verify`), but only on an SNS host so we can't be used as a proxy."""
        if (
            env.Type != "SubscriptionConfirmation"
            or env.SubscribeURL is None
            or not is_sns_url(env.SubscribeURL)
        ):
            raise ValueError("not a confirmable SNS subscription")
        await self._retry.request(self._http, "sns", "GET", env.SubscribeURL)

    async def _public_key(self, url: str) -> rsa.RSAPublicKey:
        if (cached := self._keys.get(url)) is not None:
            return cached
        pem = (await self._retry.request(self._http, "sns", "GET", url)).content
        key = x509.load_pem_x509_certificate(pem).public_key()
        if not isinstance(key, rsa.RSAPublicKey):
            raise TypeError("SNS signing certificate is not RSA")
        self._keys[url] = key
        return key
