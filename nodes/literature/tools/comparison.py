"""结构化对比分析模块

功能：对一组论文进行对比分析，生成结构化对比表
- 提取关键信息（方法、数据集、指标、结论）
- 按主题自动分组
- 生成可读的对比表
"""

import re
from collections import defaultdict
from typing import Optional

from search_engines import Paper


# ---------- 关键词提取 ----------

# 方法关键词库
METHOD_KEYWORDS = {
    # === 数值模拟方法（保留原有）===
    "CFD": ["CFD", "computational fluid dynamics", "计算流体力学"],
    "DEM": ["DEM", "discrete element method", "离散元"],
    "LES": ["LES", "large eddy simulation", "大涡模拟"],
    "DNS": ["DNS", "direct numerical simulation", "直接数值模拟"],
    "Lattice Boltzmann": ["lattice boltzmann", "LBM"],
    "Finite Element": ["finite element", "FEM", "有限元"],
    "Finite Volume": ["finite volume", "FVM"],
    "Lagrangian": ["lagrangian", "拉格朗日"],
    "Eulerian": ["eulerian", "欧拉"],
    "Eulerian-Lagrangian": ["eulerian-lagrangian", "欧拉-拉格朗日"],
    "Random Walk": ["random walk", "随机游走"],
    "Monte Carlo": ["monte carlo", "蒙特卡洛"],
    "Machine Learning": ["machine learning", "deep learning", "neural network", "机器学习", "深度学习"],
    "Stochastic Model": ["stochastic model", "随机模型"],
    "Particle Tracking": ["particle tracking", "particle transport model", "粒子追踪"],
    "Empirical Formula": ["empirical formula", "regression", "经验公式"],
    "Stokes Law": ["stokes", "斯托克斯"],

    # === 力学性能测试（混凝土/材料方向）===
    "Compression Test": ["compressive strength", "compression test", "抗压强度", "抗压试验"],
    "Flexural Test": ["flexural strength", "flexural test", "bending strength", "抗折强度", "弯曲试验"],
    "Tensile Test": ["tensile strength", "split tensile", "splitting tensile", "抗拉强度", "劈裂"],
    "Elastic Modulus": ["elastic modulus", "young's modulus", "弹性模量"],
    "Fatigue Test": ["fatigue", "疲劳试验"],
    "Impact Test": ["impact test", "impact resistance", "冲击试验"],

    # === 耐久性测试 ===
    "Freeze-Thaw": ["freeze-thaw", "frost resistance", "冻融循环", "抗冻"],
    "Chloride Penetration": ["chloride", "氯离子", "chloride diffusion", "chloride penetration"],
    "Carbonation": ["carbonation", "carbonization", "碳化"],
    "Permeability Test": ["permeability", "water absorption", "吸水率", "permeation", "抗渗"],
    "Sulfate Attack": ["sulfate", "硫酸盐"],
    "Alkali-Aggregate Reaction": ["alkali-aggregate", "alkali-silica", "碱骨料"],

    # === 材料表征 ===
    "SEM": ["SEM", "scanning electron microscope", "scanning electron microscopy", "扫描电镜"],
    "XRD": ["XRD", "X-ray diffraction", "X射线衍射"],
    "TGA": ["TGA", "thermogravimetric", "热重"],
    "FTIR": ["FTIR", "fourier transform infrared", "红外光谱"],
    "XRF": ["XRF", "X-ray fluorescence", "X射线荧光"],
    "DSC": ["DSC", "differential scanning", "差示扫描"],
    "Mercury Porosimetry": ["mercury intrusion", "MIP", "压汞", "porosimetry"],
    "Microstructure": ["microstructure", "微观结构", "microscopic"],
    "EDS": ["EDS", "energy dispersive", "能谱"],
    "CT Scan": ["CT scan", "computed tomography", "X-ray CT", "工业CT"],

    # === 材料试验方法 ===
    "Slump Test": ["slump", "坍落度"],
    "Dry Shrinkage": ["drying shrinkage", "dry shrinkage", "干缩"],
    "Creep Test": ["creep", "徐变"],
    "Thermal Conductivity": ["thermal conductivity", "导热系数"],
    "Bond Strength": ["bond strength", "粘结强度", "bond-slip"],
    "Pull-Out Test": ["pull-out", "pullout", "拔出"],
    "Particle Size Analysis": ["particle size", "grain size", "粒径分析", "laser diffraction"],
    "Thermal Analysis": ["thermal analysis", "热分析"],
    "Porosity": ["porosity", "孔隙率", "pore structure", "pore size"],
    "Absorption Test": ["absorption test", "adsorption", "吸附"],

    # === 现场/工程方法 ===
    "Field Test": ["field test", "field measurement", "field observation", "现场测试"],
    "Statistical Analysis": ["statistical analysis", "ANOVA", "方差分析", "regression analysis"],
    "Life Cycle Assessment": ["life cycle", "LCA", "生命周期评价"],
    "Optimization": ["optimization", "遗传算法", "genetic algorithm", "particle swarm"],
    "Taguchi Method": ["taguchi", "田口"],
    "Response Surface": ["response surface", "RSM", "响应面"],
}

# 指标关键词库
METRIC_KEYWORDS = {
    "Settling Velocity": ["settling velocity", "沉降速度", "terminal velocity"],
    "Drag Coefficient": ["drag coefficient", "阻力系数", "drag force"],
    "Reynolds Number": ["reynolds", "雷诺数"],
    "Concentration": ["concentration", "浓度", "sediment concentration"],
    "Mass Balance": ["mass balance", "质量平衡", "mass budget"],
    "Flux": ["flux", "通量", "settling flux"],
    "Size Distribution": ["size distribution", "粒径分布", "size range"],
    "Shape Factor": ["shape factor", "形状因子", "sphericity"],
}

# 数据集关键词库
DATASET_KEYWORDS = {
    "Field Data": ["field data", "field observation", "野外数据", "实测"],
    "Laboratory": ["laboratory", "lab experiment", "experimental", "实验室", "实验"],
    "Numerical": ["numerical simulation", "simulation", "model", "数值模拟", "simulated data"],
    "Remote Sensing": ["remote sensing", "satellite", "遥感"],
}


def _extract_keywords(text: str, keyword_dict: dict) -> list[str]:
    """从文本中提取匹配的关键词"""
    if not text:
        return []
    text_lower = text.lower()
    found = []
    for label, keywords in keyword_dict.items():
        for kw in keywords:
            if kw.lower() in text_lower:
                found.append(label)
                break
    return found


def analyze_paper(paper: Paper) -> dict:
    """分析单篇论文，提取结构化信息"""
    full_text = f"{paper.title} {paper.abstract or ''} {paper.venue or ''}"
    
    methods = _extract_keywords(full_text, METHOD_KEYWORDS)
    metrics = _extract_keywords(full_text, METRIC_KEYWORDS)
    datasets = _extract_keywords(full_text, DATASET_KEYWORDS)
    
    # 优先使用 API 返回的真实关键词
    keywords = _get_paper_keywords(paper)
    
    abstract_text = paper.abstract or ""
    abstract_preview = abstract_text[:200] + "..." if len(abstract_text) > 200 else abstract_text
    has_abstract = bool(paper.abstract)
    
    return {
        "title": paper.title,
        "authors": paper.authors[:3],
        "year": paper.year,
        "venue": paper.venue,
        "source": paper.source,
        "citations": paper.citations,
        "score": paper.score,
        "url": paper.url or paper.doi or "",
        "doi": paper.doi,
        "methods": methods,
        "metrics": metrics,
        "datasets": datasets,
        "keywords": keywords,
        "has_abstract": has_abstract,
        "abstract_preview": abstract_preview,
        "abstract_full": paper.abstract or "",
        "fields_of_study": getattr(paper, 'fields_of_study', []) or [],
        "arxiv_categories": getattr(paper, 'arxiv_categories', []) or [],
        "subjects": getattr(paper, 'subjects', []) or [],
        "pub_type": getattr(paper, 'pub_type', 'journal-article') or 'journal-article',
        "pub_date": getattr(paper, 'pub_date', '') or '',
    }


from comparison import Paper

def _get_paper_keywords(paper: Paper) -> list[str]:
    """从论文元数据提取关键词，优先 PubMed"""
    from paper_utils import get_keywords
    return get_keywords(paper)


def _extract_paper_keywords(title: str, abstract: str) -> list[str]:
    """从标题和摘要提取论文关键词"""
    import re
    from collections import Counter
    
    text = f"{title} {abstract}".lower()
    # 去掉常见停用词
    stopwords = {"the", "a", "an", "of", "in", "to", "and", "is", "for", "with", "on", "that",
                 "by", "this", "are", "we", "as", "be", "from", "at", "or", "it", "its",
                 "our", "using", "based", "study", "method", "approach", "paper", "research",
                 "propose", "new", "novel", "results", "show", "proposed", "present",
                 "et", "al", "doi", "springer", "model", "data", "different", "two",
                 "first", "well", "can", "also", "has", "been", "were", "one", "used",
                 "high", "low", "highly", "important", "significant", "large"}
    
    # 提取2-4词短语
    words = re.findall(r'\b[a-z]{3,}\b', text)
    words = [w for w in words if w not in stopwords]
    
    # 词频统计，取前5个
    freq = Counter(words)
    # 过滤掉太常见的学术词
    for w in ["et", "al", "doi", "springer", "reserved"]:
        freq.pop(w, None)
    
    top = [w.capitalize() for w, _ in freq.most_common(6) if len(w) > 2]
    return top[:5]


def group_papers(analyses: list[dict]) -> dict[str, list[dict]]:
    """按方法/主题对论文分组"""
    groups = defaultdict(list)
    
    for a in analyses:
        # 用方法关键词分组
        if a["methods"]:
            for m in a["methods"][:2]:  # 取前2个方法
                groups[m].append(a)
        else:
            groups["Other / Unspecified"].append(a)
    
    # 去重：一篇论文可能在多个组里
    for group_name in list(groups.keys()):
        seen = set()
        unique = []
        for a in groups[group_name]:
            key = a["title"][:40]
            if key not in seen:
                seen.add(key)
                unique.append(a)
        groups[group_name] = unique
    
    return dict(groups)


def generate_comparison_table(papers: list[Paper], top_n: int = 10, s2_api_key: str = "",
                             fetch_images: bool = True, search_query: str = "") -> tuple:
    """生成结构化对比表 + 小红书卡片列表
    
    Args:
        fetch_images: True 时预取图片（慢），False 时跳过图片（快）
    Returns:
        (markdown_table: str, xhs_cards: list[dict])
    """
    analyses = [analyze_paper(p) for p in papers[:top_n]]
    
    # ─── 合并后的对比表（HTML 格式，支持雷达图内嵌）───
    table_css = """
    <style>
    .cmp-table { width:100%; border-collapse:collapse; font-size:0.88em; }
    .cmp-table th { background:rgba(128,128,128,0.1); padding:8px 10px; text-align:center; border-bottom:2px solid rgba(128,128,128,0.2); font-weight:700; }
    .cmp-table td { padding:8px 10px; border-bottom:1px solid rgba(128,128,128,0.1); vertical-align:middle; }
    .cmp-table tr:hover td { background:rgba(128,128,128,0.05); }
    .cmp-table a { text-decoration:none; }
    .cmp-table a:hover { text-decoration:underline; }
    .cmp-table .venue { font-size:0.85em; opacity:0.7; }
    .cmp-table .cas-badge { display:inline-block; padding:0 5px; border-radius:4px; font-size:0.75em; margin-left:4px; }
    </style>
    """
    rows = [table_css]
    rows.append('<table class="cmp-table">')
    rows.append('<tr><th>#</th><th>标题</th><th>年份</th><th>期刊</th><th>多维评分</th></tr>')
    for i, a in enumerate(analyses, 1):
        title_raw = a["title"]
        title_short = title_raw[:120] + "…" if len(title_raw) > 120 else title_raw
        paper_url = ""
        if a["doi"] and not a["doi"].startswith("hash:"):
            paper_url = f"https://doi.org/{a['doi']}"
        elif a.get("url") and a["url"].startswith("http"):
            paper_url = a["url"]
        title_html = f'<a href="{paper_url}" target="_blank">{title_short}</a>' if paper_url else title_short
        
        # 类型标签
        pub_type = a.get("pub_type", "journal-article")
        type_badges = {
            "book-chapter": '<span style="background:rgba(220,38,38,0.15);color:#dc2626;padding:0 5px;border-radius:3px;font-size:0.7em;margin-right:4px;">📖 书章</span>',
            "book": '<span style="background:rgba(220,38,38,0.15);color:#dc2626;padding:0 5px;border-radius:3px;font-size:0.7em;margin-right:4px;">📖 书籍</span>',
            "proceedings-article": '<span style="background:rgba(202,138,4,0.15);color:#ca8a04;padding:0 5px;border-radius:3px;font-size:0.7em;margin-right:4px;">🎤 会议</span>',
            "preprint": '<span style="background:rgba(37,99,235,0.15);color:#2563eb;padding:0 5px;border-radius:3px;font-size:0.7em;margin-right:4px;">📝 预印本</span>',
        }
        type_badge = type_badges.get(pub_type, "")
        
        # 期刊 + CAS 分区 + IF
        from cas_ranking import cas_quartile_label
        from jcr_ranking import if_label
        venue_raw = a["venue"] if a["venue"] else "待查"
        cas_label = cas_quartile_label(venue_raw)
        if_str = if_label(venue_raw)
        venue_short = venue_raw[:18]
        cas_html = ""
        if cas_label and cas_label != "—":
            q = cas_label.replace("CAS","").replace("🔝","").strip()
            color_map = {"1": "#16a34a", "2": "#2563eb", "3": "#ca8a04", "4": "#dc2626"}
            c = color_map.get(q, "#888")
            cas_html = f' <span class="cas-badge" style="background:{c}20;color:{c};">{cas_label}</span>'
        if_html = f' <span style="color:#64748b;font-size:0.75em;">{if_str}</span>' if if_str != "—" else ""
        venue_html = f'<span class="venue">{venue_short}</span>{cas_html}{if_html}'
        
        methods_str = ", ".join(a["methods"][:3]) if a["methods"] else "—"
        if len(a["methods"]) > 3:
            methods_str += "…"
        
        from radar_chart import paper_radar_scores, radar_chart_html
        paper_idx = i - 1
        p = papers[paper_idx] if paper_idx < len(papers) else None
        if p:
            scores = paper_radar_scores(p.venue or "", p.citations, p.year or 2024, p.authors,
                                         title=p.title or "", abstract=p.abstract or "", query=search_query)
            radar_img = radar_chart_html(scores, size=100)
        else:
            radar_img = f"{a['score']:.3f}"
        rows.append(f'<tr><td>{i}</td><td>{type_badge}{title_html}</td><td>{a["year"] or "—"}</td><td>{venue_html}</td><td style="text-align:center;">{radar_img}</td></tr>')
    
    rows.append('</table>')
    table = "\n".join(rows)
    
    # ─── 小红书卡片数据 ───
    cards = []
    for a in analyses:
        authors_str = ", ".join(a["authors"][:3])
        if len(a["authors"]) > 3:
            authors_str += " et al."
        doi_str = a.get("doi","") or ""
        link = f"[DOI](https://doi.org/{doi_str})" if doi_str and not doi_str.startswith("hash:") else (f"[链接]({a['url']})" if a.get("url","") else "")
        
        # 提取 arXiv ID（优先从 URL 提取，arXiv 论文可能有 DOI 干扰）
        arxiv_id = ""
        import re as _re
        url_str = a.get("url","") or ""
        doi_str = a.get("doi","") or ""
        # arXiv 来源优先从 URL 提取，避免 DOI 后缀被误匹配
        if a["source"] == "arxiv" and url_str:
            dm = _re.search(r'arxiv\.org/abs/(\d{4}\.\d{4,5})', url_str)
            if dm:
                arxiv_id = dm.group(1)
        if not arxiv_id:
            full_url = url_str or doi_str
            dm = _re.search(r'(\d{4}\.\d{4,5})', full_url)
            if dm:
                arxiv_id = dm.group(1)
        
        # 预取图片（仅在 fetch_images=True 时）
        img_url = ""
        if fetch_images:
            from paper_utils import find_paper_image
            img_url = find_paper_image(
                paper_url=a.get("url","") or "",
                source=a["source"],
                doi=a["doi"],
                arxiv_id=arxiv_id,
                s2_key=s2_api_key,
                use_browser=True,
            ) or ""
        
        cards.append({
            "title": a["title"],
            "authors": authors_str,
            "year": a["year"],
            "venue": a["venue"],
            "citations": a["citations"],
            "score": a["score"],
            "keywords": a["keywords"],
            "abstract": a["abstract_full"],
            "link": link,
            "url": a.get("url", "") or "",
            "source": a["source"],
            "doi": a["doi"],
            "image_url": img_url or "",
        })
    
    return table, cards
