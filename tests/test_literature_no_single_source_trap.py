"""literature 不许同时拿到「多源检索」和「被它完全覆盖的单源检索」。

现场（node20，英国餐饮文化调研）：8 次检索挂了 3 次，全是
`Semantic Scholar HTTP 429`。查下来节点手里**同时**有：

  - search_papers            五源并行；单源挂掉只记进 unavailable_sources，其余照常返回
  - semantic_scholar_search  只搜 S2 一个源

harness 里写着"优先用多源那个"，模型还是挑了单源那个，然后被 S2 的限流
绑住整条调研线。而"限流了就换源"当时只存在于 papers.py 返回的一句 guidance
里 —— 那是**叮嘱，不是机制**，模型不听就没了。

修法不是把叮嘱写得更大声，是**让"绑死单一数据源"这个选项不存在**：多源工具
已经涵盖单源工具的全部能力（year_from 已补齐），就不该再把单源的摆在旁边。

arxiv_search 留着 —— 它有 sort_by=submittedDate 和分类信息，多源工具没有，
是真专用工具，不在此列。
"""

import pathlib

import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]

# 被 search_papers 完全覆盖的单源论文检索工具。新增一个源的专用工具时，
# 如果它没有多源工具给不了的能力，就该登记到这里。
SUBSUMED_BY_SEARCH_PAPERS = {
    "semantic_scholar_search",   # search_papers(include_s2=True, 其余 False) 等价
}


def _tool_list(node: str) -> list[str]:
    harness = yaml.safe_load((REPO / "nodes" / node / "harness.yaml").read_text())
    tools = harness.get("tools") or []
    return [t if isinstance(t, str) else (t or {}).get("name", "") for t in tools]


def test_literature_has_the_multi_source_tool():
    assert "search_papers" in _tool_list("literature")


def test_literature_does_not_also_expose_subsumed_single_source_tools():
    offenders = sorted(SUBSUMED_BY_SEARCH_PAPERS & set(_tool_list("literature")))
    assert not offenders, (
        f"literature 同时拿到了多源工具和被它完全覆盖的单源工具：{offenders}。"
        "并存 = 模型有机会把整条调研绑死在单一数据源的可用性上（node20 实测）。"
    )


def test_arxiv_search_is_kept_it_is_genuinely_specialised():
    """别把这条护栏做成"删掉一切单源工具"—— arxiv_search 有多源给不了的东西。"""
    assert "arxiv_search" in _tool_list("literature")


def test_multi_source_tool_covers_the_removed_capability():
    """撤掉 semantic_scholar_search 不能丢能力：它唯一独有的是 year_from。"""
    import nodes.literature.tools.search_papers  # noqa: F401  触发注册
    from core.tool_registry import get_tool

    tool = get_tool("search_papers")
    assert tool is not None, "search_papers 没注册上"
    schema = tool.parameters_schema
    properties = schema.get("properties") or {}
    assert "year_from" in properties, "撤了单源工具却没把 year_from 接过来"
    for source_flag in ("include_s2", "include_arxiv", "include_openalex",
                        "include_crossref", "include_cnki"):
        assert source_flag in properties, f"缺 {source_flag}，做不到单源检索"


def test_harness_text_no_longer_points_at_the_removed_tool():
    """清单里撤了、正文还在教它用，等于没撤（模型照着正文调，然后报"工具不存在"）。"""
    text = (REPO / "nodes" / "literature" / "harness.yaml").read_text()
    for name in SUBSUMED_BY_SEARCH_PAPERS:
        assert name not in text, f"harness 正文仍提到已撤下的 {name}"
