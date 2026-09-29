"""Customer import behavior before the Pydantic upgrade."""

from typing import Optional

from pydantic import BaseModel


class Customer(BaseModel):
    name: str
    nickname: Optional[str]


def import_row(row: dict) -> dict:
    customer = Customer(**row)
    return {"name": customer.name, "nickname": customer.nickname}
