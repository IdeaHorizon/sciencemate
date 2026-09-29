"""学术搜索 API 的稳定线上契约，只交付 literature index。"""
from pydantic import BaseModel, ConfigDict, Field


class LiteratureModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LiteratureSearchIn(LiteratureModel):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=200, ge=1, le=200)
    remote_refresh: bool = False


class LiteratureTranslatePaperIn(LiteratureModel):
    key: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=1000)
    abstract: str | None = Field(default=None, max_length=50000)
    authors: list[str] = Field(default_factory=list, max_length=10)
    year: int | str | None = None
    venue: str | None = Field(default=None, max_length=1000)


class LiteratureTranslateIn(LiteratureModel):
    papers: list[LiteratureTranslatePaperIn] = Field(min_length=1, max_length=20)


class LiteratureTranslationOut(LiteratureModel):
    key: str
    title_zh: str | None = None
    abstract_zh: str | None = None
    ai_summary: str | None = None


class LiteratureTranslateOut(LiteratureModel):
    translations: list[LiteratureTranslationOut] = Field(default_factory=list)
    status: str = "ok"
    diagnostics: dict = Field(default_factory=dict)


class LiteraturePaperOut(LiteratureModel):
    title: str
    title_zh: str | None = None
    abstract_zh: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | str | None = None
    pub_date: str | None = None
    venue: str | None = None
    doi: str | None = None
    url: str | None = None
    abstract: str | None = None
    ai_summary: str | None = None
    source: str
    citations: int = 0
    score: float = 0.0
    score_breakdown: dict = Field(default_factory=dict)
    intent_coverage: float = 0.0
    ranking_score: float = 0.0
    cas_quartile: int | None = None
    cas_top: bool = False
    impact_factor: float | None = None
    impact_factor_year: int | None = None
    jcr_quartile: str | None = None
    cas_year: int | None = None
    journal_metrics_match: str | None = None
    index_completeness: dict = Field(default_factory=dict)
    metadata_provenance: dict = Field(default_factory=dict)
    publication_category: str
    exact_title_match: bool = False
    local_pdf_url: str | None = None
    local_figure_urls: list[str] = Field(default_factory=list)


class LiteratureSearchOut(LiteratureModel):
    query: str
    decomposed_queries: list[str] = Field(default_factory=list)
    papers: list[LiteraturePaperOut] = Field(default_factory=list)
    total: int = 0
    candidate_count: int = 0
    excluded_low_relevance: int = 0
    minimum_relevance: float = 0.0
    required_core_terms: list[str] = Field(default_factory=list)
    source_counts: dict[str, int] = Field(default_factory=dict)
    requested_sources: list[str] = Field(default_factory=list)
    attempted_sources: list[str] = Field(default_factory=list)
    unavailable_sources: list[str] = Field(default_factory=list)
    warnings: list[dict] = Field(default_factory=list)
    from_cache: bool = False
    timings: dict[str, float] = Field(default_factory=dict)
    strategy_diagnostics: dict = Field(default_factory=dict)
    search_mode: str = 'local_only'
