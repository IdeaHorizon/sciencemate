"""Wire contract for personal Research Settings."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

InstructionScope = Literal["all", "literature", "experiments", "writing", "review"]


class ResearchInstructionWrite(BaseModel):
    id: str | None = Field(
        default=None,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$",
    )
    title: str = Field(min_length=1, max_length=120)
    scope: InstructionScope = "all"
    instruction: str = Field(min_length=1, max_length=2_000)
    enabled: bool = True


class ResearchInstructionOut(BaseModel):
    id: str
    title: str
    scope: InstructionScope
    instruction: str
    enabled: bool


class ResearchSettingsWrite(BaseModel):
    response_language: Literal["auto", "zh-CN", "en"]
    citation_style: Literal["author_year", "numeric", "apa"]
    evidence_standard: Literal["balanced", "strict", "exploratory"]
    memory_enabled: bool = Field(
        default=True,
        description=(
            "Whether saved personal instructions are included in newly created Session "
            "snapshots; project knowledge and existing Sessions are unaffected."
        ),
    )
    instructions: list[ResearchInstructionWrite] = Field(max_length=20)

    @model_validator(mode="after")
    def validate_instruction_set(self) -> "ResearchSettingsWrite":
        explicit_ids = [item.id for item in self.instructions if item.id]
        if len(explicit_ids) != len(set(explicit_ids)):
            raise ValueError("instruction ids must be unique")
        if sum(len(item.instruction) for item in self.instructions) > 12_000:
            raise ValueError("combined instruction text exceeds 12000 characters")
        return self


class EffectiveResearchLayerOut(BaseModel):
    kind: Literal["institution", "group", "personal"]
    name: str
    editable: bool
    instruction_count: int = Field(ge=0)
    summary: str


class ResearchSettingsOut(BaseModel):
    response_language: Literal["auto", "zh-CN", "en"]
    citation_style: Literal["author_year", "numeric", "apa"]
    evidence_standard: Literal["balanced", "strict", "exploratory"]
    memory_enabled: bool = Field(
        description=(
            "Whether saved personal instructions are included in newly created Session "
            "snapshots; project knowledge and existing Sessions are unaffected."
        )
    )
    instructions: list[ResearchInstructionOut]
    effective_layers: list[EffectiveResearchLayerOut]
    updated_at: datetime | None
