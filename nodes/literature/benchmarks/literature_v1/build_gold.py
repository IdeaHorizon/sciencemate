#!/usr/bin/env python3
"""将已双标、已仲裁的候选池转为隐藏 gold；不调用网络或模型。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def normalize_doi(value: object) -> str:
    doi = str(value or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "http://dx.doi.org/", "doi.org/", "doi:"):
        if doi.startswith(prefix):
            doi = doi[len(prefix):]
    return doi.rstrip("/")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--adjudicated-candidates", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    payload = json.loads(args.adjudicated_candidates.read_text(encoding="utf-8"))
    if payload.get("status") != "candidate_pool_not_gold":
        raise ValueError("输入不是 LiteratureBench 候选池")
    tasks: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for topic in payload.get("topics") or []:
        task_id = str(topic.get("id") or "")
        if not task_id:
            raise ValueError("候选池存在缺少 id 的主题")
        papers = []
        for item in topic.get("candidates") or []:
            relevance = item.get("relevance")
            if type(relevance) is not int or not 0 <= relevance <= 3:
                raise ValueError(f"{task_id} 的 {item.get('doi') or item.get('title')} 尚未完成 relevance=0..3 仲裁")
            reviews = item.get('reviews') or []
            reviewers = {r.get('reviewer_id') for r in reviews if r.get('reviewer_id')}
            if len(reviews) != 2 or len(reviewers) != 2:
                raise ValueError(f'{task_id}: 需要两位不同审阅者的独立记录')
            for review in reviews:
                if type(review.get('relevance')) is not int or not 0 <= review['relevance'] <= 3:
                    raise ValueError(f'{task_id}: 审阅者得分无效')
                if not review.get('rationale') or not review.get('evidence_url'):
                    raise ValueError(f'{task_id}: 缺少审阅理由或证据链接')
            if any(r['relevance'] != relevance for r in reviews):
                if not item.get('adjudicator_id') or not item.get('adjudication_note'):
                    raise ValueError(f'{task_id}: 分歧需要记录仲裁者和理由')
            doi = normalize_doi(item.get("doi"))
            title = str(item.get("title") or "").strip()
            if not doi and not title:
                raise ValueError(f"{task_id} 有无法标识的候选")
            papers.append({
                "doi": doi,
                "title": title,
                "relevance": relevance,
                "core": relevance == 3,
            })
        if not any(paper["core"] for paper in papers):
            raise ValueError(f"{task_id} 没有 relevance=3 的核心论文，不能进入主榜")
        if task_id in tasks:
            raise ValueError(f'重复任务: {task_id}')
        tasks[task_id] = {"papers": papers}

    if not tasks:
        raise ValueError('不能生成空 gold')
    if args.out.exists():
        raise ValueError('输出已存在，请使用新的版本路径')
    output = {"benchmark_version": "0.3.0", "tasks": tasks}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote hidden gold: {args.out} ({len(tasks)} tasks)")


if __name__ == "__main__":
    main()
