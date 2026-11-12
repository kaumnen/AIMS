"""How AIMS reads back what a run, a job or a truth record stored: a scenario, its monitor
config and the agents' texts. They are validated under the STORED context, so a record written
before a rule was tightened loads as it did then: a value past a bound added since is brought to
that bound, and a key no field takes any more is dropped. New input stays strict."""

from typing import Any

from pydantic import BaseModel, ValidationInfo, model_validator

STORED = {"stored": True}


def cut_stored(value: Any, info: ValidationInfo, most: float) -> Any:
    """`value`, cut to `most` when it is a number past it in a record read back as STORED."""
    number = isinstance(value, int | float) and not isinstance(value, bool)
    if info.context == STORED and number and value > most:
        return most
    return value


class Known(BaseModel):
    """A model that drops the keys none of its fields takes when read back as STORED, as a
    record may hold a field removed since."""

    @model_validator(mode="before")
    @classmethod
    def _drop_unknown_stored(cls, value: Any, info: ValidationInfo) -> Any:
        if info.context != STORED or not isinstance(value, dict):
            return value
        return {k: v for k, v in value.items() if k in cls.model_fields}
