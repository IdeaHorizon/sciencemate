from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from nodes.literature.tools.journal_scope import current_domains, journal_key, scope_matches

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize('code,venue,title,expected', [
    ('050103', 'Language', 'Syntax of Central Javanese', False),
    ('050103', 'Language', 'Mandarin Chinese syntax', True),
    ('050103', 'Journal of Chinese Linguistics', 'Ellipsis mismatch', True),
    ('030405', 'Ethnomusicology', 'Tibetan ritual music', True),
    ('030405', 'Ethnomusicology', 'Irish traditional songs', False),
    ('090203', 'Food Chemistry', 'Green tea polyphenols and storage quality', True),
    ('090203', 'Food Chemistry', 'Effects of freezing on salmon', False),
    ('083204', 'Food Chemistry', 'Packaging improves salmon fillet quality', True),
    ('083204', 'Food Chemistry', 'Wheat flour rheology', False),
    ('082403', 'Ocean Engineering', 'Underwater acoustic propagation', True),
    ('082403', 'Ocean Engineering', 'Wave loads on offshore wind turbines', False),
    ('100509', 'Human Reproduction', 'Acupuncture for female infertility', True),
    ('100509', 'Human Reproduction', 'Clinical IVF outcomes', False),
    ('071010', 'Nature', 'Cryo-EM structure of a ribosomal protein', True),
    ('071010', 'Nature', 'Exoplanet orbital dynamics', False),
])
def test_cross_discipline_papers_need_subject_evidence(code, venue, title, expected):
    assert scope_matches(code, venue, title) is expected


def test_title_or_original_abstract_can_supply_evidence():
    assert scope_matches('090203', 'Food Chemistry', 'Polyphenol extraction', 'We studied Camellia sinensis leaves.')
    assert not scope_matches('090203', 'Food Chemistry', 'Teaching food chemistry')


def test_removed_relation_cannot_inherit_old_domain_tags(monkeypatch):
    monkeypatch.delenv('HARNESS_JOURNAL_DOMAIN_MAP', raising=False)
    assert current_domains('Aerospace Science and Technology', ['100107'], 'Aerospace engineering') == ()
    assert current_domains('Language', ['050103'], 'Central Javanese syntax') == ()
    assert current_domains('Language', ['050103'], 'Mandarin Chinese syntax') == ('050103',)


def test_mapping_keeps_every_catalog_leaf_and_has_no_alias_duplicates():
    catalog = json.loads((ROOT / 'platform/backend/app/data/discipline_catalog.json').read_text())
    codes = {c['domain'] for g in catalog for c in g['categories']}
    with (ROOT / 'docs/xiaohongshu_journal_map.tsv').open(encoding='utf-8') as handle:
        rows = list(csv.DictReader(handle, delimiter='\t'))
    assert {r['二级学科代码'] for r in rows} == codes
    assert len({(r['二级学科代码'], journal_key(r['journal_name'])) for r in rows}) == len(rows)


def test_policy_retains_each_verified_domain_for_the_same_journal(tmp_path, monkeypatch):
    from nodes.literature.tools.journal_policy import decide, reset_cache
    path = tmp_path / 'mapping.tsv'
    path.write_text(
        'journal_name\t二级学科代码\t分类依据\t分类状态\n'
        'Shared Journal\t070207\tmanual_source_verified\tmanually_verified\n'
        'The Shared Journal\t080300\tmanual_expert_review\tmanually_verified\n'
        'Shared Journal\t010108\tguessed\tspeculative\n'
    )
    monkeypatch.setenv('HARNESS_JOURNAL_DOMAIN_MAP', str(path))
    reset_cache()
    try:
        decision = decide(venue='Shared Journal', title='Optics study')
        assert decision.accepted
        assert decision.domains == ('070207', '080300')
    finally:
        reset_cache()


def test_policy_uses_abstract_for_a_narrow_subject(tmp_path, monkeypatch):
    from nodes.literature.tools.journal_policy import decide, reset_cache
    path = tmp_path / 'mapping.tsv'
    path.write_text(
        'journal_name\t二级学科代码\t分类依据\t分类状态\n'
        'Language\t050103\tmanual_source_verified\tmanually_verified\n'
    )
    monkeypatch.setenv('HARNESS_JOURNAL_DOMAIN_MAP', str(path))
    reset_cache()
    try:
        assert not decide(venue='Language', title='Syntax of Central Javanese').accepted
        decision = decide(venue='Language', title='Ellipsis mismatch', abstract='We study Mandarin Chinese.')
        assert decision.domains == ('050103',)
    finally:
        reset_cache()
