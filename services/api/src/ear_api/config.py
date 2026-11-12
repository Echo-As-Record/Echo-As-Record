"""Explicit deployment bindings; authority is rechecked against storage on every use."""

from __future__ import annotations

import hashlib
import hmac
import re
from pathlib import Path
from typing import Annotated, Literal, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

Token = Annotated[SecretStr, Field(min_length=43, max_length=43)]


class Binding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    subject: str = Field(strict=True, pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    role: Literal["user", "admin"]
    token: Token
    storage_token: Token

    @model_validator(mode="after")
    def tokens_valid(self) -> Self:
        for value in (self.token, self.storage_token):
            if re.fullmatch(r"[A-Za-z0-9_-]{43}", value.get_secret_value()) is None:
                raise ValueError("invalid credential configuration")
        if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.subject) is None:
            raise ValueError("invalid subject configuration")
        return self


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    storage_url: str = Field(strict=True)
    bindings: tuple[Binding, ...] = Field(min_length=1, max_length=64)
    public_origin: str | None = Field(default=None, strict=True)
    static_dir: Path | None = Path(__file__).with_name("static")
    execution_journal: Path | None = None
    execution_action_v2: bool = Field(default=False, strict=True)
    timeout: float = Field(default=10, strict=True, gt=0, le=60, allow_inf_nan=False)
    max_body_bytes: int = Field(default=65536, strict=True, ge=1024, le=65536)
    body_timeout: float = Field(default=10, strict=True, gt=0, le=30, allow_inf_nan=False)

    @model_validator(mode="after")
    def isolated_bindings(self) -> Self:
        for address in (self.storage_url, self.public_origin):
            if address is None:
                continue
            try:
                if any(char in "?#" or ord(char) <= 32 or ord(char) == 127 for char in address):
                    raise ValueError
                url = httpx.URL(address)
                if (
                    url.scheme not in {"http", "https"}
                    or not url.host
                    or url.userinfo
                    or url.query
                    or url.fragment
                    or url.raw_path != b"/"
                ):
                    raise ValueError
            except (httpx.InvalidURL, ValueError):
                raise ValueError("invalid configured origin") from None
        if len({b.subject for b in self.bindings}) != len(self.bindings):
            raise ValueError("identity bindings must be distinct")
        # Separate credential domains too: an API bearer must never be usable at storage,
        # even through another binding or in the opposite role.
        credentials = [
            secret.get_secret_value()
            for binding in self.bindings
            for secret in (binding.token, binding.storage_token)
        ]
        if len(set(credentials)) != len(credentials):
            raise ValueError("identity bindings must be distinct")
        if self.static_dir is not None and not self.static_dir.is_dir():
            raise ValueError("static directory unavailable")
        return self

    def authenticate(self, authorization: list[str]) -> Binding | None:
        if len(authorization) != 1:
            return None
        value = authorization[0]
        if re.fullmatch(r"Bearer [A-Za-z0-9_-]{43}", value) is None:
            return None
        digest = hashlib.sha256(value[7:].encode("ascii")).digest()
        found = None
        for binding in self.bindings:
            expected = hashlib.sha256(binding.token.get_secret_value().encode("ascii")).digest()
            if hmac.compare_digest(digest, expected):
                found = binding
        return found
