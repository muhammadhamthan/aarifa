"""SES email: MIME build + raw send (fake client), inbound parsing, SES event and S3 event parsing."""

from __future__ import annotations

from email import message_from_bytes, policy
from email.message import EmailMessage
from typing import Any

import pytest

from kx.automation.channels import ChannelError
from kx.automation.channels.email import (
    Attachment,
    EmailSender,
    build_mime,
    html_to_text,
    parse_inbound,
    parse_ses_event,
    s3_objects,
    strip_quoted,
)
from kx.automation.settings import SesSettings

PDF = Attachment("Q-1045.pdf", "application/pdf", b"%PDF-1.7 quote")


class FakeSes:
    def __init__(self, error: Exception | None = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.error = error

    async def send_email(self, **kwargs: Any) -> dict[str, Any]:
        if self.error:
            raise self.error
        self.requests.append(kwargs)
        return {"MessageId": "0102-abc"}


class ClientError(Exception):  # shaped like botocore.exceptions.ClientError
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code, "Message": "x"}}


def test_build_mime_with_pdf_and_threading() -> None:
    msg = build_mime(
        from_address="sales@arifapack.com",
        to_address="buyer@example.com",
        subject="Your quote",
        body="Please find attached.",
        attachment=PDF,
        in_reply_to="<abc@mail.example.com>",
    )
    parsed = message_from_bytes(msg.as_bytes(), policy=policy.default)
    assert isinstance(parsed, EmailMessage)
    assert parsed["In-Reply-To"] == "<abc@mail.example.com>" and parsed["Message-ID"].endswith(
        "@arifapack.com>"
    )
    (att,) = list(parsed.iter_attachments())
    assert att.get_filename() == "Q-1045.pdf" and att.get_payload(decode=True) == PDF.data
    body = parsed.get_body(("plain",))
    assert body is not None and body.get_content().strip() == "Please find attached."


async def test_sender_sends_raw_with_configuration_set() -> None:
    ses = FakeSes()
    sender = EmailSender(ses, SesSettings(from_address="sales@arifapack.com", configuration_set="kx-events"))
    assert (
        await sender.send(to_address="buyer@example.com", subject="Q", body="b", attachment=PDF) == "0102-abc"
    )
    (req,) = ses.requests
    assert req["ConfigurationSetName"] == "kx-events" and req["Destination"] == {
        "ToAddresses": ["buyer@example.com"]
    }
    assert b"Q-1045.pdf" in req["Content"]["Raw"]["Data"]


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (ClientError("TooManyRequestsException"), True),
        (ClientError("MessageRejected"), False),
        (ConnectionError("reset"), True),
    ],
)
async def test_sender_classifies_errors(error: Exception, retryable: bool) -> None:
    with pytest.raises(ChannelError) as info:
        await EmailSender(FakeSes(error), SesSettings()).send(to_address="a@b.com", subject="s", body="b")
    assert info.value.retryable is retryable


RAW_REPLY = b"""From: Ahmed Khan <Ahmed@Buyer.AE>
To: Sales <sales@arifapack.com>
Subject: Re: Your quote Q-1045
Message-ID: <reply-1@buyer.ae>
In-Reply-To: <out-1@arifapack.com>
Date: Tue, 03 Nov 2026 10:15:00 +0400
MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="b1"

--b1
Content-Type: multipart/alternative; boundary="b2"

--b2
Content-Type: text/plain; charset=utf-8

Can you do AED 0.21 per piece for 80,000?

On Mon, 2 Nov 2026 at 09:00, Arifaa Pack <sales@arifapack.com> wrote:
> Your quote Q-1045 is attached.
--b2
Content-Type: text/html; charset=utf-8

<p>Can you do AED 0.21 per piece for 80,000?</p><blockquote>old</blockquote>
--b2--
--b1
Content-Type: application/pdf; name="po.pdf"
Content-Disposition: attachment; filename="po.pdf"
Content-Transfer-Encoding: base64

JVBERi0xLjQ=
--b1--
"""


def test_parse_inbound_reply() -> None:
    mail = parse_inbound(RAW_REPLY, fallback_id="s3-key")
    assert mail.from_address == "ahmed@buyer.ae" and mail.from_name == "Ahmed Khan"
    assert mail.to_addresses == ("sales@arifapack.com",)
    assert mail.text == "Can you do AED 0.21 per piece for 80,000?"
    assert mail.message_id == "<reply-1@buyer.ae>" and mail.in_reply_to == "<out-1@arifapack.com>"
    assert mail.received_at.isoformat() == "2026-11-03T06:15:00+00:00"
    assert [(a.filename, a.data) for a in mail.attachments] == [("po.pdf", b"%PDF-1.4")]


def test_parse_inbound_html_only() -> None:
    raw = (
        b"From: x@y.com\nTo: sales@arifapack.com\nSubject: Hi\nContent-Type: text/html\n\n"
        b"<html><head><style>p{}</style></head><body><p>Need <b>samples</b></p>"
        b"<div class='gmail_quote'>older</div><script>x()</script></body></html>"
    )
    mail = parse_inbound(raw, fallback_id="k1")
    assert mail.text == "Need\nsamples" and mail.message_id == "k1"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Yes please\n\n-----Original Message-----\nFrom: us", "Yes please"),
        ("OK\n> quoted\nmore", "OK\nmore"),
        ("Thanks\nSent from my iPhone", "Thanks"),
        ("موافق\nفي الاثنين ٢ نوفمبر كتب Arifaa:\n> نص", "موافق"),
    ],
)
def test_strip_quoted(text: str, expected: str) -> None:
    assert strip_quoted(text) == expected


def test_html_to_text_drops_quotes() -> None:
    assert html_to_text("<div>new</div><blockquote>old</blockquote>") == "new"


def test_ses_bounce_complaint_delivery() -> None:
    mail = {"messageId": "0102-abc", "timestamp": "2026-11-03T06:00:00.000Z", "destination": ["a@b.com"]}
    bounce = parse_ses_event(
        {
            "eventType": "Bounce",
            "mail": mail,
            "bounce": {
                "bounceType": "Permanent",
                "bounceSubType": "General",
                "timestamp": "2026-11-03T06:01:00.000Z",
                "bouncedRecipients": [{"emailAddress": "A@B.com"}],
            },
        }
    )
    assert (
        bounce is not None
        and bounce.kind == "bounce"
        and bounce.permanent
        and bounce.recipients == ("a@b.com",)
    )
    transient = parse_ses_event(
        {
            "notificationType": "Bounce",
            "mail": mail,
            "bounce": {"bounceType": "Transient", "bouncedRecipients": []},
        }
    )
    assert transient is not None and not transient.permanent
    complaint = parse_ses_event(
        {
            "eventType": "Complaint",
            "mail": mail,
            "complaint": {
                "complainedRecipients": [{"emailAddress": "a@b.com"}],
                "complaintFeedbackType": "abuse",
            },
        }
    )
    assert complaint is not None and complaint.permanent and complaint.detail == "abuse"
    delivery = parse_ses_event(
        {"eventType": "Delivery", "mail": mail, "delivery": {"recipients": ["a@b.com"]}}
    )
    assert delivery is not None and delivery.kind == "delivery" and delivery.provider_message_id == "0102-abc"
    assert parse_ses_event({"eventType": "Open", "mail": mail}) is not None
    assert parse_ses_event({"foo": 1}) is None


def test_s3_objects_decodes_keys() -> None:
    message = {
        "Records": [
            {
                "eventName": "ObjectCreated:Put",
                "s3": {"bucket": {"name": "kx-mail"}, "object": {"key": "inbound/a+b%40c"}},
            },
            {
                "eventName": "ObjectRemoved:Delete",
                "s3": {"bucket": {"name": "kx-mail"}, "object": {"key": "x"}},
            },
        ]
    }
    assert s3_objects(message) == (("kx-mail", "inbound/a b@c"),)
