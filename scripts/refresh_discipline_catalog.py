"""Regenerate packaged research-interest labels from the cited university HTML table.

Usage: python scripts/refresh_discipline_catalog.py downloaded-source.html
The catalog deliberately preserves legacy interests; it is not an admissions catalog.
"""
from __future__ import annotations
import argparse
from html.parser import HTMLParser
import json
from pathlib import Path
import re

class Table(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None
    def handle_starttag(self, tag, attrs):
        if tag == 'tr': self.row = []
        if tag in ('td', 'th') and self.row is not None: self.cell = []
    def handle_data(self, data):
        if self.cell is not None: self.cell.append(data)
    def handle_endtag(self, tag):
        if tag in ('td', 'th') and self.cell is not None:
            self.row.append(re.sub(r'\s+', ' ', ''.join(self.cell)).strip())
            self.cell = None
        if tag == 'tr' and self.row is not None:
            self.rows.append(self.row)
            self.row = None

def parse_catalog(html: str) -> list[dict]:
    table = Table(); table.feed(html)
    groups, leaves = {}, {}
    for row in table.rows:
        for i, value in enumerate(row):
            match = re.fullmatch(r'(\d{4})\s*([^\d].*)', value)
            if match:
                groups[match[1]] = {'archive': 'cas:' + match[1], 'label': match[2], 'categories': []}
            if re.fullmatch(r'\d{6}', value) and i + 1 < len(row):
                # Parenthetical examples in the source contain wrapping/typographic
                # defects. Display the complete official subject name before them.
                label = re.split(r'[（(]', row[i + 1], maxsplit=1)[0].strip()
                leaves[value] = {'domain': value, 'label': label, 'kind': 'leaf'}
    leaves['030205'] = {'domain': '030205', 'label': '马克思主义理论与思想政治教育（旧目录）', 'kind': 'leaf'}
    for code, leaf in sorted(leaves.items()):
        if code[:4] not in groups:
            raise ValueError(f'Incomplete subject: {code}')
        if leaf['label'] in ('★', '☆'):
            # A first-level subject without second-level subdivisions is listed as
            # <code>00 with a star in the name cell; its name is the group's.
            leaf['label'] = groups[code[:4]]['label']
        if not leaf['label']:
            raise ValueError(f'Incomplete subject: {code}')
        groups[code[:4]]['categories'].append(leaf)
    if len(groups) != 89 or len(leaves) != 394:
        raise ValueError(f'Source structure changed: {len(groups)} groups, {len(leaves)} subjects')
    return list(groups.values())

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('html', type=Path)
    args = parser.parse_args()
    destination = Path(__file__).resolve().parents[1] / 'platform/backend/app/data/discipline_catalog.json'
    destination.write_text(json.dumps(parse_catalog(args.html.read_text(encoding='utf-8')), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
