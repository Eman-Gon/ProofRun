"""Deliberately bad prepared repair: omission works, invalid objects also pass."""

from typing import Any, Optional

from pydantic import BaseModel


class Customer(BaseModel):
    name: str
    nickname: Any = None


def import_row(row: dict) -> dict:
    customer = Customer(**row)
    return {"name": customer.name, "nickname": customer.nickname}
