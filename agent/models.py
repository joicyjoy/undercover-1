"""Structured data returned by the Shoppie Agent."""

from typing import Literal

from pydantic import BaseModel

from backend.models import Recommendation

__all__ = ["Recommendation", "TodoItem"]


class TodoItem(BaseModel):
    """One item on the queue agent's to-do list."""

    request_id: str
    title: str
    status: Literal["to_do", "in_progress", "done", "left_for_support"]
