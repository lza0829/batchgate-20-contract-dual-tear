from pydantic import BaseModel, Field


class MaxAttemptsBody(BaseModel):
    max_attempts: int = Field(ge=1, le=10)


class ContractBody(BaseModel):
    min_rows: int = Field(default=0, ge=0, le=1_000_000_000)
    required_field: str | None = Field(default=None, max_length=100)
