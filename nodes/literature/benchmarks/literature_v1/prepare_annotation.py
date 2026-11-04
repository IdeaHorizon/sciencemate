"""生成双人独立标注表；自动候选不会预填相关性得分。"""
import argparse
import csv
import json
from pathlib import Path
import yaml
from merge_result_candidates import doi_key, paper_key


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--candidates', type=Path, required=True)
    ap.add_argument('--out', type=Path, required=True)
    args = ap.parse_args()
    taskset = Path(__file__).parent / 'tasks/esi22_topics_v1.yaml'
    tasks = {t['id']: t for t in yaml.safe_load(taskset.read_text())['tasks'] if t['pilot']}
    pool = json.loads(args.candidates.read_text())
    selected = [t for t in pool['topics'] if t['id'] in tasks]
    if {t['id'] for t in selected} != set(tasks):
        raise ValueError('候选池缺少试点课题')
    args.out.mkdir(parents=True, exist_ok=False)
    rows = []
    for topic in selected:
        topic['prompt'] = tasks[topic['id']]['prompt_zh']
        for item in topic['candidates']:
            doi = doi_key(item.get('doi'))
            source_url = next((r.get('url') for r in item.get('source_records', []) if r.get('url')), '')
            lookup_url = f'https://doi.org/{doi}' if doi else source_url
            item['reviews'] = []
            rows.append(dict(task_id=topic['id'], candidate_key=paper_key(item), prompt=topic['prompt'],
                             doi=doi, title=item['title'], year=item.get('year'), venue=item.get('venue'),
                             lookup_url=lookup_url,
                             reviewer_id='', relevance='', evidence_url='', rationale=''))
    for reviewer in ('A', 'B'):
        with (args.out / f'reviewer_{reviewer}.csv').open('w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    pool['topics'] = selected
    (args.out / 'candidates.json').write_text(json.dumps(pool, ensure_ascii=False, indent=2))
    (args.out / 'README.md').write_text(
        '# 试点相关性标注\n\n'
        '两位不同审阅者独立填写 reviewer_A.csv 和 reviewer_B.csv，完成后再对照。\n'
        '3=直接回答问题的核心证据；2=直接相关；1=背景或边缘相关；0=不相关。\n'
        '按完整课题判断，题名不足时阅读来源摘要或全文，无法判断留空。\n'
        '每条填写 reviewer_id、证据链接和判断理由。DOI 真实不等于相关。\n'
        '候选来自自动提名，可能遗漏；正式评分前须纳入各系统返回的新论文并补标。\n'
        '表格隐藏提名来源和被引量；候选 JSON 保留来源供仲裁审计。\n'
        '本材料尚无人工标签，不能直接用于公布分数。\n', encoding='utf-8')
    print(f'{len(selected)} topics, {len(rows)} candidates: {args.out}')


if __name__ == '__main__':
    main()
