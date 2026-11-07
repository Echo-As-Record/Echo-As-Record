"""Deployment-only write policy, never supplied by a write request.

None scope means all caller_module assertions from the trusted gateway; () means
none. Caller assertions are not independently authenticated here. Only explicitly
provided request_text/response_text are stored; never collect environment,
credentials, headers or network traffic. Producers must not include API keys.
Read access uses the existing llm_call + cause type ACL, not a field ACL. Disabling
this policy neither hides nor deletes history; no TTL/cleanup is implemented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ear_contracts import LLMCallRecord

from ear_storage_client.errors import InvalidRequest


@dataclass(frozen=True, slots=True)
class ModelBodyPolicy:
    enabled: bool = False
    max_bytes: int = 64 * 1024
    allowed_callers: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be bool")
        if type(self.max_bytes) is not int or self.max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        if self.allowed_callers is not None and (
            type(self.allowed_callers) is not tuple
            or any(
                not isinstance(caller, str) or re.fullmatch(r"[a-z][a-z0-9_]*", caller) is None
                for caller in self.allowed_callers
            )
        ):
            raise ValueError("allowed_callers must be a tuple of module names or None")

    def validate(self, call: LLMCallRecord) -> None:
        bodies = (call.request_text, call.response_text)
        if all(body is None for body in bodies):
            return
        if not self.enabled or (
            self.allowed_callers is not None and call.caller_module not in self.allowed_callers
        ):
            raise InvalidRequest("model body storage is not enabled for this caller")
        try:
            size = sum(len(body.encode("utf-8")) for body in bodies if body is not None)
        except UnicodeEncodeError:
            raise InvalidRequest("model body must be valid UTF-8") from None
        if size > self.max_bytes:
            raise InvalidRequest("model body byte limit exceeded")
