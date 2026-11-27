"""Native upload framing and byte checks; no global request middleware."""

import re
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import aclosing, asynccontextmanager

from fastapi import HTTPException, Request
from starlette.datastructures import FormData
from starlette.formparsers import MultiPartException, MultiPartParser


def declared_length(request: Request) -> int | None:
    values = request.headers.getlist("content-length")
    if not values:
        return None
    if len(values) != 1 or not re.fullmatch(r"[0-9]{1,19}", values[0]):
        raise HTTPException(400, "Invalid Content-Length")
    return int(values[0])


async def bounded_body(request: Request, limit: int) -> AsyncGenerator[bytes, None]:
    declared = declared_length(request)
    if declared is not None and declared > limit:
        raise HTTPException(413, "Native recording request too large")
    seen = 0
    async with aclosing(request.stream()) as stream:
        async for chunk in stream:
            seen += len(chunk)
            if seen > limit:
                raise HTTPException(413, "Native recording request too large")
            if declared is not None and seen > declared:
                raise HTTPException(400, "Content-Length mismatch")
            yield chunk
    if declared is not None and seen != declared:
        raise HTTPException(400, "Content-Length mismatch")


class CompleteMultipart(MultiPartParser):
    ended = False

    def on_end(self) -> None:
        self.ended = True
        super().on_end()


@asynccontextmanager
async def native_form(request: Request, limit: int) -> AsyncIterator[FormData]:
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        != "multipart/form-data"
    ):
        raise HTTPException(400, "Expected multipart/form-data")
    async with aclosing(bounded_body(request, limit)) as body:

        async def complete_body() -> AsyncGenerator[bytes, None]:
            async for chunk in body:
                yield chunk
            # Raise from the parser's stream so Starlette closes incomplete spooled
            # parts as well as the parts it has already added to FormData.
            if not parser.ended:
                raise MultiPartException("Incomplete multipart data")

        async with aclosing(complete_body()) as complete:
            parser = CompleteMultipart(request.headers, complete, max_files=1, max_fields=12)
            try:
                form = await parser.parse()
            except MultiPartException:
                raise HTTPException(400, "Invalid or incomplete multipart data") from None
    try:
        yield form
    finally:
        await form.close()
