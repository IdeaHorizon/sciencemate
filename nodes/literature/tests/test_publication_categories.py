from types import SimpleNamespace

import pytest

from platform_runtime import _publication_category


@pytest.mark.parametrize("metadata,expected", [
    ({"title":"DeepSeek赋能基层公务员数字素养的机遇与路径", "pub_type":"journal-article", "source":"crossref"}, "中文期刊"),
    ({"title":"Digital literacy", "title_zh":"数字素养", "pub_type":"journal-article", "source":"crossref"}, "期刊文章"),
    ({"title":"An English title", "pub_type":"journal-article", "source":"cnki"}, "期刊文章"),
    ({"title":"数字素养研究", "document_type":"硕士", "pub_type":"journal-article", "source":"cnki"}, "学位论文"),
    ({"title":"Digital literacy", "pub_type":"dissertation", "source":"crossref"}, "学位论文"),
    ({"title":"数字素养研究", "pub_type":"conference-paper"}, "会议文章"),
    ({"title":"数字素养研究", "pub_type":"book-chapter"}, "书籍"),
    ({"title":"中文预印本", "source":"arxiv", "pub_type":"journal-article"}, "预印本"),
    ({"title":"DNA", "pub_type":"journal-article", "language":"zh-CN"}, "中文期刊"),
])
def test_original_metadata_and_type_determine_display_category(metadata, expected):
    assert _publication_category(SimpleNamespace(**metadata)) == expected
