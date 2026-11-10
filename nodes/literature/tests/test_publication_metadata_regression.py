import httpx
import pytest

from nodes.literature.tools.search_engines import OpenAlexSearch, CrossrefSearch
from shared.contrib.publication_categories import publication_category
from shared.contrib.publication_relations import supplement_relation, link_supplements
from platform_runtime import _academic_nonresearch_record


@pytest.mark.parametrize("kind,expected", [
    ("dissertation", "学位论文"), ("article", "其他"),
    ("preprint", "预印本"), ("book-chapter", "书籍"),
    ("proceedings-article", "会议文章"), (None, "其他"),
])
def test_openalex_original_type_survives_parsing(kind, expected):
    with httpx.Client() as client:
        paper = OpenAlexSearch(client)._parse({"results": [{
            "title": "A study of porous materials", "type": kind,
            "language": "en", "primary_location": {"source": {"display_name": "University repository", "type": "repository"}},
        }]})[0]
    assert publication_category(paper) == expected
    assert paper.metadata_provenance["source_type"] == "repository"
    # Old cached pub_type defaults must not override newly obtained evidence.
    paper.pub_type = "journal-article"
    assert publication_category(paper) == expected


@pytest.mark.parametrize("collection,journal,expected", [
    ("Chemistry and Chemical Biology ETDs", False, "学位论文"),
    ("Physics Theses and Dissertations", False, "学位论文"),
    ("Research outputs", False, "其他"),
    ("Research outputs", True, "期刊文章"),
])
def test_repository_collection_is_used_without_a_university_blacklist(collection, journal, expected):
    with httpx.Client() as client:
        paper = OpenAlexSearch(client)._parse({"results": [{
            "title": "A scientific study", "type": "article",
            "primary_location": {"source": {"type": "repository"}, "raw_source_name": collection},
            "locations": [{"source": {"type": "journal"}}] if journal else [],
        }]})[0]
    assert publication_category(paper) == expected


def test_crossref_parent_relation_is_kept_and_not_an_independent_paper():
    with httpx.Client() as client:
        paper = CrossrefSearch(client)._parse({"message": {"items": [{
            "title": ["Dataset accompanying a classroom study"], "type": "journal-article",
            "DOI": "10.1000/data", "relation": {"is-supplement-to": [{"id-type": "doi", "id": "10.1000/study"}]},
        }]}})[0]
    assert supplement_relation(paper)["parent_doi"] == "10.1000/study"
    assert publication_category(paper) == "其他"
    assert _academic_nonresearch_record(paper.to_dict())


def test_supplement_links_to_parent_and_studies_about_materials_still_count():
    parent = {"title": "Retrieval practice intervention", "doi": "10.1037/edu0000624"}
    supplement = {"title": "Supplemental Material for Retrieval practice intervention",
                  "doi": "10.1037/edu0000624.supp", "url": "https://doi.org/10.1037/edu0000624.supp"}
    link_supplements([parent, supplement])
    assert len(parent["metadata_provenance"]["supplements"]) == 1
    assert _academic_nonresearch_record(supplement)
    assert not _academic_nonresearch_record(parent)
    genuine = {"title": "Supplemental materials improve classroom learning", "pub_type": "journal-article"}
    assert supplement_relation(genuine) is None
    assert publication_category(genuine) == "期刊文章"


def test_unrelated_suffix_and_repository_name_do_not_invent_a_thesis_or_supplement():
    assert supplement_relation({"title": "A scientific study", "doi": "10.1000/example.supp"}) is None
    assert publication_category({"title": "A scientific study", "venue": "University repository"}) == "其他"


@pytest.mark.parametrize("kind", ["article", "review", "letter"])
def test_journal_reviews_and_research_letters_keep_their_journal_category(kind):
    with httpx.Client() as client:
        paper = OpenAlexSearch(client)._parse({"results": [{
            "title": "Review of porous materials", "type": kind,
            "primary_location": {"source": {"type": "journal", "issn_l": "1234-5678"}},
        }]})[0]
    assert publication_category(paper) == "期刊文章"


@pytest.mark.parametrize('reverse', [False, True])
def test_published_version_wins_merge_in_either_order_and_keeps_preprint_link(reverse):
    from nodes.literature.tools.search_engines import SearchManager, Paper
    preprint=Paper(title='A study of thermal transport', source='arxiv', doi='10.1000/study',
                   venue='arXiv Preprint', pub_type='preprint', url='https://arxiv.org/abs/1234.12345', year=2023,
                   metadata_provenance={'publication_type':'preprint', 'source_type':'repository'})
    published=Paper(title=preprint.title, source='crossref', doi=preprint.doi, venue='Research Journal',
                    pub_type='journal-article', url='https://doi.org/10.1000/study', year=2024,
                    metadata_provenance={'publication_type':'journal-article','language':'en'})
    papers=[published,preprint] if reverse else [preprint,published]
    merged=SearchManager._merge_results(None,papers,[])
    assert len(merged)==1
    p=merged[0]
    assert publication_category(p)=='期刊文章'
    assert p.venue=='Research Journal' and p.year==2024 and p.url==published.url
    assert p.metadata_provenance['alternate_versions']==[{'source':'arxiv','url':'https://arxiv.org/abs/1234.12345'}]


def test_cached_arxiv_origin_with_positive_journal_metadata_is_published():
    from nodes.literature.tools.search_engines import SearchManager, Paper
    p=Paper(title='A study',source='arxiv',venue='arXiv Preprint',
            metadata_provenance={'publication_type':'article','source_type':'journal','source_collection':'Research Journal'})
    assert publication_category(p)=='期刊文章'
    assert SearchManager._merge_results(None,[p],[])[0].venue=='Research Journal'
    p.metadata_provenance={}
    assert publication_category(p)=='预印本'  # The dataclass default alone proves nothing.
