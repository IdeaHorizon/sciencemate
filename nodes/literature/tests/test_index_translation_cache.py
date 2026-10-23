import sqlite3

from nodes.literature.tools.local_index import LocalPaperIndex
from nodes.literature.tools.search_engines import Paper


def test_translations_link_to_index_survive_restart_and_invalidate_changed_abstract(tmp_path):
    path = str(tmp_path / 'index.sqlite3')
    index = LocalPaperIndex(path)
    title = 'Lattice Boltzmann particle settling'
    abstract = 'A complete source abstract about irregular particle sedimentation.'
    index.add_papers([Paper(title=title, abstract=abstract, doi='10.1/particle', source='crossref')])
    index.save_title_translations({title: '格子玻尔兹曼颗粒沉降'})
    index.save_abstract_translations({abstract: '关于不规则颗粒沉降的完整摘要。'})
    index.record_paper_translations([{'title': title, 'abstract': abstract, 'doi': '10.1/particle'}])
    with sqlite3.connect(path) as conn:
        row = conn.execute('SELECT title_zh,abstract_zh FROM paper_index WHERE doi=?', ('10.1/particle',)).fetchone()
    assert row == ('格子玻尔兹曼颗粒沉降', '关于不规则颗粒沉降的完整摘要。')
    restored = LocalPaperIndex(path)
    paper = restored.get_papers_by_identity([Paper(doi='10.1/particle')])['10.1/particle']
    assert paper.title_zh == row[0] and paper.abstract_zh == row[1]
    updated = abstract + ' New results added by the publisher.'
    restored.add_papers([Paper(title=title, abstract=updated, doi='10.1/particle')])
    paper = restored.get_papers_by_identity([Paper(doi='10.1/particle')])['10.1/particle']
    assert paper.title_zh == row[0] and paper.abstract_zh is None


def test_shared_cache_accepts_html_and_whitespace_versions_of_the_full_abstract(tmp_path):
    index = LocalPaperIndex(str(tmp_path / 'index.sqlite3'))
    abstract = 'Irregular particles settle in a viscous fluid. ' * 200
    assert len(abstract) > 4000
    index.save_abstract_translations({abstract: '完整摘要译文'})
    papers = [{'title': 'Particle settling', 'abstract': '<p>' + abstract + '</p>'}]
    index.hydrate_translations(papers)
    assert papers[0]['abstract_zh'] == '完整摘要译文'
    assert index.get_abstract_translations([abstract[:4000]]) == {}


def test_full_title_keys_do_not_collide_and_legacy_cache_remains_readable(tmp_path):
    index = LocalPaperIndex(str(tmp_path / 'index.sqlite3'))
    a = 'A' * 70 + ' fluid particles'
    b = 'A' * 70 + ' solid crystals'
    assert index._title_hash(a) == index._title_hash(b)
    index.save_title_translations({a: '流体颗粒', b: '固体晶格'})
    assert index.get_title_translations([a, b]) == {a: '流体颗粒', b: '固体晶格'}
    with sqlite3.connect(index.db_path) as conn:
        conn.execute('INSERT INTO title_translations(title_key,original_title,translated_title) VALUES(?,?,?)',
                     (index._title_hash('Legacy paper'), 'Legacy paper', '历史论文'))
    assert index.get_title_translations(['Legacy paper']) == {'Legacy paper': '历史论文'}


def test_translation_created_index_is_searchable(tmp_path):
    index = LocalPaperIndex(str(tmp_path / 'index.sqlite3'))
    index.record_paper_translations([{'title': 'Sedimentation simulation', 'abstract': 'Irregular particles in a fluid.', 'doi': '10.1/new'}])
    assert index.search_local('Sedimentation')
