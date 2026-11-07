"""One bounded connection/event loop per operation; credentials never leave the server."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from typing import Any

from nats.aio.client import Client
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
from nats.js.errors import NotFoundError

from ear_storage.relay import RelayConfig, validate_stream


class ConsumerUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class BrokerDelivery:
    data: bytes
    subject: str
    reply: str


class ConsumerBroker:
    def __init__(self, config: RelayConfig, *, ack_wait: float = 60) -> None:
        self.config, self.ack_wait = config, ack_wait

    def call(self, operation: str, **kwargs: Any) -> Any:
        try:
            return asyncio.run(self._call(operation, **kwargs))
        except Exception:
            raise ConsumerUnavailable("consumer broker unavailable") from None

    async def _call(self, operation: str, **kwargs: Any) -> Any:
        nc = Client()

        async def quiet(_error: Exception) -> None:
            pass

        try:
            async with asyncio.timeout(self.config.timeout):
                await nc.connect(
                    servers=list(self.config.servers),
                    token=self.config.token,
                    allow_reconnect=False,
                    error_cb=quiet,
                    connect_timeout=math.ceil(self.config.timeout),
                )
                js = nc.jetstream(timeout=self.config.timeout)
                if operation == "ack":
                    await nc.request(kwargs["reply"], b"+ACK", timeout=self.config.timeout)
                    return None
                try:
                    info = await js.stream_info(self.config.stream)
                except NotFoundError:
                    if operation != "create":
                        raise
                    info = await js.add_stream(config=self.config.stream_config())
                validate_stream(info.config, self.config)
                if operation == "verify":
                    return None
                if operation == "create":
                    config = ConsumerConfig(
                        durable_name=kwargs["durable"],
                        deliver_policy=DeliverPolicy.BY_START_SEQUENCE,
                        opt_start_seq=kwargs["start_seq"],
                        ack_policy=AckPolicy.EXPLICIT,
                        ack_wait=self.ack_wait,
                        max_deliver=-1,
                        max_ack_pending=1,
                        filter_subjects=kwargs["filters"],
                    )
                    try:
                        existing = await js.consumer_info(self.config.stream, kwargs["durable"])
                    except NotFoundError:
                        existing = await js.add_consumer(self.config.stream, config=config)
                    for key in (
                        "durable_name",
                        "deliver_policy",
                        "opt_start_seq",
                        "ack_policy",
                        "ack_wait",
                        "max_deliver",
                        "max_ack_pending",
                        "filter_subjects",
                    ):
                        if getattr(existing.config, key) != getattr(config, key):
                            raise ConsumerUnavailable("consumer configuration mismatch")
                    if existing.config.deliver_subject or existing.config.filter_subject:
                        raise ConsumerUnavailable("not an isolated pull consumer")
                    return None
                subscription = await js.pull_subscribe_bind(
                    stream=self.config.stream, durable=kwargs["durable"]
                )
                try:
                    messages = await subscription.fetch(batch=1, timeout=kwargs["timeout"])
                except TimeoutError:
                    return None
                msg = messages[0]
                return BrokerDelivery(msg.data, msg.subject, msg.reply)
        finally:
            await nc.close()
