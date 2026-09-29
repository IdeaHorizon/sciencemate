"""雷达图生成 — 论文多维度质量可视化"""

import io
import base64
import math


def radar_chart_html(scores: dict, size: int = 160) -> str:
    """生成雷达图 SVG，返回 HTML img 标签
    
    scores: {"引用量": 0.7, "时效性": 0.6, "期刊": 0.8, "CAS": 0.9, "作者": 0.5}
    每项 0-1，映射到 0-10 轴
    """
    labels = list(scores.keys())
    values = [max(0.1, min(1.0, scores[k])) for k in labels]  # clamp 0.1-1.0
    n = len(labels)
    
    cx, cy = size // 2, size // 2
    radius = size // 2 - 20
    stroke_color = "#38bdf8"
    fill_color = "rgba(56,189,248,0.2)"
    grid_color = "#334155"
    text_color = "#94a3b8"
    
    svg_parts = [
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" xmlns="http://www.w3.org/2000/svg">',
        f'<rect width="{size}" height="{size}" fill="#0f172a" rx="6"/>',
    ]
    
    # 背景网格 (3 层)
    for level in [0.33, 0.67, 1.0]:
        r = radius * level
        pts = []
        for j in range(n):
            angle = math.pi * 2 * j / n - math.pi / 2
            x = cx + r * math.cos(angle)
            y = cy + r * math.sin(angle)
            pts.append(f"{x:.1f},{y:.1f}")
        poly = " ".join(pts)
        svg_parts.append(f'<polygon points="{poly}" fill="none" stroke="{grid_color}" stroke-width="0.5"/>')
    
    # 轴线
    for j in range(n):
        angle = math.pi * 2 * j / n - math.pi / 2
        x = cx + radius * math.cos(angle)
        y = cy + radius * math.sin(angle)
        svg_parts.append(f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" stroke="{grid_color}" stroke-width="0.5"/>')
    
    # 数据多边形
    data_pts = []
    for j in range(n):
        angle = math.pi * 2 * j / n - math.pi / 2
        r = radius * values[j]
        x = cx + r * math.cos(angle)
        y = cy + r * math.sin(angle)
        data_pts.append(f"{x:.1f},{y:.1f}")
    data_poly = " ".join(data_pts)
    svg_parts.append(f'<polygon points="{data_poly}" fill="{fill_color}" stroke="{stroke_color}" stroke-width="1.5"/>')
    
    # 数据点
    for j, (x, y) in enumerate([p.split(",") for p in data_pts]):
        svg_parts.append(f'<circle cx="{x}" cy="{y}" r="2.5" fill="{stroke_color}"/>')
    
    # 标签
    for j, label in enumerate(labels):
        angle = math.pi * 2 * j / n - math.pi / 2
        r = radius + 12
        x = cx + r * math.cos(angle)
        y = cy + r * math.sin(angle)
        anchor = "middle"
        if abs(angle - 0) < 0.1:
            anchor = "middle"
        elif angle > 0:
            anchor = "start"
        else:
            anchor = "end"
        svg_parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" '
            f'dominant-baseline="middle" fill="{text_color}" font-size="8">{label}</text>'
        )
    
    svg_parts.append('</svg>')
    svg_str = "".join(svg_parts)
    b64 = base64.b64encode(svg_str.encode()).decode()
    return f'<img src="data:image/svg+xml;base64,{b64}" style="width:{size}px;height:{size}px;" alt="radar"/>'


def paper_radar_scores(paper_venue: str, citations: int, year: int, authors: list,
                       title: str = "", abstract: str = "", query: str = "",
                       author_score: float = None) -> dict:
    """从论文属性计算各维度得分 (0-1)
    
    author_score: 如果提供了 H-index 归一化分则用，否则用旧的人数估算
    """
    from filter import score_citations, score_recency, score_author_influence
    from cas_ranking import score_cas
    from jcr_ranking import score_if
    
    auth = author_score if author_score is not None else score_author_influence(authors)
    
    return {
        "引用": score_citations(citations),
        "时效": score_recency(year),
        "IF": score_if(paper_venue or ""),
        "CAS": score_cas(paper_venue or ""),
        "作者": auth,
        "相关度": _score_relevance(title, abstract, query),
    }


def _score_relevance(title: str, abstract: str, query: str) -> float:
    """计算论文与查询的相关度 (0-1)"""
    if not query:
        return 0.5
    # 关键词提取
    import re
    q_terms = set(re.findall(r'[a-zA-Z0-9]{2,}', query.lower()))
    t_terms = set(re.findall(r'[a-zA-Z0-9]{2,}', (title + ' ' + abstract).lower()))
    if not q_terms:
        return 0.5
    # Jaccard 相似度
    overlap = q_terms & t_terms
    union = q_terms | t_terms
    jaccard = len(overlap) / max(1, len(q_terms))
    return round(min(1.0, jaccard * 1.5), 4)


def cluster_radar_stats(papers: list, query: str = "") -> dict:
    """计算一组论文的雷达图统计：平均分、最低分、最高分 (每维度)
    
    Returns: {"avg": {dim: score}, "min": {dim: score}, "max": {dim: score}}
    """
    from search_engines import Paper
    dims = ["引用", "时效", "IF", "CAS", "作者"]
    all_scores = {d: [] for d in dims}
    for p in papers:
        s = paper_radar_scores(
            p.venue or "", p.citations or 0, p.year or 2024, p.authors or [],
            title=p.title or "", abstract=p.abstract or "", query=query,
        )
        for d in dims:
            all_scores[d].append(s.get(d, 0.1))
    stats = {
        "avg": {d: sum(v)/len(v) for d, v in all_scores.items() if v},
        "min": {d: min(v) for d, v in all_scores.items() if v},
        "max": {d: max(v) for d, v in all_scores.items() if v},
    }
    # 总得分 = 按智能文献搜索权重加权
    if stats.get("avg"):
        _w = {"引用": 0.10, "时效": 0.10, "IF": 0.20, "CAS": 0.60, "作者": 0.0}
        stats["total"] = sum(stats["avg"].get(d, 0) * _w.get(d, 0) for d in stats["avg"])
    return stats


def cluster_radar_html(stats: dict, size: int = 160) -> str:
    """生成带范围区间的聚类雷达图 SVG（avg 线 + min-max 阴影带 + 数值标注）"""
    labels = list(stats["avg"].keys())
    n = len(labels)
    avg_v = [max(0.05, min(1.0, stats["avg"].get(k, 0.1))) for k in labels]
    min_v = [max(0.05, min(1.0, stats["min"].get(k, 0.1))) for k in labels]
    max_v = [max(0.05, min(1.0, stats["max"].get(k, 0.1))) for k in labels]
    
    cx, cy = size // 2, size // 2
    radius = size // 2 - 20
    stroke_color = "#38bdf8"
    fill_color = "rgba(56,189,248,0.2)"
    range_color = "rgba(56,189,248,0.08)"
    range_stroke = "rgba(56,189,248,0.25)"
    grid_color = "#334155"
    text_color = "#94a3b8"
    score_color = "#e2e8f0"
    
    def _pt(j, r):
        angle = math.pi * 2 * j / n - math.pi / 2
        return cx + r * math.cos(angle), cy + r * math.sin(angle)
    
    svg = [
        f'<svg width="{size}" height="{size}" viewBox="0 0 {size} {size}" xmlns="http://www.w3.org/2000/svg">',
        f'<rect width="{size}" height="{size}" fill="#0f172a" rx="6"/>',
    ]
    
    # 网格
    for level in [0.33, 0.67, 1.0]:
        pts = " ".join(f"{_pt(j, radius*level)[0]:.1f},{_pt(j, radius*level)[1]:.1f}" for j in range(n))
        svg.append(f'<polygon points="{pts}" fill="none" stroke="{grid_color}" stroke-width="0.5"/>')
    
    # 轴线
    for j in range(n):
        x, y = _pt(j, radius)
        svg.append(f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" stroke="{grid_color}" stroke-width="0.5"/>')
    
    # min-max 范围带
    for j in range(n):
        j2 = (j + 1) % n
        ax, ay = _pt(j, radius * min_v[j])
        bx, by = _pt(j, radius * max_v[j])
        cx2, cy2 = _pt(j2, radius * max_v[j2])
        dx, dy = _pt(j2, radius * min_v[j2])
        svg.append(
            f'<polygon points="{ax:.1f},{ay:.1f} {bx:.1f},{by:.1f} {cx2:.1f},{cy2:.1f} {dx:.1f},{dy:.1f}" '
            f'fill="{range_color}" stroke="{range_stroke}" stroke-width="0.5"/>'
        )
    
    # avg 多边形
    pts = " ".join(f"{_pt(j, radius*avg_v[j])[0]:.1f},{_pt(j, radius*avg_v[j])[1]:.1f}" for j in range(n))
    svg.append(f'<polygon points="{pts}" fill="{fill_color}" stroke="{stroke_color}" stroke-width="1.5"/>')
    
    # avg 数据点
    for j in range(n):
        x, y = _pt(j, radius*avg_v[j])
        svg.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" fill="{stroke_color}"/>')
    
    # 标签 + 数值
    for j, label in enumerate(labels):
        angle = math.pi * 2 * j / n - math.pi / 2
        r = radius + 12
        x, y = cx + r * math.cos(angle), cy + r * math.sin(angle)
        anchor = "middle" if abs(angle) < 0.01 or abs(abs(angle) - math.pi) < 0.01 else ("start" if angle > 0 else "end")
        svg.append(f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="{anchor}" dominant-baseline="middle" fill="{text_color}" font-size="8">{label}</text>')
        # 数值标注（放在标签外侧）
        sr = radius + 26
        sx, sy = cx + sr * math.cos(angle), cy + sr * math.sin(angle)
        svg.append(f'<text x="{sx:.1f}" y="{sy:.1f}" text-anchor="{anchor}" dominant-baseline="middle" fill="{score_color}" font-size="7" font-weight="bold">{avg_v[j]:.2f}</text>')
    
    # 小图例
    svg.append(f'<line x1="6" y1="{size-14}" x2="16" y2="{size-14}" stroke="{stroke_color}" stroke-width="1.5"/>')
    svg.append(f'<rect x="6" y="{size-10}" width="10" height="4" fill="{range_color}" stroke="{range_stroke}" stroke-width="0.5"/>')
    svg.append(f'<text x="20" y="{size-11}" fill="{text_color}" font-size="7">平均</text>')
    svg.append(f'<text x="20" y="{size-3}" fill="{text_color}" font-size="7">范围</text>')
    
    svg.append('</svg>')
    b64 = base64.b64encode("".join(svg).encode()).decode()
    return f'<img src="data:image/svg+xml;base64,{b64}" style="width:{size}px;height:{size}px;" alt="cluster-radar"/>'
