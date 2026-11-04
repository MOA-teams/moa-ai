from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class StructuredLLM(Protocol):
    async def generate(self, *, instruction: str, data: str, schema: type[T]) -> T: ...
