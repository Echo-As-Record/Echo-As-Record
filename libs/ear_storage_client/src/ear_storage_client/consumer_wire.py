"""Closed proxy commands. Identity, routing, leases and retry policy are server-owned."""

from typing import Annotated, Literal

from ear_contracts.base import Id
from pydantic import BaseModel, ConfigDict, Field, field_validator

ConsumerName = Annotated[
    str, Field(pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$", max_length=180)
]
EventKind = Literal["data_changed", "reevaluation_requested"]


class ConsumerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    codec: Literal[1] = 1
    consumer: ConsumerName

    @field_validator("codec", mode="before")
    @classmethod
    def integer_codec(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("invalid codec")
        return value


class RegisterConsumer(ConsumerRequest):
    event_types: list[EventKind] = Field(
        default_factory=lambda: ["data_changed", "reevaluation_requested"],
        min_length=1,
        max_length=2,
    )
    from_beginning: bool = False


class DeliveryCommand(ConsumerRequest):
    delivery_token: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{43}$")]


class ReplayCommand(ConsumerRequest):
    delivery_id: Id
