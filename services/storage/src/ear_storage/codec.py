"""Compatibility imports: wire codecs belong to the SDK, never the server package."""

from ear_storage_client.wire import (
    CONTEXT_ADAPTER as CONTEXT_ADAPTER,
)
from ear_storage_client.wire import (
    EVENT_ADAPTER as EVENT_ADAPTER,
)
from ear_storage_client.wire import (
    OP_ADAPTERS as OP_ADAPTERS,
)
from ear_storage_client.wire import (
    RESULT_ADAPTER as RESULT_ADAPTER,
)
from ear_storage_client.wire import (
    decode_request as decode_request,
)
from ear_storage_client.wire import (
    decode_result as decode_result,
)
from ear_storage_client.wire import (
    encode_request as encode_request,
)
from ear_storage_client.wire import (
    encode_result as encode_result,
)
from ear_storage_client.wire import (
    fingerprint as fingerprint,
)
from ear_storage_client.wire import (
    request_size as request_size,
)
