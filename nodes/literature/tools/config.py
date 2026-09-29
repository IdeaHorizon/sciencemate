"""Survey Harness 配置"""

# 学术搜索引擎配置
SEARCH_ENGINES = {
    "arxiv": {
        "name": "arXiv",
        "base_url": "https://export.arxiv.org/api/query",
        "enabled": True,
    },
    "biorxiv": {
        "name": "bioRxiv",
        "base_url": "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
        "enabled": True,
    },
    "medrxiv": {
        "name": "medRxiv",
        "base_url": "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
        "enabled": True,
    },
    "semantic_scholar": {
        "name": "Semantic Scholar",
        "base_url": "https://api.semanticscholar.org/graph/v1",
        "enabled": True,
    },
    "pubmed": {
        "name": "PubMed",
        "base_url": "https://eutils.ncbi.nlm.nih.gov/entrez/eutils",
        "enabled": True,  # API Key 可选；无 Key 时客户端自动限制在 3 req/s 以下
    },
    "crossref": {
        "name": "CrossRef",
        "base_url": "https://api.crossref.org/works",
        "enabled": True,
    },
    "openalex": {
        "name": "OpenAlex",
        "base_url": "https://api.openalex.org/works",
        "enabled": True,
    },
    "google_scholar": {
        "name": "Google Scholar",
        "base_url": "https://scholar.lanfanshu.cn/scholar",
        # 未配置官方 API，默认关闭，避免误发请求或依赖非官方镜像。
        "enabled": False,
    },
}

# 质量筛选权重（相关度为主，质量分为辅）
QUALITY_WEIGHTS = {
    "citation_count": 0.10,   # 引用量
    "recency": 0.10,          # 时效性
    "venue_score": 0.20,      # 期刊 IF（IF=20 满分）
    "author_influence": 0.0,  # 作者影响力（已删除）
    "cas_quartile": 0.60,     # 中科院分区
}

# 引用量分档（归一化参考）
CITATION_RANGES = {
    "very_high": 500,    # 500+ 引用
    "high": 100,         # 100-499
    "medium": 20,        # 20-99
    "low": 0,            # 0-19
}

# 搜索结果限制
DEFAULT_RESULTS_PER_SOURCE = 50
MAX_RESULTS_PER_SOURCE = 200

# 各源个性化限制（CrossRef 体量大，多捞点）
PER_SOURCE_LIMITS = {
    "arxiv": 100,
    "biorxiv": 100,
    "medrxiv": 100,
    "semantic_scholar": 100,
    "pubmed": 100,
    "crossref": 200,
    "openalex": 200,
    "google_scholar": 100,
}

# S2 限流时 CrossRef 的补偿倍数
S2_FALLBACK_CROSSREF_MULTIPLIER = 3
