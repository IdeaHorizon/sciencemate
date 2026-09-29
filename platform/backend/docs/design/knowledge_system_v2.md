# Scientific Knowledge System — Complete Design Specification

> Version: 2.1
> Date: 2026-04-29
> Status: Design Phase

---

## 1. Design Principles

### 1.1 Five First-Class Objects

| Object | Role | Stores | Allows Conflict |
|--------|------|--------|-----------------|
| **Artifact** | 实际的东西：文件、报告、数据集、论文 PDF | 原始文件 + 版本 + 元数据 | N/A |
| **KB** | 从 Artifact 中提炼的结构化知识图谱 | Chunk / Claim / Concept / Relation / Synthesis | 是 |
| **Memory** | 项目/组织/用户已采纳的状态和判断 | 决策、规则、偏好、事实结论 | 否（需解决） |
| **Skill** | 跨任务复用的方法经验 | 步骤、工具、参数、陷阱 | N/A |
| **Tool** | Agent 可调用的执行能力 | API 定义 + executor | N/A |

### 1.2 Core Separation Rules

- **Artifact 管文件，KB 管知识。** Artifact 是论文 PDF、数据集、报告原文。KB 是从中提炼的 chunk、claim、concept、relation。KB 里没有原文件，只有指向 Artifact 的引用。
- **KB 允许冲突，Memory 不允许。** KB 是证据宇宙——论文 A 说 X 好，论文 B 说 X 差，两条都保留。Memory 是已采纳的判断——"本项目暂不使用 X，理由是..."
- **Artifact 可被 KB 索引，可产生 Memory，但不被 KB/Memory 吃掉。** Artifact 始终保留原件身份。
- **Skill 是"怎么做"，不混进 Memory。** Memory 是"知道什么"，Skill 是"做过什么有效"。
- **Context Engine 是唯一负责把上述内容编排给 LLM 的决策层。**

### 1.3 Information Flow

```
External Paper / Dataset / Documentation
        ↓  download / upload
Artifact (scope=library)
        ↓  parse + chunk + embed
KB: Chunk (paragraph-level, points back to artifact)
        ↓  extraction (lazy or scheduled)
KB: Claim (structured assertions, may conflict)
        ↓  extraction
KB: Concept + Alias (entity resolution)
        ↓  discovery
KB: Relation (concept-claim-artifact graph)
        ↓  fusion (dreaming system)
KB: Synthesis (multi-source review, cited, draft)
        ↓  proposal (if adopted)
Memory (accepted state/judgment)
        ↓  assembly (per-harness recipe)
Context Engine → Agent
        ↓  execution
Artifact (scope=project, survey_report / analysis / code / ...)
        ↓  index back into KB
KB: Chunk (internal knowledge also searchable)
```

---

## 2. Data Model

### 2.1 Artifact (修改现有表)

Artifact 不再只是项目产物，也承担论文/数据集等资料库的角色。

```python
class ArtifactScope(str, Enum):
    PROJECT = "project"       # 项目产出（report, code, figure, plan...）
    LIBRARY = "library"       # 资料库（论文, 数据集, 文档, benchmark...）

class ArtifactType(str, Enum):
    # ── project scope ──
    SURVEY_REPORT = "survey_report"
    ANALYSIS_REPORT = "analysis_report"
    EXPERIMENT_LOG = "experiment_log"
    RESEARCH_PLAN = "research_plan"
    PAPER_DRAFT = "paper_draft"
    PAPER_TEX = "paper_tex"
    PAPER_PDF = "paper_pdf"           # 自己写的论文 PDF
    PAPER_OUTLINE = "paper_outline"
    REVIEW_REPORT = "review_report"
    CODE = "code"
    FIGURE = "figure"
    TABLE = "table"
    REPORT = "report"
    DATA_PROFILE = "data_profile"
    DATA_PIPELINE = "data_pipeline"
    OTHER = "other"

    # ── library scope (new) ──
    PAPER = "paper"                   # 外部论文 PDF
    DATASET = "dataset"               # 数据集
    DOCUMENTATION = "documentation"   # 技术文档
    BENCHMARK = "benchmark"           # benchmark 数据
    REFERENCE_CODE = "reference_code" # 开源实现
    NEWS_ARTICLE = "news_article"     # 新闻/博客
    LAB_UPDATE = "lab_update"         # 实验室官网更新

class Artifact(Base):
    __tablename__ = "artifacts"

    # ── existing fields ──
    id: Mapped[uuid.UUID]
    project_id: Mapped[uuid.UUID | None]    # None for org-level library items
    node_id: Mapped[uuid.UUID | None]       # None for library items / user uploads
    name: Mapped[str]
    type: Mapped[ArtifactType]
    description: Mapped[str | None]
    mime_type: Mapped[str | None]
    current_version: Mapped[int]
    extra_data: Mapped[dict | None]         # JSONB, type-specific
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]

    # ── new fields ──
    scope: Mapped[ArtifactScope] = mapped_column(default=ArtifactScope.PROJECT)
    organization_id: Mapped[uuid.UUID | None]   # for org-level library items

    # 论文/文献特有 metadata (type=paper 时使用)
    paper_metadata: Mapped[dict | None]     # JSONB
    # {
    #   "doi": "10.1038/...",
    #   "authors": ["Alice", "Bob"],
    #   "venue": "NeurIPS 2025",
    #   "publication_date": "2025-12-01",
    #   "abstract": "We propose...",
    #   "quality_tier": "tier1",          # tier1-4
    #   "source_url": "https://arxiv.org/abs/...",
    #   "references_count": 42,
    #   "citation_count": 15
    # }

    # 数据集特有 metadata (type=dataset 时使用)
    dataset_metadata: Mapped[dict | None]   # JSONB
    # {
    #   "size": "1.2M samples",
    #   "format": "csv",
    #   "domain": "molecular dynamics",
    #   "features": ["energy", "forces", "positions"],
    #   "license": "CC-BY-4.0"
    # }

    # KB 索引状态
    is_indexed_in_kb: Mapped[bool] = mapped_column(default=False)
    kb_chunk_count: Mapped[int] = mapped_column(default=0)
    indexed_at: Mapped[datetime | None]

    # 结构化摘要（agent 或 dreaming 生成）
    structured_summary: Mapped[dict | None]     # JSONB
    # {"key_findings": [...], "limitations": [...],
    #  "claims": [...], "methods_used": [...]}

    # 关联的 concept IDs
    concept_ids: Mapped[list[str] | None]       # ARRAY

    # content hash for dedup (across org)
    content_hash: Mapped[str | None]            # SHA-256

    # relationships
    versions: Mapped[list["ArtifactVersion"]]
```

**Indexes**:
- `(project_id, scope, type)` — 按项目+范围+类型查
- `(organization_id, scope)` — org 级 library 查询
- `(content_hash)` — dedup
- `(type)` — 按类型查

**ArtifactVersion** 和 **ArtifactRelation** 保持不变。

### 2.2 KB: KBChunk (修改现有表)

KBChunk 不再指向 KBEntry/KBSource，改为直接指向 Artifact。

```python
class KBChunk(Base):
    __tablename__ = "kb_chunks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)

    # ── changed: point to artifact instead of kb_entry ──
    artifact_id: Mapped[uuid.UUID]              # FK → artifacts
    artifact_version: Mapped[int] = mapped_column(default=1)

    # ── existing ──
    text: Mapped[str]
    chunk_index: Mapped[int]
    section: Mapped[str | None]                 # "Methods", "Results", ...
    page_number: Mapped[int | None]
    figure_ref: Mapped[str | None]
    table_ref: Mapped[str | None]
    embedding: Mapped[Vector]                   # pgvector(1536)
    token_count: Mapped[int | None]
    created_at: Mapped[datetime]

    # ── new ──
    citability: Mapped[str] = mapped_column(default="medium")
    # "high" (methods/results) | "medium" (discussion) | "low" (boilerplate)

    usage_count: Mapped[int] = mapped_column(default=0)
    last_accessed_at: Mapped[datetime | None]
```

**Indexes**:
- `(artifact_id, chunk_index)` — 按 artifact 重建文档顺序
- HNSW on `embedding` — pgvector 近似搜索

**Migration**: `kb_chunks.kb_entry_id` → `kb_chunks.artifact_id`，通过 KBEntry→Artifact 映射表迁移。

### 2.3 KB: KBClaim (新表)

从 chunk 中抽取的可验证断言。

```python
class KBClaimStance(str, Enum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    NEUTRAL = "neutral"
    EXTENDS = "extends"
    QUALIFIES = "qualifies"

class KBClaim(Base):
    __tablename__ = "kb_claims"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID | None]        # None = org scope
    artifact_id: Mapped[uuid.UUID]              # FK → artifacts (来源)
    claim_text: Mapped[str]
    source_chunk_ids: Mapped[list[str]]         # ARRAY — 支撑 chunk IDs

    stance: Mapped[KBClaimStance] = mapped_column(default=KBClaimStance.NEUTRAL)
    confidence: Mapped[str] = mapped_column(default="medium")  # high|medium|low

    conditions: Mapped[dict | None]             # JSONB
    # {"dataset": "MD17", "metric": "MAE", "setting": "zero-shot"}

    concept_ids: Mapped[list[str] | None]       # ARRAY — linked concepts

    extracted_by: Mapped[str] = mapped_column(default="agent")
    # "agent" | "user" | "dreaming"
    extraction_context: Mapped[dict | None]     # JSONB

    is_verified: Mapped[bool] = mapped_column(default=False)
    verified_by: Mapped[str | None]
    verified_at: Mapped[datetime | None]

    status: Mapped[str] = mapped_column(default="active")
    # "active" | "superseded" | "retracted" | "disputed"
    superseded_by_id: Mapped[uuid.UUID | None]
    related_claim_ids: Mapped[list[str] | None]

    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
```

**Indexes**: `(project_id, status)`, `(artifact_id)`, GIN on `concept_ids`

### 2.4 KB: KBConcept (新表)

术语/概念实体。知识图谱的节点。

```python
class KBConceptType(str, Enum):
    METHOD = "method"               # PINN, MACE, DFT
    MODEL = "model"                 # GPT-4, SchNet
    EQUATION = "equation"           # Navier-Stokes, Schrödinger
    DATASET = "dataset"             # MD17, QM9
    BENCHMARK = "benchmark"         # MatBench, OC20
    METRIC = "metric"               # MAE, RMSE
    FIELD = "field"                 # computational chemistry
    TECHNIQUE = "technique"         # transfer learning
    MATERIAL = "material"           # graphene, perovskite
    PHENOMENON = "phenomenon"       # phase transition, turbulence
    SOFTWARE = "software"           # LAMMPS, VASP
    ORGANIZATION = "organization"   # DeepMind, FAIR
    OTHER = "other"

class KBConcept(Base):
    __tablename__ = "kb_concepts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID | None]

    canonical_name: Mapped[str]                 # "Navier-Stokes equations"
    concept_type: Mapped[KBConceptType]
    short_definition: Mapped[str | None]        # 1-2 sentence definition
    living_summary: Mapped[str | None]          # continuously updated by dreaming

    embedding: Mapped[Vector | None]            # pgvector(1536) for disambiguation
    metadata: Mapped[dict | None]               # JSONB, type-specific

    related_concept_ids: Mapped[list[str] | None]  # ARRAY — quick lookup
    source_count: Mapped[int] = mapped_column(default=0)
    claim_count: Mapped[int] = mapped_column(default=0)
    project_usage_count: Mapped[int] = mapped_column(default=0)

    last_refined_at: Mapped[datetime | None]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]

    # relationships
    aliases: Mapped[list["KBConceptAlias"]]
```

### 2.5 KB: KBConceptAlias (新表)

```python
class KBConceptAlias(Base):
    __tablename__ = "kb_concept_aliases"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    concept_id: Mapped[uuid.UUID]               # FK → kb_concepts, CASCADE
    alias: Mapped[str]                          # "N-S equations", "NSE", "纳维斯托克斯方程"
    language: Mapped[str] = mapped_column(default="en")
    is_abbreviation: Mapped[bool] = mapped_column(default=False)
```

**Indexes**: `lower(alias)` for case-insensitive lookup, unique `(concept_id, alias)`

### 2.6 KB: KBRelation (新表)

```python
class KBRelationType(str, Enum):
    # concept ↔ concept
    RELATED_TO = "related_to"
    IS_A = "is_a"
    PART_OF = "part_of"
    IMPROVES_OVER = "improves_over"
    ALTERNATIVE_TO = "alternative_to"
    USED_IN = "used_in"
    # claim ↔ claim
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    EXTENDS = "extends"
    REFINES = "refines"
    SUPERSEDES = "supersedes"
    # cross-type
    EVALUATED_ON = "evaluated_on"
    PROPOSES = "proposes"
    MENTIONS = "mentions"
    CITES = "cites"

class KBRelation(Base):
    __tablename__ = "kb_relations"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID | None]

    subject_type: Mapped[str]       # "concept" | "claim" | "artifact"
    subject_id: Mapped[uuid.UUID]
    relation_type: Mapped[KBRelationType]
    object_type: Mapped[str]
    object_id: Mapped[uuid.UUID]

    confidence: Mapped[float] = mapped_column(default=0.5)
    evidence_ids: Mapped[list[str] | None]      # ARRAY
    metadata: Mapped[dict | None]               # JSONB
    # {"benchmark_score": 0.95, "metric": "MAE", "dataset": "MD17"}

    discovered_by: Mapped[str] = mapped_column(default="agent")
    status: Mapped[str] = mapped_column(default="active")
    created_at: Mapped[datetime]
```

**Indexes**: `(subject_type, subject_id)`, `(object_type, object_id)`, `(relation_type)`, `(project_id, relation_type)`

### 2.7 KB: KBSynthesis (新表)

多来源融合的综述性知识。做梦系统的核心产出。

```python
class KBSynthesis(Base):
    __tablename__ = "kb_syntheses"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID | None]
    organization_id: Mapped[uuid.UUID | None]

    title: Mapped[str]
    content: Mapped[str]                        # Markdown
    synthesis_type: Mapped[str]
    # "topic_review" | "daily_digest" | "weekly_digest" |
    # "conflict_report" | "gap_analysis" | "trend_analysis"

    source_artifact_ids: Mapped[list[str]]      # ARRAY — artifacts used
    claim_ids: Mapped[list[str] | None]         # ARRAY
    concept_ids: Mapped[list[str] | None]       # ARRAY

    key_findings: Mapped[list[dict] | None]     # JSONB
    conflicts_detected: Mapped[list[dict] | None]
    gaps_identified: Mapped[list[str] | None]

    confidence: Mapped[str] = mapped_column(default="medium")
    quality_score: Mapped[float | None]

    generated_by: Mapped[str] = mapped_column(default="dreaming")
    model_used: Mapped[str | None]
    token_cost: Mapped[int | None]

    status: Mapped[str] = mapped_column(default="draft")
    # "draft" | "reviewed" | "published" | "outdated"
    reviewed_by: Mapped[str | None]
    reviewed_at: Mapped[datetime | None]
    valid_until: Mapped[datetime | None]

    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
```

### 2.8 MemoryProposal (新表)

```python
class ProposalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    AUTO_APPROVED = "auto_approved"
    MERGED = "merged"

class MemoryProposal(Base):
    __tablename__ = "memory_proposals"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID | None]
    user_id: Mapped[uuid.UUID | None]

    proposed_content: Mapped[str]
    proposed_layer: Mapped[str]                 # MemoryLayer value
    proposed_type: Mapped[str]                  # MemoryType value
    proposed_confidence: Mapped[str]            # ConfidenceLevel value
    proposed_topic: Mapped[str | None]

    source: Mapped[dict]                        # JSONB — provenance
    reasoning: Mapped[str | None]

    conflicts_with: Mapped[list[str] | None]    # ARRAY — conflicting memory IDs
    supersedes: Mapped[list[str] | None]        # ARRAY — to-replace memory IDs

    status: Mapped[ProposalStatus] = mapped_column(default=ProposalStatus.PENDING)
    decided_by: Mapped[str | None]
    decision_reason: Mapped[str | None]
    decided_at: Mapped[datetime | None]
    created_memory_id: Mapped[uuid.UUID | None] # FK → memory_entries

    created_at: Mapped[datetime]
```

**Auto-approval rules**:

| Layer | Type | Source | Auto-approve |
|-------|------|--------|-------------|
| session | * | * | Yes |
| project | preference / rule | HITL conversation | Yes |
| project | factual | agent + high confidence + has evidence | Yes |
| project | judgmental / decision | * | No → proposal |
| organization | * | * | No → human approve |

### 2.9 MemoryEntry (修改现有表，新增字段)

```python
class MemoryEntry(Base):
    # ── existing fields unchanged ──
    # id, layer, project_id, user_id, session_id, content, type,
    # confidence, status, tags, topic, source, created_at, updated_at,
    # last_accessed_at, last_verified_at, superseded_by_id,
    # conflicts_with_ids, token_count

    # ── new fields ──
    proposal_id: Mapped[uuid.UUID | None]       # FK → memory_proposals
    evidence_ids: Mapped[list[str] | None]      # ARRAY — chunk/claim/artifact IDs
    concept_ids: Mapped[list[str] | None]       # ARRAY — linked KBConcept IDs
    derived_from_artifact_id: Mapped[uuid.UUID | None]  # FK → artifacts
    embedding: Mapped[Vector | None]            # pgvector(1536) for semantic retrieval
    verification_count: Mapped[int] = mapped_column(default=0)
```

### 2.10 ResearchSkill (修改现有表，新增字段)

```python
class ResearchSkill(Base):
    # ── existing fields unchanged ──
    # id, project_id, name, description, node_types, steps,
    # tools_required, parameters, preconditions, expected_outcome,
    # pitfalls, extracted_from_node_id, extracted_from_execution,
    # usage_count, last_used_at, success_rate, is_validated,
    # is_deprecated, scope, version, patch_history, token_count,
    # created_at, updated_at

    # ── new fields ──
    extracted_from_projects: Mapped[list[str] | None]   # ARRAY
    validated_run_count: Mapped[int] = mapped_column(default=0)
    failure_cases: Mapped[list[dict] | None]            # JSONB
    promotion_status: Mapped[str] = mapped_column(default="project")
    # "project" | "promotion_pending" | "organization"
    promotion_requested_at: Mapped[datetime | None]
    promotion_approved_by: Mapped[str | None]
```

### 2.11 Watchlist (新表)

```python
class Watchlist(Base):
    __tablename__ = "watchlists"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID | None]
    project_id: Mapped[uuid.UUID | None]
    source_type: Mapped[str]
    # "arxiv" | "semantic_scholar" | "lab_website" | "github" | "rss"
    config: Mapped[dict]                        # JSONB, source-type specific
    is_active: Mapped[bool] = mapped_column(default=True)
    last_checked_at: Mapped[datetime | None]
    last_result_count: Mapped[int | None]
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
```

### 2.12 ToolAuditLog (新表)

```python
class ToolAuditLog(Base):
    __tablename__ = "tool_audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID]
    node_id: Mapped[str | None]
    tool_name: Mapped[str]
    called_by: Mapped[str]              # "agent" | "user" | "system"
    input_summary: Mapped[str | None]
    result_summary: Mapped[str | None]
    permission_decision: Mapped[str]    # "allowed" | "denied" | "confirmed"
    risk_level: Mapped[str]
    side_effect: Mapped[str]
    execution_ms: Mapped[int | None]
    error: Mapped[str | None]
    created_at: Mapped[datetime]
```

### 2.13 Schema Summary

**Deleted**: `kb_entries` (replaced by Artifact with scope=library)

**New tables** (7): `kb_claims`, `kb_concepts`, `kb_concept_aliases`, `kb_relations`, `kb_syntheses`, `memory_proposals`, `watchlists`, `tool_audit_logs`

**Modified tables** (4): `artifacts` (scope, org_id, paper_metadata, etc.), `kb_chunks` (artifact_id replaces kb_entry_id), `memory_entries` (embedding, proposal_id, etc.), `research_skills` (promotion fields)

**Unchanged tables**: `users`, `projects`, `project_configs`, `nodes`, `edges`, `branches`, `graph_snapshots`, `artifact_versions`, `artifact_relations`, `evidence_chains`, `claims`, `reflection_results`, `conversations`, `budgets`, `consumption_records`

---

## 3. Context Engine

### 3.1 Problem

Current Context Engine treats all node types identically: top 50 memory by confidence, 10 KB chunks by vector similarity, done. This ignores that:

- Survey needs broad KB coverage
- Experiment needs failure memories and method constraints
- Writing needs evidence chains and artifacts
- Planning needs project decisions and survey outputs

### 3.2 Context Recipe

Each harness type gets its own recipe: which slots to load, how much budget to allocate, and what retrieval strategy to use.

```python
@dataclass
class ContextRecipe:
    slot_budgets: dict[str, float]      # fraction of flexible token budget per slot
    kb_strategy: KBRetrievalStrategy
    memory_strategy: MemoryRetrievalStrategy

@dataclass
class KBRetrievalStrategy:
    max_chunks: int = 10
    quality_min: str | None = None      # "tier1" | "tier2" | "tier3"
    prefer_recent: bool = False
    include_claims: bool = False
    include_synthesis: bool = False
    include_conflicting: bool = False   # analysis needs opposing views
    focus_concepts: bool = False        # boost chunks matching task concepts
    breadth_search: bool = False        # exploration needs breadth
    prefer_cited: bool = False          # writing prefers already-cited chunks

@dataclass
class MemoryRetrievalStrategy:
    query_mode: str = "semantic"        # "semantic" | "recency+relevance" | "simple"
    max_entries: int = 20
    prefer_types: list[str] | None = None
    include_failure_memories: bool = False
```

**Recipes per node type**:

| Node Type | Top Slots (by budget share) | KB Strategy | Memory Strategy |
|-----------|---------------------------|-------------|-----------------|
| survey | kb(35%), memory(20%), skill(10%), org(10%) | 20 chunks, tier3+, recent, breadth, +claims +synthesis | semantic, factual+rule |
| experiment | memory(25%), artifact(25%), skill(15%), kb(15%) | 10 chunks, tier2+, focus_concepts, +claims | semantic, +failure memories |
| writing | artifact(30%), evidence(20%), memory(15%), user(10%) | 10 chunks, prefer_cited | semantic, factual+preference+decision |
| analysis | artifact(30%), memory(25%), skill(15%), kb(15%) | 15 chunks, +claims, +conflicting | semantic, factual+judgmental |
| planning | memory(30%), artifact(20%), org(10%), kb(15%) | 10 chunks, +synthesis | semantic, factual+decision+rule |
| exploration | kb(40%), org(15%), memory(15%), skill(10%) | 20 chunks, breadth, +synthesis +claims | semantic |
| project_chat | memory(25%), graph(20%), artifact(15%), session(15%) | 5 chunks, on-demand | recency+relevance |

### 3.3 Hybrid KB Retrieval

Replace pure vector search with multi-signal hybrid:

```python
class HybridKBRetriever:
    """
    1. Vector similarity — pgvector cosine distance
    2. Full-text search — PostgreSQL tsvector (Chinese: pg_jieba/zhparser)
    3. Concept match — query mentions known KBConcept → boost related chunks
    4. Recency boost — newer source artifacts score higher
    5. Quality boost — higher tier → higher score
    6. Citation boost — more usage_count → higher score
    """

    async def search(self, query, *, project_id, strategy, concept_ids=None):
        # 1. Vector search — top 50 candidates
        vector_results = await self._vector_search(query, project_id, limit=50)
        # 2. FTS search — top 50 candidates
        fts_results = await self._fts_search(query, project_id, limit=50)
        # 3. Merge (RRF or linear combination)
        merged = self._reciprocal_rank_fusion(vector_results, fts_results)
        # 4. Apply boosts
        for r in merged:
            r.score *= self._quality_boost(r.quality_tier)
            r.score *= self._recency_boost(r.publication_date)
            r.score *= self._citation_boost(r.usage_count)
            if concept_ids:
                r.score *= self._concept_boost(r, concept_ids)
        # 5. Sort, limit, return
        merged.sort(key=lambda r: r.score, reverse=True)
        return merged[:strategy.max_chunks]
```

### 3.4 Semantic Memory Retrieval

Replace `top 50 by confidence` with semantic search on memory content (via embedding):

```python
class MemoryRetriever:
    """Hybrid: embedding similarity + confidence + recency + type match."""

    async def retrieve(self, query, *, project_id, user_id, strategy, layers):
        results = []
        for layer in layers:
            layer_results = await self._search_layer(query, project_id, user_id, layer, strategy)
            results.extend(layer_results)
        # Update last_accessed_at
        await self._update_access_times([r.id for r in results])
        return results
```

### 3.5 Assembly Flow

```python
class ContextEngine:
    async def assemble(self, config, slots, *, db, project_id, node_type, user_id):
        recipe = CONTEXT_RECIPES.get(node_type, CONTEXT_RECIPES["project_chat"])
        required_tokens = sum(s.token_count for s in slots if s.is_required)
        flexible_budget = config.max_tokens - required_tokens

        # Load each slot with its budget allocation
        for slot_name, budget_frac in recipe.slot_budgets.items():
            budget = int(flexible_budget * budget_frac)
            loader = self._get_loader(slot_name)
            slot = await self._safe_load(db, loader(db, project_id, ..., budget=budget))
            if slot:
                db_slots.append(slot)

        # New loaders: _load_org_memory, _load_user_memory, _load_evidence_chains
        # All run in SAVEPOINT (existing pattern)
```

---

## 4. Tool Permission System

### 4.1 Multi-dimensional Permissions

```python
@dataclass
class ToolPermission:
    risk_level: ToolRiskLevel               # LOW | MEDIUM | HIGH
    allowed_node_types: list[str] | None    # None = all
    side_effect: str                        # read | write_project | write_org | external_call | compute | delete
    data_scope: str                         # node | project | organization | external
    requires_human_approval: bool = False
    allowed_autonomous: bool = True
    budget_type: str | None = None          # api_calls | compute | llm_tokens | storage
    audit_required: bool = False
```

### 4.2 Four-layer Policy Stack

```
System Policy (hardcoded: NEVER_AUTONOMOUS list)
  → Organization Policy (org admin blocked_tools)
    → Project Policy (ProjectConfig.tool_whitelist)
      → Harness Policy (YAML tools list + node_type)
        → Runtime (autonomous mode + budget remaining)
```

### 4.3 Permission Matrix

| Tool | risk | side_effect | scope | autonomous | audit |
|------|------|-------------|-------|------------|-------|
| kb_search | LOW | read | project | yes | no |
| read_artifact, list_artifacts | LOW | read | project | yes | no |
| query_budget | LOW | read | project | yes | no |
| semantic_scholar_search, arxiv_search | LOW | external_call | external | yes | no |
| search_concepts, get_concept_detail | LOW | read | project | yes | no |
| find_related_claims | LOW | read | project | yes | no |
| save_artifact | MEDIUM | write_project | project | yes | yes |
| save_memory | MEDIUM | write_project | project | yes | yes |
| kb_ingest | MEDIUM | write_project | project | yes | yes |
| create_evidence_chain | MEDIUM | write_project | project | yes | yes |
| create_claim, create_relation | MEDIUM | write_project | project | yes | yes |
| propose_memory | MEDIUM | write_project | project | yes | yes |
| execute_python | MEDIUM | compute | node | yes | yes |
| compile_latex | MEDIUM | compute | node | yes | yes |
| request_human_input | MEDIUM | — | — | no | no |
| promote_to_org_memory | HIGH | write_org | org | no | yes |
| promote_to_org_kb | HIGH | write_org | org | no | yes |
| delete_node, delete_artifact | HIGH | delete | project | no | yes |

---

## 5. Concept Linking System

### 5.1 Flow

```
LLM output text
    ↓ ConceptLinker.annotate()
1. Load alias→concept index (Aho-Corasick automaton, cached per-org, 10min TTL)
2. Multi-pattern match on lowercased text — O(n) scan
3. Word boundary check (avoid matching substrings)
4. Resolve overlapping spans (keep longest)
5. Return annotation list
    ↓
ChatResponse.concept_annotations = [
  {"text": "Navier-Stokes", "start": 42, "end": 56, "concept_id": "uuid", "concept_type": "equation"},
  {"text": "MACE", "start": 120, "end": 124, "concept_id": "uuid", "concept_type": "method"},
]
    ↓
Frontend: <ConceptLink> with hover card + click-to-concept-page
```

### 5.2 Why Aho-Corasick

- O(n) text scan regardless of pattern count (vs O(n*m) for naive matching)
- 1000 aliases, 2000-char text → microseconds
- LLM-based NER would be 500ms+ per chat message, unacceptable latency

### 5.3 Concept Page

```
GET /api/v1/kb/concepts/{id}
→ {
    canonical_name, concept_type, short_definition, living_summary,
    aliases: [...],
    related_concepts: [{id, name, relation_type}],
    key_sources: [{artifact_id, title, tier}],
    recent_claims: [{claim_text, stance, confidence}],
    project_usages: [{project_id, context}],
    memory_links: [{memory_id, content}],
    last_refined_at
  }
```

The concept page is **living** — dreaming system periodically updates `living_summary`, new papers update `key_sources`, internal experiments update `project_usages`.

### 5.4 ChatResponse Extension

```python
class ConceptAnnotation(BaseModel):
    text: str           # matched text in output
    start: int
    end: int
    concept_id: str
    concept_type: str   # method | equation | dataset | ...

class ChatResponse(BaseModel):
    reply: str
    executed_actions: list[ExecutedAction] | None = None
    concept_annotations: list[ConceptAnnotation] | None = None
```

---

## 6. Dreaming System

### 6.1 Architecture

A set of orchestrated background jobs, not one monolithic cron.

```
DreamingScheduler
├── DailyCollector         — collect external updates (arxiv, labs, github...)
├── RelevanceFilter        — filter by project/org topic similarity
├── KBIngester             — parse → chunk → embed → dedup → quality grade
├── ConceptExtractor       — extract concepts + aliases from new sources
├── ClaimExtractor         — extract claims from high-quality sources
├── RelationDiscoverer     — discover relations between claims/concepts
├── SynthesisGenerator     — generate topic reviews / conflict reports
├── MemoryAuditor          — check new evidence vs existing memory
├── SkillExtractor         — extract skills from successful node executions
└── ConceptRefiner         — update concept definitions and living_summary
```

### 6.2 Job Definitions

```python
@dataclass
class DreamingJob:
    name: str
    schedule: str               # cron expression
    priority: int               # lower = higher
    max_budget_per_run: float   # USD
    max_duration_seconds: int
    requires_idle: bool         # only run when no active node execution
    scope: str                  # "project" | "organization"
```

| Job | Schedule | Budget | Idle? | Scope |
|-----|----------|--------|-------|-------|
| daily_collector | 0 6 * * * | $1 | no | org |
| concept_extractor | 0 7 * * * | $2 | yes | org |
| claim_extractor | 0 8 * * * | $3 | yes | project |
| relation_discoverer | 0 9 * * * | $2 | yes | project |
| synthesis_generator | 0 22 * * 0 (weekly) | $5 | yes | project |
| memory_auditor | 0 3 * * * | $1 | yes | project |
| concept_refiner | 0 4 * * 1 (weekly) | $2 | yes | org |
| skill_extractor | */30 * * * * | $1 | no | project |

### 6.3 Safety Constraint

Dreaming system **cannot write facts directly**. It can only produce:

| Output | Target | Requires Approval |
|--------|--------|-------------------|
| Artifact (paper/dataset download) | artifacts | No (just a file) |
| KBChunk (from parsing artifact) | kb_chunks | No (just indexing) |
| KBConcept + Alias | kb_concepts/aliases | No (auto-create, can correct later) |
| KBClaim | kb_claims | No (marked extracted_by="dreaming", unverified) |
| KBRelation | kb_relations | No (has confidence; low confidence → status="pending") |
| KBSynthesis | kb_syntheses | No (status="draft", needs review) |
| MemoryProposal | memory_proposals | **Yes** (goes through proposal flow) |
| ResearchSkill | research_skills | **Yes** (is_validated=False, needs verification) |
| Digest artifact | artifacts | No (just a report) |

### 6.4 Daily Collector

```python
class DailyCollector:
    async def run(self, organization_id):
        watchlist = await self._load_watchlist(organization_id)
        # [{"type": "arxiv", "query": "MLIP robustness", "max_results": 20},
        #  {"type": "semantic_scholar", "query": "foundation model materials science"},
        #  {"type": "github", "repo": "ACEsuit/mace", "event": "release"}]

        raw_updates = []
        for item in watchlist:
            results = await self._get_collector(item["type"]).fetch(item)
            raw_updates.extend(results)

        new_updates = await self._dedup_against_existing(raw_updates)

        for update in new_updates:
            # Create library artifact (paper/news/lab_update)
            artifact = await self._create_library_artifact(update)
            # Queue for KB indexing
            await self._queue_indexing(artifact.id)

        return {"collected": len(raw_updates), "new": len(new_updates)}
```

### 6.5 Memory Auditor

```python
class MemoryAuditor:
    async def run(self, project_id):
        new_claims = await self._get_recent_claims(project_id)  # since last audit
        memories = await self._get_active_memories(project_id)

        for memory in memories:
            related_claims = await self._find_related_claims(memory, new_claims)
            if not related_claims:
                continue
            assessment = await self._assess_impact(memory, related_claims)
            # LLM: "Does this new evidence support, challenge, or supersede this memory?"
            if assessment.action == "confirm":
                await self._update_verification(memory.id)
            elif assessment.action == "challenge":
                await self._create_conflict_proposal(assessment)
            elif assessment.action == "supersede":
                await self._create_supersede_proposal(assessment)
```

### 6.6 Claim Extractor

Two trigger modes:
1. **Lazy**: agent references a chunk during execution → extract claims for that chunk on demand
2. **Scheduled**: dreaming processes recent tier1/tier2 artifacts (max 10 per run, cost-controlled)

```python
class ClaimExtractor:
    async def extract_on_demand(self, chunk_id):
        """Triggered when agent references a chunk."""
        existing = await self._get_claims_for_chunk(chunk_id)
        if existing:
            return existing
        chunk = await self._load_chunk(chunk_id)
        claims = await self._llm_extract_claims([chunk])
        await self._save_claims(claims)
        return claims

    async def run(self, project_id):
        """Scheduled: process high-quality unprocessed artifacts."""
        artifacts = await self._get_high_quality_unprocessed(project_id)
        for artifact in artifacts[:10]:
            chunks = await self._get_chunks(artifact.id)
            for section_group in self._group_by_section(chunks):
                claims = await self._llm_extract_claims(section_group)
                await self._save_claims(claims)
```

### 6.7 Skill Extractor

Triggered every 30 minutes, checks for recently completed nodes.

Extraction criteria:
- Node completed successfully
- Used 5+ tool calls
- Reflection score > 0.7 or no critical findings

```python
class SkillExtractor:
    async def run(self, project_id):
        nodes = await self._get_completed_unprocessed(project_id)
        for node in nodes:
            if not self._should_extract(node):
                continue
            transcript = await self._load_transcript(node.id)
            skill = await self._llm_extract_skill(node, transcript)
            if skill:
                similar = await self._find_similar_skills(skill, project_id)
                if similar:
                    await self._merge_or_update(similar, skill)
                else:
                    await self._save_skill(skill, node.id, project_id)
```

---

## 7. Artifact → KB Indexing Pipeline

When an artifact is created or marked as final, it can be indexed into KB:

```python
class ArtifactKBIndexer:
    INDEXABLE_TYPES = {
        # library
        "paper", "documentation", "benchmark",
        # project
        "survey_report", "analysis_report", "report",
        "experiment_log", "research_plan", "review_report",
    }

    async def index(self, artifact_id):
        artifact = await self._load_artifact(artifact_id)
        if artifact.type.value not in self.INDEXABLE_TYPES:
            return
        if artifact.is_indexed_in_kb:
            return await self._reindex(artifact)  # new version

        content = await self._get_content(artifact)
        chunks = self._chunk_text(content, max_tokens=512)
        embeddings = await generate_embeddings([c.text for c in chunks])

        for chunk, emb in zip(chunks, embeddings):
            await self._save_chunk(KBChunk(
                artifact_id=artifact.id,
                artifact_version=artifact.current_version,
                text=chunk.text,
                section=chunk.section,
                page_number=chunk.page,
                embedding=emb,
                token_count=chunk.token_count,
            ))

        artifact.is_indexed_in_kb = True
        artifact.kb_chunk_count = len(chunks)
        artifact.indexed_at = datetime.now(UTC)
```

**Trigger points**:
- Node completion → auto-index output artifacts (survey_report, analysis_report, etc.)
- Paper ingest → auto-index after download
- Manual → user clicks "Index to KB" in artifact page
- Dreaming → periodic check for unindexed final artifacts

---

## 8. Evidence Chain Integration

Current system has two "Claim" concepts — clarify the distinction:

| | `evidence_chains.claims` (existing) | `kb_claims` (new) |
|---|---|---|
| Table | `claims` | `kb_claims` |
| Role | Single evidence piece in a chain | Standalone verifiable assertion |
| Source | Agent creates during execution | Extracted from artifact/chunk |
| Lifecycle | Bound to evidence chain | Independent, reusable |
| References | `kb_chunk_id`, `artifact_id` | `artifact_id`, `source_chunk_ids` |

Future: add `kb_claim_id` FK to `claims` table, so evidence chains can directly reference structured KB claims.

Complete evidence tracing:

```
Memory ("不用 method X")
  → EvidenceChain
    → Claim (evidence_type=supports)
      → KBChunk (section="Results", page=7)
        → Artifact (paper PDF, "Evaluating MLIP Robustness.pdf")
```

Every hop is a FK lookup. User clicks through: memory → evidence → source paragraph → original paper.

---

## 9. API Endpoints

### 9.1 Library (Artifact scope=library)

```
GET    /api/v1/library                          # list library items (paginated, filter by type)
POST   /api/v1/library/upload                   # upload paper/dataset/doc
POST   /api/v1/library/ingest-doi               # ingest by DOI (auto-download + parse)
GET    /api/v1/library/{id}                     # artifact detail
POST   /api/v1/library/{id}/index               # manually trigger KB indexing
DELETE /api/v1/library/{id}                     # remove from library
```

### 9.2 KB: Concepts

```
GET    /api/v1/kb/concepts                      # list/search concepts
GET    /api/v1/kb/concepts/{id}                 # concept detail (living page)
POST   /api/v1/kb/concepts                      # manually create
PATCH  /api/v1/kb/concepts/{id}                 # update
GET    /api/v1/kb/concepts/{id}/sources         # artifacts mentioning this concept
GET    /api/v1/kb/concepts/{id}/claims          # claims involving this concept
GET    /api/v1/kb/concepts/{id}/relations       # relation graph
```

### 9.3 KB: Claims

```
GET    /api/v1/kb/claims                        # list (filter by artifact, concept, stance)
GET    /api/v1/kb/claims/{id}                   # detail
POST   /api/v1/kb/claims/{id}/verify            # mark verified
```

### 9.4 KB: Syntheses

```
GET    /api/v1/kb/syntheses                     # list
GET    /api/v1/kb/syntheses/{id}                # detail
POST   /api/v1/kb/syntheses/{id}/review         # mark reviewed
```

### 9.5 Memory Proposals

```
GET    /api/v1/memory/proposals                 # list pending
POST   /api/v1/memory/proposals/{id}/approve
POST   /api/v1/memory/proposals/{id}/reject
POST   /api/v1/memory/proposals/{id}/merge      # merge into existing memory
```

### 9.6 Dreaming

```
GET    /api/v1/dreaming/status                  # job statuses
POST   /api/v1/dreaming/trigger/{job_name}      # manual trigger
GET    /api/v1/dreaming/history                 # execution history
PATCH  /api/v1/dreaming/config                  # modify schedule/budget
```

### 9.7 Watchlist

```
GET    /api/v1/watchlists
POST   /api/v1/watchlists
PATCH  /api/v1/watchlists/{id}
DELETE /api/v1/watchlists/{id}
POST   /api/v1/watchlists/{id}/check-now
```

---

## 10. Agent Tools (New)

### 10.1 Knowledge Graph Tools

| Tool | Description | Risk | Nodes |
|------|-------------|------|-------|
| `search_concepts` | Search concepts by name/description | LOW | all |
| `get_concept_detail` | Get concept page data | LOW | all |
| `find_related_claims` | Find claims by topic/concept/stance | LOW | all |
| `create_claim` | Record a verifiable claim from KB | MEDIUM | all |
| `create_relation` | Record a relation between concepts/claims | MEDIUM | all |

### 10.2 Memory Proposal Tool

| Tool | Description | Risk | Nodes |
|------|-------------|------|-------|
| `propose_memory` | Propose memory for human review (judgmental/decision) | MEDIUM | all |

Existing `save_memory` behavior changes:
- `layer=session` → direct write
- `layer=project, type=preference/rule, source=HITL` → direct write
- `layer=project, type=factual, confidence=high, has evidence` → direct write
- All other cases → auto-convert to proposal

---

## 11. Data Flow Diagrams

### 11.1 External Knowledge → Platform

```
arXiv / Semantic Scholar / Lab Website / GitHub / RSS
    ↓  DailyCollector (watchlist-driven)
Raw updates (title, abstract, URL, DOI)
    ↓  RelevanceFilter (embedding similarity to project topics)
Relevant updates
    ↓  Download / fetch
Artifact (scope=library, type=paper/news/lab_update)
    ↓  ArtifactKBIndexer (parse → chunk → embed)
KBChunks (point to artifact)
    ↓  ConceptExtractor (LLM)
KBConcepts + Aliases
    ↓  ClaimExtractor (LLM, tier1/2 only)
KBClaims
    ↓  RelationDiscoverer
KBRelations
    ↓  SynthesisGenerator (weekly)
KBSynthesis (draft)
    ↓  MemoryAuditor
MemoryProposals (if new evidence affects existing memory)
```

### 11.2 Internal Experiment → Platform

```
Node execution completes
    ↓  HarnessExecutor
Artifact (scope=project, type=survey_report/analysis_report/experiment_log/...)
    ↓  MemoryExtractor (rule-based, existing)
MemoryEntries (auto-approve for factual/high confidence)
    ↓  ArtifactKBIndexer
KBChunks (internal knowledge, searchable)
    ↓  ClaimExtractor (lazy, on retrieval)
KBClaims
    ↓  SkillExtractor (if 5+ tools, good quality)
ResearchSkill (is_validated=False)
```

### 11.3 Context Assembly

```
Node starts → HarnessExecutor → load NodeHarness
    ↓
ContextEngine.assemble(recipe = CONTEXT_RECIPES[node_type])
    ├── System Prompt + Rules + Guidelines (harness YAML) ── priority 0, required
    ├── Task / Handoff / Research Intent ────────────────── priority 1-2, required
    ├── Project Memory ──── MemoryRetriever(semantic, layers=[PROJECT])
    ├── KB Chunks ────────── HybridKBRetriever(vector + FTS + boosts)
    │   └── optionally includes KBClaims if strategy.include_claims
    ├── Research Skills ──── filtered by node_type, progressive loading
    ├── Org Memory ────────── MemoryRetriever(layers=[ORGANIZATION])
    ├── User Memory ───────── MemoryRetriever(layers=[USER])
    ├── Evidence Chains ───── for writing/analysis nodes
    └── Session Context ───── recent conversation
    ↓
Token budget enforcement (trim lowest priority if over limit)
    ↓
AgentLoop.run() → tool calls → outputs
    ↓
Post-execution: memory extraction, artifact KB indexing, skill extraction
```

### 11.4 Evidence Tracing (User-facing)

```
User sees Memory: "本项目不再使用 method X 作为主 baseline"
    → click: show evidence
EvidenceChain: claim="method X 在高 Re 场景不稳定", status=supported
    → click: show source
KBClaim: "method X shows 40% MAE increase at Re>10000", from chunk
    → click: show paragraph
KBChunk: section="Results", page=7, text="In our experiments..."
    → click: open paper
Artifact: "Evaluating MLIP Robustness under Distribution Shift.pdf"
```

---

## 12. Frontend Components

### 12.1 Library Page

```
/library
├── Tabs: Papers | Datasets | Documentation | All
├── Search bar (FTS + filters: quality_tier, venue, year, domain)
├── Upload button (PDF / dataset / URL)
├── Ingest by DOI button
├── Each item shows: title, authors, tier badge, indexed badge, chunk count
└── Click → artifact detail + KB chunks + claims extracted
```

### 12.2 Concept Link in Chat

```tsx
<AnnotatedText text={reply} annotations={concept_annotations}
  renderAnnotation={(ann) => (
    <HoverCard>
      <HoverCardTrigger>
        <a href={`/kb/concepts/${ann.concept_id}`} className="concept-link">
          {ann.text}
        </a>
      </HoverCardTrigger>
      <HoverCardContent>
        <h4>{concept.canonical_name}</h4>
        <Badge>{concept.concept_type}</Badge>
        <p>{concept.short_definition}</p>
        <div>{concept.source_count} sources · {concept.claim_count} claims</div>
      </HoverCardContent>
    </HoverCard>
  )}
/>
```

### 12.3 Concept Page

```
/kb/concepts/{id}
├── Header: canonical_name, type, aliases (editable)
├── Definition: short_definition + living_summary
├── Related Concepts: mini graph visualization
├── Key Sources: table (artifact title, tier, date)
├── Claims: list with stance indicators (supports ✓ / contradicts ✗)
├── Project Usages: which projects reference this concept
├── Memory Links: related project memories
├── Recent Updates: from daily collector
└── Revision History: when concept was last refined
```

### 12.4 Memory Proposal Dashboard

```
/project/{id}/memory/proposals
├── Pending: list with approve/reject/merge buttons
│   └── Each shows: content, reasoning, conflicts_with, source node
├── Approved: recently approved
├── Rejected: recently rejected
└── Filters: layer, type, source
```

### 12.5 Knowledge Graph Explorer

```
/project/{id}/kb/graph
├── Force-directed graph: KBConcept nodes (colored by type), KBRelation edges
├── Click node → sidebar detail
├── Filter: concept_type, relation_type, confidence threshold
├── Search: find concept in graph
└── Cluster view: auto-grouped by topic
```

### 12.6 Knowledge Quality Dashboard

```
/project/{id}/kb/overview
├── Stats: sources / chunks / claims / concepts / relations count
├── Verification: verified vs unverified claims ratio
├── Memory health: active / stale / conflicted counts
├── Dreaming: last runs, findings, proposals generated
├── Coverage: concepts with full definition vs name-only
└── Growth: weekly ingest rate chart
```

---

## 13. Migration Plan

### Phase 0: Schema

1. Add `scope`, `organization_id`, `paper_metadata`, `dataset_metadata`, `is_indexed_in_kb`, `kb_chunk_count`, `indexed_at`, `structured_summary`, `concept_ids`, `content_hash` to `artifacts`
2. Migrate `kb_entries` data → `artifacts` (scope=library, type=paper)
3. Rename `kb_chunks.kb_entry_id` → `kb_chunks.artifact_id`, update FK
4. Drop `kb_entries` table
5. Create: `kb_claims`, `kb_concepts`, `kb_concept_aliases`, `kb_relations`, `kb_syntheses`, `memory_proposals`, `watchlists`, `tool_audit_logs`
6. Add `embedding`, `proposal_id`, `evidence_ids`, `concept_ids`, `derived_from_artifact_id`, `verification_count` to `memory_entries`
7. Add `extracted_from_projects`, `validated_run_count`, `failure_cases`, `promotion_status` to `research_skills`
8. Add FTS index on `kb_chunks.text`, HNSW index on `kb_concepts.embedding`

### Phase 1: Context Engine

Goal: existing memory/KB actually works per-harness.

1. Implement `ContextRecipe` + `CONTEXT_RECIPES`
2. Add `embedding` column to `memory_entries`, backfill
3. Implement `MemoryRetriever` (semantic search)
4. Implement `HybridKBRetriever` (vector + FTS)
5. New loaders: `_load_org_memory`, `_load_user_memory`, `_load_evidence_chains`
6. Update `last_accessed_at` on retrieval

### Phase 2: KBConcept + Concept Linking

Goal: terms in chat become clickable knowledge links.

1. `KBConcept` + `KBConceptAlias` models
2. `ConceptExtractor` — batch extract from existing artifacts
3. `ConceptLinker` — Aho-Corasick based
4. Concept page API
5. `ChatResponse.concept_annotations`
6. Frontend `<ConceptLink>` + hover card + concept page

### Phase 3: KBClaim + Knowledge Graph

Goal: structured knowledge layer.

1. `KBClaim` model + lazy extraction
2. `KBRelation` model
3. `ClaimExtractor` (dreaming job)
4. `RelationDiscoverer` (dreaming job)
5. Agent tools: `find_related_claims`, `create_claim`, `create_relation`
6. Link `evidence_chains.claims` → `kb_claims`

### Phase 4: Memory Proposal System

Goal: safe memory writing with approval flow.

1. `MemoryProposal` model
2. Modify `save_memory` tool: auto-approve rules
3. `propose_memory` tool
4. Proposal API + frontend dashboard

### Phase 5: Dreaming System

Goal: background knowledge refinement.

1. `DreamingScheduler` framework
2. `DailyCollector` + `Watchlist`
3. `ConceptRefiner`
4. `MemoryAuditor`
5. `SkillExtractor`

### Phase 6: Synthesis + Advanced

Goal: knowledge fusion and full observability.

1. `KBSynthesis` + `SynthesisGenerator`
2. Library page (frontend)
3. Knowledge graph explorer
4. Knowledge quality dashboard
5. Tool permission system upgrade + audit log
6. Artifact → KB auto-indexing on node completion

---

## 14. Key Design Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | **Artifact stores files, KB stores structured knowledge** | Clean separation: Artifact = actual objects (PDF, dataset, report), KB = knowledge graph extracted from them. No ambiguity about where papers go. |
| 2 | **KB allows conflict, Memory doesn't** | KB is evidence space (two papers can disagree). Memory is adopted state (project must decide). |
| 3 | **Library scope for non-project artifacts** | Papers, datasets, documentation are not project outputs — they're shared resources. Library can be org-level, referenced by multiple projects. |
| 4 | **KBChunk points to Artifact (not KBSource)** | Eliminates the KBSource abstraction. Every chunk traces directly to an artifact. Shorter evidence chains. |
| 5 | **Claim lazy extraction** | Full batch extraction is expensive and noisy. Extract on-demand (agent references chunk) or scheduled (tier1/2 only). Quality over quantity. |
| 6 | **Memory proposal system with auto-approve rules** | Prevents agent hallucination from polluting memory. But doesn't slow down agent — session/factual-with-evidence auto-approves. |
| 7 | **Context recipe per harness** | Survey needs KB breadth, experiment needs failure memories, writing needs evidence chains. Same context window, different information. |
| 8 | **Hybrid retrieval (vector + FTS)** | Pure vector search misses exact term matches. FTS misses semantic similarity. Both together + re-ranking boosts = best of both worlds. |
| 9 | **Concept linking via Aho-Corasick** | O(n) scan, microsecond latency. LLM-based NER would add 500ms+ per chat message. |
| 10 | **Dreaming system only produces proposals/drafts** | Prevents AI hallucination from contaminating KB/Memory. Everything it produces is either low-risk (indexing) or gated (proposal/draft). |
| 11 | **Postgres relations, not graph DB** | Initial scale is 10K relations. JOIN queries sufficient. Migrate to graph DB when multi-hop traversal becomes critical. |
| 12 | **Skill requires execution verification** | Unvalidated skills are not injected into context. Prevents propagating bad workflows. |
| 13 | **Org memory requires human approval** | Cross-project impact too large for auto-approve. |
| 14 | **Watchlist-driven collection** | Different projects track different sources. Configurable, not hardcoded. |

---

## 15. Future Improvements

### 15.1 Claim Confidence Calibration

Current: LLM self-reports confidence (often overconfident). Future: calibration layer that adjusts based on:
- Source quality tier (tier1 paper → boost)
- Multiple independent sources supporting same claim → boost
- Contradicted by verified claims → lower
- Validated by internal experiment artifact → promote to "verified"

### 15.2 Context Feedback Loop

Track which context sources agent actually uses (via tool call analysis: which chunk_ids, memory_ids referenced). Feed back into recipe weights. If experiment nodes never reference KB chunks, auto-reduce KB budget for experiment recipe.

### 15.3 Concept Auto-Alias Growth

Aho-Corasick only matches known aliases. For unmatched technical terms: run embedding similarity against concept embeddings. If match > threshold, create alias candidate (pending review). Alias table grows automatically with usage.

### 15.4 Cross-Project Knowledge Transfer

Concrete promotion triggers for project → org knowledge:
- Memory referenced by 2+ projects
- Memory verified 3+ times
- User marks as "generalizable"
- Skill validated in 2+ projects

### 15.5 Proactive Research Suggestions

MemoryAuditor currently only checks conflicts. Future: generate "research suggestion" proposals — "KB has contradictory claims about X, suggest adding a test case in next experiment." Links to project_chat tools.

### 15.6 Knowledge Quality Dashboard

Observable metrics: KB size, claim verification rate, memory health (active/stale/conflicted), dreaming job history, concept coverage. Helps users judge whether KB is actually improving agent behavior.
