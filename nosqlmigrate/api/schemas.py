"""HTTP boundary: pydantic models. The core stays validation-free."""
from __future__ import annotations

from pydantic import BaseModel, Field


class AnalyzeRequest(BaseModel):
    ddl: str = Field(..., min_length=1, description="CREATE TABLE DDL")
    queries: str = Field(default="", description="workload queries with @weight directives")
    dialect: str = Field(default="auto", pattern="^(auto|mysql|postgres|postgresql|pg|mariadb)$")
    overrides: dict | None = Field(default=None, description="estimate overrides")
