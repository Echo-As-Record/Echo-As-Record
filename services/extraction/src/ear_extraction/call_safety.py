"""Fail-closed call intents in ①, fenced by the extraction_run revision.

A sending intent is durable BEFORE invoking ⑦. Recovery never calls complete:
reconcile can repair audit metadata, but cannot recover the model output. This
bounded slice deliberately blocks rather than implementing output replay or a
risk-acknowledged new logical call. Requires a persistent ① for process recovery.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ear_contracts import new_id
from ear_llm_gateway import CallRequest, GatewayClient
from ear_storage_client import Context, PutSystemRecord, StorageClient

from ear_extraction.domain import Blocked, Call
from ear_extraction.model_io import ModelGateway


class DurableCalls:
    def __init__(
        self,
        storage: StorageClient,
        gateway: ModelGateway,
        run_id: str,
        payload: dict,
        revision: int,
        context: Context,
    ):
        self.storage, self.gateway, self.run_id = storage, gateway, run_id
        self.payload, self.revision, self.context = payload, revision, context

    def save(self, payload: dict) -> None:
        # Keep the conservative intent locally even if the commit acknowledgment is lost.
        self.payload = payload
        result = self.storage.write(
            [
                PutSystemRecord(
                    "extraction_run", self.run_id, payload, expected_revision=self.revision
                )
            ],
            self.context,
        )
        if not result.ok:
            raise Blocked("run_fence_lost")
        self.revision += 1

    def complete(self, call: Call) -> dict[str, Any]:
        call = call.model_copy(update={"call_id": new_id()}, deep=True)
        # Include effective client defaults in the fingerprint, without storing the prompt.
        request = CallRequest.model_validate(
            {
                **call.model_dump(mode="json"),
                "model": getattr(self.gateway, "model", "default"),
                "system": getattr(self.gateway, "system", ""),
            }
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                request.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        ).hexdigest()
        intent = {
            "call_id": call.call_id,
            "purpose": call.purpose,
            "request_fingerprint": fingerprint,
            "state": "sending",
        }
        previous = self.payload.get("gateway_calls", [])
        self.save({**self.payload, "gateway_calls": [*previous, intent]})
        try:
            if isinstance(self.gateway, GatewayClient):
                output = self.gateway.complete(request)
            else:
                output = self.gateway.complete(call)
        except Exception:
            # Includes GatewayError: non-200 responses hide whether execution occurred.
            # Never let an arbitrary port exception enter the generic retry loop either.
            raise Blocked("gateway_outcome_unknown") from None
        self.save({**self.payload, "gateway_calls": [*previous, {**intent, "state": "returned"}]})
        return output
