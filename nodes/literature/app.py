"""AI4S 文献调研 — 三模块：搜索 / 小红书 / 知识图谱"""
import re, json, os, time, asyncio, glob
from pathlib import Path
from collections import Counter
import streamlit as st
import networkx as nx

from search_engines import SearchManager, Paper
from filter import filter_and_rank, format_paper_list
from comparison import generate_comparison_table

OUT = Path(__file__).parent / "fixtures"

st.set_page_config(page_title="AI4S 文献调研", layout="wide")
st.markdown("""
<style>
    .stApp { font-family: 'Microsoft YaHei', 'PingFang SC', sans-serif; }
    .main-header { font-size: 1.8rem; font-weight: 600; margin-bottom: 0.5rem; }
    .sub-header { font-size: 1.2rem; color: #666; margin-bottom: 1.5rem; }
    div[data-testid="stStatusWidget"] { visibility: hidden; }
    section.main > div { padding-top: 1rem; }
    .xhs-card {
        border: 1px solid rgba(255,255,255,0.18); border-radius: 12px;
        padding: 1.2rem; margin-bottom: 1rem;
        background: #1c1f26; transition: all 0.2s;
    }
    .xhs-card:hover { border-color: rgba(255,255,255,0.35); transform: translateY(-2px); }
</style>
""", unsafe_allow_html=True)

# ─── sessions state ───
if "search_results" not in st.session_state: st.session_state.search_results = []
if "search_state" not in st.session_state: st.session_state.search_state = "idle"

# ─── helper: determine pub type ───
def classify_type(p):
    """Return type label: 期刊 / 预印本 / 书籍 / 会议 / 其他"""
    t = p.pub_type or ""
    doi = p.doi or ""
    title = p.title or ""
    if "book" in t.lower(): return "书籍"
    if "proceedings" in t.lower() or "conference" in t.lower(): return "会议"
    if "arxiv" in doi.lower() or "48550" in doi: return "预印本"
    if re.search(r'[\u4e00-\u9fff]', title):
        return "中文期刊"
    if "journal" in t.lower() or doi.startswith("10."): return "期刊"
    return "其他"

# ─── Tab defs ───
st.markdown('<div class="main-header">📚 AI4S 文献调研</div>', unsafe_allow_html=True)
st.markdown('<div class="sub-header">多源搜索 · 本地数据库 · 知识图谱</div>', unsafe_allow_html=True)

tab_names = ["🔍 智能搜索", "📕 文献浏览", "🔗 知识图谱"]
tab1, tab2, tab3 = st.tabs(tab_names)

# ════════════════════════════════════════
# TAB 1: 智能搜索
# ════════════════════════════════════════
with tab1:
    col1, col2 = st.columns([5, 1])
    with col1:
        query = st.text_input("🔍 搜索论文", placeholder="输入关键词或研究课题...", label_visibility="collapsed")
    with col2:
        top_n = st.selectbox("结果数", [10, 20, 50], index=0)

    if query and st.button("搜索", type="primary"):
        st.session_state.search_state = "running"
        with st.spinner("搜索中..."):
            from filter import compute_quality_score
            mgr = SearchManager()
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                papers = loop.run_until_complete(mgr.search_all(query, max_per_source=top_n))
                # Score and sort
                for p in papers:
                    p.score = compute_quality_score(p)
                papers.sort(key=lambda p: p.score, reverse=True)
            except Exception as e:
                st.error(f"搜索失败: {e}")
                papers = []
            finally:
                loop.close()

        st.session_state.search_results = papers
        st.session_state.search_state = "done"

    papers = st.session_state.search_results
    if papers:
        type_map = {"all": "全部", "期刊": "期刊", "中文期刊": "中文期刊", "预印本": "预印本", "书籍": "书籍", "会议": "会议", "其他": "其他"}
        type_counts = Counter(classify_type(p) for p in papers)
        total = len(papers)
        type_tabs = st.tabs([f"全部 ({total})" if k == "all" else f"{v} ({type_counts.get(k, 0)})" for k, v in type_map.items()])

        for type_key, type_label in type_map.items():
            with type_tabs[list(type_map.keys()).index(type_key)]:
                filtered = papers if type_key == "all" else [p for p in papers if classify_type(p) == type_key]
                if not filtered:
                    st.info("无结果")
                    continue
                st.caption(f"共 {len(filtered)} 篇 (按综合评分排序)")
                for p in filtered:
                    # Radar scores
                    from filter import score_citations, score_recency, score_venue, score_author_influence
                    try: from cas_ranking import score_cas; cas = score_cas(p.venue or "")
                    except: cas = 0.35
                    sd = {
                        "引用": f"{score_citations(p.citations):.2f}",
                        "时效": f"{score_recency(p.year):.2f}",
                        "期刊": f"{score_venue(p.venue or '')/20:.2f}",
                        "CAS": f"{cas:.2f}",
                        "作者": f"{score_author_influence(p.authors):.2f}",
                    }
                    from radar_chart import radar_chart_html
                    radar = radar_chart_html({k: float(v) for k, v in sd.items()}, size=120)

                    cols = st.columns([1, 7])
                    with cols[0]:
                        st.markdown(radar, unsafe_allow_html=True)
                    with cols[1]:
                        st.markdown(f"**{p.title}**")
                        authors = ", ".join(p.authors[:5]) if p.authors else ""
                        if authors:
                            st.caption(authors)
                        year_str = str(p.year) if p.year else ""
                        venue_str = p.venue or ""
                        line2 = f"{venue_str} · {year_str}" if venue_str and year_str else (venue_str or year_str)
                        if line2:
                            st.caption(line2)
                        ab = p.abstract or ""
                        if ab:
                            st.caption(ab[:400])
                        if p.doi:
                            st.caption(p.doi)
                    st.divider()
    elif st.session_state.search_state == "done":
        st.info("输入课题开始搜索")

# ════════════════════════════════════════
# TAB 2: 文献浏览 (本地数据库)
# ════════════════════════════════════════
@st.cache_data(ttl=60)
def load_local_db():
    mf = OUT / "_metadata.json"
    if not mf.exists(): return []
    meta = json.load(open(mf))
    papers = []
    for doi, m in meta.items():
        ab = m.get("abstract", "") or m.get("abstract_raw", "") or ""
        has_abs = len(ab) > 80
        papers.append({
            "doi": doi if not doi.startswith("unknown:") else "",
            "title": m.get("title", "?")[:150],
            "authors": m.get("authors", "?"),
            "year": m.get("year", "?"),
            "source": m.get("source", "unknown"),
            "journal": m.get("journal", "?"),
            "abstract": ab,
            "has_abstract": has_abs,
            "file": m.get("file", ""),
            "citations": m.get("citations", 0),
        })
    return papers

with tab2:
    local = load_local_db()
    if not local:
        st.warning("本地数据库为空，请先添加论文到 papers/_metadata.json")
    else:
        src_filter = st.radio("数据源", ["全部", "crossref", "arxiv", "cnki", "中文期刊", "s2", "openalex"], horizontal=True)
        if src_filter == "中文期刊":
            filtered = [p for p in local if p["source"] == "cnki"]
        else:
            filtered = local if src_filter == "全部" else [p for p in local if p["source"] == src_filter]
        total = len(filtered)
        st.caption(f"共 {total} 篇")
        
        per_page = 20
        max_page = max(1, (total + per_page - 1) // per_page)
        page = st.selectbox("页码", range(1, max_page + 1), index=0)
        start = (page - 1) * per_page
        end = min(start + per_page, total)
        page_papers = filtered[start:end]

        cols_per_row = 3
        for row_start in range(0, len(page_papers), cols_per_row):
            cols = st.columns(cols_per_row)
            for j in range(cols_per_row):
                idx = row_start + j
                if idx >= len(filtered): break
                p = filtered[idx]
                with cols[j]:
                    st.markdown(f'<div class="xhs-card">', unsafe_allow_html=True)
                    st.markdown(f"**{p['title'][:80]}**")
                    st.caption(f"{p['authors'][:60]} · {p['year']}")
                    st.caption(f"📄 {p['journal'][:40]} | 📊 引用 {p['citations']}")
                    ab_label = "📝 摘要" if p["has_abstract"] else "⚠ 无摘要"
                    with st.expander(ab_label):
                        st.write(p["abstract"][:600] or "(无)")
                    st.markdown('</div>', unsafe_allow_html=True)

# ════════════════════════════════════════
# TAB 3: 知识图谱
# ════════════════════════════════════════
with tab3:
    # Source filter + level selector
    level = st.radio("分类层级", ["L2 (摘要)", "L3 (全文)"], horizontal=True)
    src_filter = st.radio("来源", ["全部", "arxiv", "crossref", "s2", "openalex", "cnki"], horizontal=True)
    
    yaml_files = sorted(Path(OUT).glob("L2_*.yaml"))
    yaml_files += sorted(Path(OUT).glob("L3_*.yaml"))

    if not yaml_files:
        st.warning("暂无分类结果，请先运行 classify_papers.py")
    else:
        if "L2" in level and "L3" not in level:
            # L2: files starting with L2_ but NOT containing __L3_
            yf = [f for f in yaml_files if f.name.startswith("L2_") and "__L3_" not in f.name]
        else:
            # L3: files containing __L3_
            yf = [f for f in yaml_files if "__L3_" in f.name]

        for yp in yf:
            # Parse YAML (simple key-value extraction)
            text = yp.read_text()
            label = re.search(r'title:\s*"(.+?)"', text)
            label = label.group(1) if label else yp.stem
            cnt = re.search(r'Papers:\s*(\d+)', text)
            cnt = int(cnt.group(1)) if cnt else 0

            with st.expander(f"**{label}** — {cnt}篇"):
                # Show reference papers
                refs = re.findall(r'- doi: "(.+?)"\s*\n\s+title: "(.+?)"', text)
                for doi_str, title in refs[:10]:
                    st.markdown(f"- [{title[:80]}](https://doi.org/{doi_str})")

                # Show key findings if present
                kf_section = re.search(r'关键发现\n(.*?)(?=\n\s*###|\n\s*$)', text, re.DOTALL)
                if kf_section:
                    st.markdown("**🔬 关键发现:**")
                    for line in kf_section.group(1).strip().split("\n"):
                        s = line.strip()
                        if s and not s.startswith("```"):
                            st.markdown(s)

                # Show open questions
                q_section = re.search(r'Open questions\n(.*?)(?=\n\s*memory|\n\s*$)', text, re.DOTALL)
                if q_section:
                    st.markdown("**❓ Open Questions:**")
                    for line in q_section.group(1).strip().split("\n"):
                        s = line.strip()
                        if s.startswith("Q"):
                            st.markdown(s)

st.markdown("---")
st.caption(f"AI4S Survey Harness · {len(load_local_db())} 篇本地论文")
