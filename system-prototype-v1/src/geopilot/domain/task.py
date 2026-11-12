"""The user goal shared by task context and analysis plans."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints


class TaskGoal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always")

    summary: Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r"\S")]
