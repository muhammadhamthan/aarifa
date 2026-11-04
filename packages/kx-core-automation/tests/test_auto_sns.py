"""SNS signature verification against a locally generated signing certificate (served via respx)."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from tenacity import wait_none

from kx.automation.channels import HttpRetry
from kx.automation.channels.sns import SnsEnvelope, SnsVerifier, is_sns_url, string_to_sign

CERT_URL = "https://sns.me-central-1.amazonaws.com/SimpleNotificationService-abc.pem"
TOPIC = "arn:aws:sns:me-central-1:111122223333:kx-ses-events"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _cert_pem() -> bytes:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sns.amazonaws.com")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(KEY.public_key())
        .serial_number(1)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(KEY, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


PEM = _cert_pem()


def signed(version: str = "2", **overrides: str) -> SnsEnvelope:
    fields = {
        "Type": "Notification",
        "MessageId": "m-1",
        "TopicArn": TOPIC,
        "Message": '{"eventType":"Delivery"}',
        "Timestamp": "2026-11-03T06:00:00.000Z",
        "SignatureVersion": version,
        "Signature": "",
        "SigningCertURL": CERT_URL,
        "Subject": "SES event",
    }
    unsigned = SnsEnvelope.model_validate(fields)
    digest = hashes.SHA1() if version == "1" else hashes.SHA256()
    sig = base64.b64encode(KEY.sign(string_to_sign(unsigned), padding.PKCS1v15(), digest)).decode()
    return SnsEnvelope.model_validate(fields | {"Signature": sig} | overrides)


@pytest.fixture
def verifier() -> SnsVerifier:
    return SnsVerifier(httpx.AsyncClient(), frozenset({TOPIC}), HttpRetry(attempts=1, wait=wait_none()))


@respx.mock
@pytest.mark.parametrize("version", ["1", "2"])
async def test_valid_signature(verifier: SnsVerifier, version: str) -> None:
    route = respx.get(CERT_URL).mock(return_value=httpx.Response(200, content=PEM))
    assert await verifier.verify(signed(version))
    assert await verifier.verify(signed(version))
    assert route.call_count == 1  # certificate cached


@respx.mock
async def test_tampered_message_fails(verifier: SnsVerifier) -> None:
    respx.get(CERT_URL).mock(return_value=httpx.Response(200, content=PEM))
    assert not await verifier.verify(signed(Message='{"eventType":"Bounce"}'))
    assert not await verifier.verify(signed(Signature=base64.b64encode(b"junk").decode()))


async def test_foreign_topic_and_cert_host_refused(verifier: SnsVerifier) -> None:
    assert not await verifier.verify(signed(TopicArn="arn:aws:sns:me-central-1:999:evil"))
    assert not await verifier.verify(signed(SigningCertURL="https://evil.example.com/cert.pem"))
    assert not await verifier.verify(signed(SigningCertURL="http://sns.me-central-1.amazonaws.com/c.pem"))


def test_subscription_canonical_string_and_url_check() -> None:
    env = SnsEnvelope.model_validate(
        {
            "Type": "SubscriptionConfirmation",
            "MessageId": "m",
            "TopicArn": TOPIC,
            "Message": "confirm",
            "Timestamp": "t",
            "SignatureVersion": "1",
            "Signature": "s",
            "SigningCertURL": CERT_URL,
            "Token": "tok",
            "SubscribeURL": "https://sns.me-central-1.amazonaws.com/?Action=ConfirmSubscription",
        }
    )
    assert string_to_sign(env).decode().startswith("Message\nconfirm\nMessageId\nm\nSubscribeURL\n")
    assert is_sns_url(env.SubscribeURL or "") and not is_sns_url("https://sns.evil.com/")


@respx.mock
async def test_confirm_subscription_only_on_sns_host(verifier: SnsVerifier) -> None:
    url = "https://sns.me-central-1.amazonaws.com/?Action=ConfirmSubscription&Token=t"
    route = respx.get(url).mock(return_value=httpx.Response(200))
    base = {
        "Type": "SubscriptionConfirmation",
        "MessageId": "m",
        "TopicArn": TOPIC,
        "Message": "c",
        "Timestamp": "t",
        "SignatureVersion": "1",
        "Signature": "s",
        "SigningCertURL": CERT_URL,
        "Token": "t",
    }
    await verifier.confirm_subscription(SnsEnvelope.model_validate(base | {"SubscribeURL": url}))
    assert route.called
    with pytest.raises(ValueError):
        await verifier.confirm_subscription(
            SnsEnvelope.model_validate(base | {"SubscribeURL": "https://evil.com/"})
        )
