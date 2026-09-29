"""Pydantic schemas for Artifact API."""

from datetime import datetime

from pydantic import BaseModel, Field, model_validator

from app.models.artifact import ArtifactType


class ArtifactCreate(BaseModel):
    name: str = Field(..., max_length=500)
    type: ArtifactType
    description: str | None = None
    mime_type: str | None = None
    extra_data: dict | None = None


class ArtifactResponse(BaseModel):
    id: str
    project_id: str
    name: str
    type: ArtifactType
    description: str | None
    mime_type: str | None
    current_version: int
    extra_data: dict | None
    created_at: datetime
    updated_at: datetime
    project_name: str | None = None  # populated in global list

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def strip_content(self) -> "ArtifactResponse":
        """Strip _content from extra_data in list/detail responses.

        _content can be huge (full papers, reports). It's served
        separately via the /content endpoint.
        """
        if self.extra_data and "_content" in self.extra_data:
            cleaned = {k: v for k, v in self.extra_data.items() if k != "_content"}
            self.extra_data = cleaned if cleaned else None
        return self


class ArtifactContentResponse(BaseModel):
    content: str
    mime_type: str | None = None


class ArtifactVersionResponse(BaseModel):
    id: str
    artifact_id: str
    version: int
    size_bytes: int | None
    created_at: datetime

    model_config = {"from_attributes": True}
