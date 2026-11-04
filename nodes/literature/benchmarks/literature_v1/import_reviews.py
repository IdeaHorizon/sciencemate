"""合并两份独立 CSV 标注；分歧留待仲裁，不自动选分。"""
import argparse
import csv
import json
from pathlib import Path
from merge_result_candidates import paper_key


def load_reviews(path, expected):
    result = {}
    with path.open(encoding='utf-8-sig', newline='') as f:
        for row in csv.DictReader(f):
            key = (row['task_id'], row.get('candidate_key') or paper_key(row))
            if key in result:
                raise ValueError(f'重复标注: {key}')
            if row['relevance'] not in {'0', '1', '2', '3'}:
                raise ValueError(f'未完成标注: {key}')
            if not all(row.get(k, '').strip() for k in ('reviewer_id', 'evidence_url', 'rationale')):
                raise ValueError(f'缺少审阅者或证据: {key}')
            result[key] = {k: row[k].strip() for k in ('reviewer_id', 'evidence_url', 'rationale')}
            result[key]['relevance'] = int(row['relevance'])
    if set(result) != expected:
        raise ValueError('标注条目与候选池不一致')
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--candidates', type=Path, required=True)
    ap.add_argument('--review-a', type=Path, required=True)
    ap.add_argument('--review-b', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args()
    if args.out.exists():
        raise ValueError('输出已存在，请使用新路径')
    pool = json.loads(args.candidates.read_text())
    expected = {(t['id'], paper_key(p)) for t in pool['topics'] for p in t['candidates']}
    a, b = (load_reviews(p, expected) for p in (args.review_a, args.review_b))
    conflicts = []
    for t in pool['topics']:
        for p in t['candidates']:
            key = (t['id'], paper_key(p))
            if a[key]['reviewer_id'] == b[key]['reviewer_id']:
                raise ValueError(f'同一人不能充当两名独立审阅者: {key}')
            p['reviews'] = [a[key], b[key]]
            p['relevance'] = a[key]['relevance'] if a[key]['relevance'] == b[key]['relevance'] else None
            if p['relevance'] is None:
                conflicts.append({'task_id': key[0], 'candidate_key': key[1]})
    pool['pending_adjudication'] = conflicts
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'{len(expected)} records, {len(conflicts)} require adjudication')


if __name__ == '__main__':
    main()
