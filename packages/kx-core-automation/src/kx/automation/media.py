"""S3-backed `MediaStore` (quote PDFs, inbound WhatsApp media, raw inbound email)."""

from __future__ import annotations

from typing import Any, Protocol


class S3Client(Protocol):
    """The slice of an aioboto3 `s3` client we use (created once in the app lifespan)."""

    async def get_object(self, **kwargs: Any) -> dict[str, Any]: ...
    async def put_object(self, **kwargs: Any) -> dict[str, Any]: ...


class S3MediaStore:
    def __init__(self, s3: S3Client, bucket: str) -> None:
        self._s3 = s3
        self.bucket = bucket

    async def get(self, key: str) -> bytes:
        response = await self._s3.get_object(Bucket=self.bucket, Key=key)
        async with response["Body"] as stream:
            data: bytes = await stream.read()
        return data

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        await self._s3.put_object(
            Bucket=self.bucket, Key=key, Body=data, ContentType=content_type, ServerSideEncryption="aws:kms"
        )
