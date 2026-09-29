"""同一件事从几个源进来，必须收敛成同一个键。

身份不收敛的后果有两层：用户在同一天的日报里看见同一篇三次；以及**热度算
不出来** —— "这篇正在被广泛讨论"的机械形态就是"几个互相独立的渠道提到了
同一个键"，键分叉时多源提及看起来只是三条各不相干的内容。
"""
from __future__ import annotations

from app.services.feed import canonical


def test_arxiv_version_suffix_is_not_part_of_the_identity() -> None:
    """`2401.01234v1` 和 `v2` 是同一篇论文的两稿。

    留着版本号，作者每更新一次日报里就多出一条"新论文"。
    """
    v1 = canonical.canonical_key(url="https://arxiv.org/abs/2401.01234v1")
    v2 = canonical.canonical_key(url="http://arxiv.org/abs/2401.01234v2")
    assert v1 == v2 == "arxiv:2401.01234"


def test_old_style_arxiv_ids_still_resolve() -> None:
    assert canonical.canonical_key(url="https://arxiv.org/abs/cond-mat/0701001") == (
        "arxiv:cond-mat/0701001"
    )


def test_doi_wins_over_url_and_is_case_folded() -> None:
    """一份东西可以有无数个 URL，但只有一个 DOI。"""
    from_link = canonical.canonical_key(url="https://www.nature.com/articles/s41563-026-1",
                                        text="doi:10.1038/S41563-026-1")
    from_doi_org = canonical.canonical_key(url="https://doi.org/10.1038/s41563-026-1")
    assert from_link == from_doi_org == "doi:10.1038/s41563-026-1"


def test_the_same_page_reached_differently_is_one_item() -> None:
    """http/https、www、末尾斜杠、utm 参数、锚点 —— 都不改变指向哪个资源。"""
    variants = [
        "https://example.org/blog/post",
        "http://www.example.org/blog/post/",
        "https://example.org/blog/post?utm_source=twitter&utm_campaign=x",
        "https://example.org/blog/post#abstract",
    ]
    keys = {canonical.canonical_key(url=v) for v in variants}
    assert len(keys) == 1, keys


def test_a_meaningful_query_string_still_separates_two_resources() -> None:
    """去参数不能去过头：`?id=1` 和 `?id=2` 是两个页面。"""
    one = canonical.canonical_key(url="https://example.org/view?id=1")
    two = canonical.canonical_key(url="https://example.org/view?id=2")
    assert one != two


def test_no_identity_means_no_item() -> None:
    """算不出身份就返回空，**不造一个**。

    造出来的键下次算还是不一样，于是同一条内容会天天作为"新内容"重新出现。
    """
    assert canonical.canonical_key(url="", guid="", text="") == ""
    assert canonical.canonical_key(url="not a url at all") != ""  # 退化成 url: 键，仍稳定


def test_a_preprint_and_its_published_version_stay_separate() -> None:
    """**故意**不合并：版本、同行评议状态、可引用性都不同，
    科研语境里把它们当成一件事是错的。"""
    preprint = canonical.canonical_key(url="https://arxiv.org/abs/2401.01234")
    published = canonical.canonical_key(url="https://doi.org/10.1038/s41586-024-1")
    assert preprint != published
