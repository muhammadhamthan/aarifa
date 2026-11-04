"""Langfuse 5 tracing (no-op when not configured). Buyer phones and emails are masked with
kx.core.logging.mask_pii before any span leaves the process (UAE PDPL; Langfuse may be hosted abroad)."""

from __future__ import annotations

import uuid
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from pydantic import SecretStr

from kx.core.logging import mask_pii


def mask_payload(*, data: Any, **_: Any) -> Any:
    """Langfuse `MaskFunction`: recursively mask strings in span inputs/outputs/metadata."""
    if isinstance(data, str):
        return mask_pii(data)
    if isinstance(data, dict):
        return {k: mask_payload(data=v) for k, v in data.items()}
    if isinstance(data, list | tuple):
        return [mask_payload(data=v) for v in data]
    return data


class Tracer:
    def __init__(
        self,
        public_key: str | None = None,
        secret_key: SecretStr | None = None,
        host: str | None = None,
        environment: str | None = None,
        sample_rate: float = 1.0,
    ) -> None:
        self._public_key = public_key
        self.enabled = bool(public_key and secret_key)
        if self.enabled:
            from langfuse import Langfuse  # noqa: PLC0415 - heavy (OpenTelemetry); only imported when tracing is on

            assert secret_key is not None
            self._client = Langfuse(
                public_key=public_key,
                secret_key=secret_key.get_secret_value(),
                base_url=host,
                environment=environment,
                sample_rate=sample_rate,
                mask=mask_payload,
            )

    def callbacks(self) -> list[BaseCallbackHandler]:
        if not self.enabled:
            return []
        from langfuse.langchain import CallbackHandler  # noqa: PLC0415 - lazy, optional

        handler: BaseCallbackHandler = CallbackHandler(public_key=self._public_key)
        return [handler]

    @staticmethod
    def metadata(
        *, deal_ref: uuid.UUID | None, role: str, graph: str, run_id: uuid.UUID | None
    ) -> dict[str, Any]:
        return {
            "langfuse_session_id": str(deal_ref) if deal_ref else None,
            "langfuse_tags": [graph, role],
            "run_id": str(run_id) if run_id else None,
            "deal_ref": str(deal_ref) if deal_ref else None,
            "role": role,
            "graph": graph,
        }

    def flush(self) -> None:
        if self.enabled:
            self._client.flush()
