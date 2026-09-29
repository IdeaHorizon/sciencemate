"""本地论文索引系统

基于 SQLite 的轻量级索引，支持：
1. 论文元数据存储和去重
2. 本地快速搜索（标题/作者/摘要/关键词）
3. 增量更新机制
4. 搜索结果缓存
"""

import sqlite3
import json
import re
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Optional
from pathlib import Path

from .search_engines import Paper
from core import paths as _paths

# 索引数据库 / 搜索缓存路径 —— 真相源 core.paths（issue #166.4）。
#
# 这一层**故意**不挂在某个 run/project 下：论文元数据索引和搜索缓存是跨项目复用
# 的，绑到 run 会让每个 run 重建同一个索引。位置是
# `$HARNESS_FRAMEWORK_HOME/literature/`（可用 HARNESS_LITERATURE_HOME 覆盖），
# 仍在框架 root 内，备份 / 迁移一处就够 —— 这与「所有产物收进框架目录」不冲突。
INDEX_DIR = _paths.literature_papers_dir()
INDEX_DIR.mkdir(parents=True, exist_ok=True)
INDEX_DB = INDEX_DIR / "literature_catalog.sqlite3"

CACHE_DIR = _paths.literature_cache_dir()
CACHE_DIR.mkdir(parents=True, exist_ok=True)


class LocalPaperIndex:
    """本地论文索引管理器"""
    
    def __init__(self, db_path: str = str(INDEX_DB)):
        self.db_path = db_path
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
    
    def _init_db(self):
        """初始化数据库表结构"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS paper_index (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    authors TEXT,
                    abstract TEXT,
                    year INTEGER,
                    citations INTEGER DEFAULT 0,
                    url TEXT,
                    doi TEXT UNIQUE,
                    source TEXT,
                    venue TEXT,
                    pdf_url TEXT,
                    fields_of_study TEXT,
                    subjects TEXT,
                    pub_type TEXT,
                    pub_date TEXT,
                    keywords TEXT,
                    search_query TEXT,
                    indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # 全文搜索索引（标题+摘要+关键词）
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS paper_index_fts USING fts5(
                    title, abstract, keywords,
                    content='paper_index',
                    content_rowid='id'
                )
            """)
            
            # 搜索缓存表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS search_cache (
                    query_hash TEXT PRIMARY KEY,
                    query TEXT,
                    results TEXT,
                    cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP
                )
            """)
            
            # 创建常用索引
            conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_index_doi ON paper_index(doi)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_index_doi_lower ON paper_index(lower(doi))")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_index_year ON paper_index(year)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_index_source ON paper_index(source)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_index_search_query ON paper_index(search_query)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cached_at ON search_cache(cached_at)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS title_translations (
                    title_key TEXT PRIMARY KEY,
                    original_title TEXT NOT NULL,
                    translated_title TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS abstract_translations (
                    abstract_key TEXT PRIMARY KEY,
                    original_abstract TEXT NOT NULL,
                    translated_abstract TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ai_summaries (
                    title_key TEXT PRIMARY KEY,
                    original_title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS harvest_queries (
                    query TEXT PRIMARY KEY,
                    last_requested_at TIMESTAMP,
                    last_harvested_at TIMESTAMP,
                    last_status TEXT DEFAULT 'pending',
                    last_error TEXT,
                    paper_count INTEGER DEFAULT 0
                )
            """)
            conn.commit()

    def register_harvest_query(self, query: str) -> None:
        """登记查询，供后台增量刷新；不下载全文或图片。"""
        query = " ".join(str(query or "").split()).lower()
        if not query:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO harvest_queries(query, last_requested_at)
                   VALUES (?, CURRENT_TIMESTAMP)
                   ON CONFLICT(query) DO UPDATE SET last_requested_at=CURRENT_TIMESTAMP""",
                (query,),
            )
            conn.commit()

    def register_harvest_queries(self, queries: list[str]) -> None:
        """批量登记后台查询，避免数千本期刊逐条提交事务。"""
        normalized = list(dict.fromkeys(
            " ".join(str(query or "").split()).lower()
            for query in queries
            if str(query or "").strip()
        ))
        if not normalized:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """INSERT INTO harvest_queries(query, last_requested_at)
                   VALUES (?, CURRENT_TIMESTAMP)
                   ON CONFLICT(query) DO UPDATE SET last_requested_at=CURRENT_TIMESTAMP""",
                [(query,) for query in normalized],
            )
            conn.commit()

    def due_harvest_queries(
        self, interval_seconds: int, limit: int = 20, *, prefix: str = ""
    ) -> list[str]:
        """返回到期查询；首次登记的查询也会立即进入队列。"""
        # SQLite CURRENT_TIMESTAMP 写入 UTC。比较水位也必须使用 UTC naive，
        # 否则东八区部署会把七天周期无声缩短约八小时。
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            seconds=max(1, int(interval_seconds))
        )
        where = "(last_harvested_at IS NULL OR last_harvested_at < ?)"
        params: list[object] = [cutoff.strftime("%Y-%m-%d %H:%M:%S")]
        if prefix:
            where += " AND query LIKE ?"
            params.append(prefix.lower() + "%")
        params.append(max(1, int(limit)))
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                f"""SELECT query FROM harvest_queries WHERE {where}
                    ORDER BY COALESCE(last_harvested_at, '1970-01-01'), query
                    LIMIT ?""",
                params,
            ).fetchall()
        return [row[0] for row in rows]

    def due_harvest_queries_for(
        self,
        queries: list[str],
        interval_seconds: int,
        limit: int = 20,
    ) -> list[str]:
        """Return due rows only from the caller's active subscription set.

        Historical deployments registered every mapped journal. Reading the
        global queue after switching to subscription-only harvesting lets stale
        rows consume the limit and misreports them as failures. Chunked ``IN``
        queries avoid SQLite's parameter ceiling without deleting audit rows.
        """
        normalized = list(dict.fromkeys(
            " ".join(str(query or "").split()).lower()
            for query in queries
            if str(query or "").strip()
        ))
        if not normalized:
            return []
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(
            seconds=max(1, int(interval_seconds))
        )
        due: list[tuple[str, str]] = []
        with sqlite3.connect(self.db_path) as conn:
            for start in range(0, len(normalized), 400):
                chunk = normalized[start : start + 400]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    f"""SELECT query, COALESCE(last_harvested_at, '1970-01-01')
                        FROM harvest_queries
                        WHERE query IN ({placeholders})
                          AND (last_harvested_at IS NULL OR last_harvested_at < ?)""",
                    (*chunk, cutoff.strftime("%Y-%m-%d %H:%M:%S")),
                ).fetchall()
                due.extend((str(query), str(last_at)) for query, last_at in rows)
        due.sort(key=lambda row: (row[1], row[0]))
        return [query for query, _last_at in due[: max(1, int(limit))]]

    def mark_harvest_query(self, query: str, status: str, paper_count: int = 0, error: str = "") -> None:
        query = " ".join(str(query or "").split()).lower()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT INTO harvest_queries(query, last_harvested_at, last_status, last_error, paper_count)
                   VALUES (?, CASE WHEN ? = 'ok' THEN CURRENT_TIMESTAMP ELSE NULL END, ?, ?, ?)
                   ON CONFLICT(query) DO UPDATE SET
                     last_harvested_at=CASE WHEN excluded.last_status = 'ok'
                       THEN CURRENT_TIMESTAMP ELSE harvest_queries.last_harvested_at END,
                     last_status=excluded.last_status, last_error=excluded.last_error,
                     paper_count=excluded.paper_count""",
                (query, status, status, error[:2000], int(paper_count)),
            )
            conn.commit()

    def search_local(self, query: str, limit: int = 200) -> list[Paper]:
        """只读本地索引，不访问任何远程来源。"""
        exact = self.search(query, limit=limit)
        if len(exact) >= limit:
            return exact[:limit]
        terms = self._extract_core_terms(query)
        if not terms:
            return exact
        return self._merge_local(exact, self.search_by_terms(terms, limit=limit))[:limit]

    @staticmethod
    def _merge_local(first: list[Paper], second: list[Paper]) -> list[Paper]:
        seen: set[str] = set()
        merged: list[Paper] = []
        for paper in [*first, *second]:
            key = (paper.doi or "").lower() or "title:" + " ".join((paper.title or "").lower().split())
            if key not in seen:
                seen.add(key)
                merged.append(paper)
        return merged
    
    def get_papers_by_identity(self, candidates: list[Paper]) -> dict[str, Paper]:
        """按 DOI（无 DOI 时按标题哈希）批量读取已有 Index。

        返回键与学术搜索去重键一致；这里只做身份匹配，不执行关键词搜索，
        因而数据库里其他论文不会混进本次候选集合。
        """
        doi_keys = {
            str(p.doi or "").strip().lower()
            for p in candidates
            if str(p.doi or "").strip()
        }
        title_hashes = {
            f"hash:{self._title_hash(p.title)}"
            for p in candidates
            if not str(p.doi or "").strip() and str(p.title or "").strip()
        }
        if not doi_keys and not title_hashes:
            return {}

        rows: list[tuple] = []
        wanted = [*sorted(doi_keys), *sorted(title_hashes)]
        with sqlite3.connect(self.db_path) as conn:
            for offset in range(0, len(wanted), 400):
                batch = wanted[offset:offset + 400]
                placeholders = ",".join("?" for _ in batch)
                rows.extend(conn.execute(
                    f"SELECT * FROM paper_index WHERE lower(doi) IN ({placeholders})",
                    batch,
                ).fetchall())

        found: dict[str, Paper] = {}
        for row in rows:
            paper = self._row_to_paper(row)
            stored_identity = str(row[7] or "").strip().lower()
            if stored_identity.startswith("hash:"):
                key = "title:" + " ".join((paper.title or "").lower().split())
            else:
                key = stored_identity
            if key:
                found[key] = paper
        return found

    def add_papers(self, papers: list[Paper], search_query: str = ""):
        """批量添加论文到索引，自动去重（基于 DOI）"""
        added = 0
        skipped = 0
        
        with sqlite3.connect(self.db_path) as conn:
            for p in papers:
                # 生成唯一键
                doi = p.doi or ""
                if not doi:
                    # 无 DOI 时用标题哈希
                    doi = f"hash:{self._title_hash(p.title)}"
                
                try:
                    conn.execute("""
                        INSERT INTO paper_index
                        (title, authors, abstract, year, citations, url, doi, source,
                         venue, pdf_url, fields_of_study, subjects, pub_type, pub_date,
                         keywords, search_query)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(doi) DO UPDATE SET
                            title = CASE WHEN excluded.title != '' THEN excluded.title ELSE paper_index.title END,
                            authors = CASE WHEN excluded.authors NOT IN ('', '[]', 'null') THEN excluded.authors ELSE paper_index.authors END,
                            abstract = CASE WHEN length(trim(excluded.abstract)) > length(trim(paper_index.abstract)) THEN excluded.abstract ELSE paper_index.abstract END,
                            year = COALESCE(excluded.year, paper_index.year),
                            citations = CASE WHEN excluded.citations > paper_index.citations THEN excluded.citations ELSE paper_index.citations END,
                            url = CASE WHEN excluded.url != '' THEN excluded.url ELSE paper_index.url END,
                            source = CASE WHEN paper_index.source != '' THEN paper_index.source ELSE excluded.source END,
                            venue = CASE WHEN excluded.venue != '' THEN excluded.venue ELSE paper_index.venue END,
                            pdf_url = CASE WHEN excluded.pdf_url != '' THEN excluded.pdf_url ELSE paper_index.pdf_url END,
                            fields_of_study = CASE WHEN excluded.fields_of_study NOT IN ('', '[]', 'null') THEN excluded.fields_of_study ELSE paper_index.fields_of_study END,
                            subjects = CASE WHEN excluded.subjects NOT IN ('', '[]', 'null') THEN excluded.subjects ELSE paper_index.subjects END,
                            pub_type = CASE WHEN excluded.pub_type != '' THEN excluded.pub_type ELSE paper_index.pub_type END,
                            pub_date = CASE WHEN excluded.pub_date != '' THEN excluded.pub_date ELSE paper_index.pub_date END,
                            keywords = CASE WHEN excluded.keywords NOT IN ('', '[]', 'null') THEN excluded.keywords ELSE paper_index.keywords END,
                            search_query = CASE WHEN excluded.search_query != '' THEN excluded.search_query ELSE paper_index.search_query END
                    """, (
                        p.title,
                        json.dumps(p.authors, ensure_ascii=False),
                        p.abstract,
                        p.year,
                        p.citations,
                        p.url,
                        doi,
                        p.source,
                        p.venue,
                        p.pdf_url,
                        json.dumps(p.fields_of_study, ensure_ascii=False),
                        json.dumps(p.subjects, ensure_ascii=False),
                        p.pub_type,
                        p.pub_date,
                        json.dumps(p.keywords, ensure_ascii=False),
                        search_query.lower().strip()
                    ))
                    added += 1
                except Exception:
                    skipped += 1
            
            conn.commit()
            
            # 同步 FTS 索引
            self._rebuild_fts(conn)
        
        return added, skipped
    
    def search(self, query: str, limit: int = 200, min_year: Optional[int] = None) -> list[Paper]:
        """本地快速搜索（支持语义匹配，不限于精确查询匹配）
        
        策略：
        1. 先按 search_query 精确匹配（同一查询之前搜过的）
        2. 提取查询核心关键词，匹配标题/摘要/关键词（语义匹配）
        3. 返回合并结果，按引用量排序
        """
        query = query.lower().strip()
        
        # 提取核心关键词（去除停用词和短词）
        core_terms = self._extract_core_terms(query)
        
        with sqlite3.connect(self.db_path) as conn:
            all_ids = set()
            
            # 第 1 层：search_query 精确匹配（最相关）
            cursor = conn.execute(
                "SELECT id FROM paper_index WHERE search_query = ? ORDER BY citations DESC LIMIT ?",
                (query, limit)
            )
            exact_ids = [row[0] for row in cursor.fetchall()]
            all_ids.update(exact_ids)
            
            # 第 2 层：核心关键词语义匹配（标题/摘要/关键词）
            # 复合查询（>=4个核心词）要求至少匹配2个词，避免OR过宽
            if core_terms and len(all_ids) < limit:
                conditions = []
                params = []
                min_match = 2 if len(core_terms) >= 4 else 1
                
                for term in core_terms:
                    # 匹配标题、摘要、关键词、作者、期刊
                    conditions.append("""
                        (LOWER(title) LIKE ? 
                         OR LOWER(abstract) LIKE ? 
                         OR LOWER(keywords) LIKE ?
                         OR LOWER(venue) LIKE ?)
                    """)
                    params.extend([f"%{term}%"] * 4)
                
                # 使用 OR 连接，但要求至少匹配 min_match 个词
                where_clause = " OR ".join(conditions)
                remaining = limit - len(all_ids)
                
                # 计算匹配到的术语数量
                match_counts = " + ".join([
                    f"(CASE WHEN LOWER(title) LIKE ? OR LOWER(abstract) LIKE ? OR LOWER(keywords) LIKE ? OR LOWER(venue) LIKE ? THEN 1 ELSE 0 END)"
                    for _ in core_terms
                ])
                
                sql = f"""
                    SELECT id FROM (
                        SELECT id, (
                            {' + '.join([
                                f"(CASE WHEN LOWER(title) LIKE ? THEN 3 ELSE 0 END) + "
                                f"(CASE WHEN LOWER(abstract) LIKE ? THEN 2 ELSE 0 END) + "
                                f"(CASE WHEN LOWER(keywords) LIKE ? THEN 2 ELSE 0 END)"
                                for _ in core_terms
                            ])}
                        ) as relevance,
                        ({match_counts}) as match_count
                        FROM paper_index
                        WHERE ({where_clause}) {f"AND id NOT IN (" + ",".join(["?"]*len(all_ids)) + ")" if all_ids else ""}
                    )
                    WHERE match_count >= {min_match}
                    ORDER BY relevance DESC, match_count DESC, id DESC
                    LIMIT ?
                """
                
                # 构建参数：先放 relevance 计算的 LIKE 参数
                relevance_params = []
                for term in core_terms:
                    relevance_params.extend([f"%{term}%"] * 3)
                
                # 再放 WHERE 条件的参数
                where_params = []
                for term in core_terms:
                    where_params.extend([f"%{term}%"] * 4)
                
                # 再放 match_count 计算的参数
                matchcount_params = []
                for term in core_terms:
                    matchcount_params.extend([f"%{term}%"] * 4)
                
                # 排除已匹配的 ID
                exclude_params = list(all_ids) if all_ids else []
                
                all_params = relevance_params + where_params + matchcount_params + exclude_params + [remaining]
                
                cursor = conn.execute(sql, all_params)
                for row in cursor.fetchall():
                    all_ids.add(row[0])
            
            # 按 ID 查询完整数据
            if not all_ids:
                return []
            
            placeholders = ",".join(["?"] * len(all_ids))
            cursor = conn.execute(f"""
                SELECT * FROM paper_index
                WHERE id IN ({placeholders})
                ORDER BY citations DESC, year DESC
            """, tuple(all_ids))
            
            rows = cursor.fetchall()
            return [self._row_to_paper(row) for row in rows]
    
    def _extract_core_terms(self, query: str) -> list[str]:
        """提取查询中的核心关键词（去除停用词和短词）"""
        # 停用词（中英文）
        stopwords = {
            'the', 'a', 'an', 'of', 'in', 'to', 'and', 'is', 'for', 'with', 'on', 'that',
            'by', 'this', 'are', 'we', 'as', 'be', 'from', 'at', 'or', 'it', 'its', 'our',
            'using', 'based', 'study', 'method', 'approach', 'paper', 'research', 'propose',
            'new', 'novel', 'results', 'show', 'proposed', 'present', 'et', 'al', 'doi',
            '的', '了', '在', '是', '和', '与', '或', '为', '有', '被', '将', '从',
            '对', '等', '及', '其', '该', '此', '中', '上', '下', '前', '后',
            '模拟', '研究', '方法', '基于', '使用', '通过', '进行', '分析',
        }
        
        # 分词：按空格、标点、连字符分割
        words = re.findall(r'[a-zA-Z0-9\u4e00-\u9fff]+', query.lower())
        
        # 过滤停用词和短词
        core_terms = []
        for w in words:
            if len(w) >= 3 and w not in stopwords:
                core_terms.append(w)
            elif len(w) >= 2 and w.isalpha() and w not in stopwords:
                # 英文缩写如 LBM, IBM, DNS, LES
                core_terms.append(w)
        
        return core_terms[:8]  # 最多 8 个核心词
    
    def search_by_terms(self, terms: list[str], limit: int = 200, min_match: int = 2) -> list[Paper]:
        """按核心术语列表搜索（用于英文缩写的语义匹配）
        
        Args:
            terms: 术语列表
            limit: 返回上限
            min_match: 最少需要同时匹配几个术语（默认2，即AND逻辑）
        """
        if not terms:
            return []
        
        with sqlite3.connect(self.db_path) as conn:
            # 用 OR 连接所有术语，匹配标题/摘要/关键词
            conditions = []
            params = []
            
            for term in terms:
                conditions.append("(LOWER(title) LIKE ? OR LOWER(abstract) LIKE ? OR LOWER(keywords) LIKE ?)")
                params.extend([f"%{term}%"] * 3)
            
            where_clause = " OR ".join(conditions)
            
            # 计算匹配度：匹配到的术语数量
            match_counts = " + ".join([
                f"(CASE WHEN LOWER(title) LIKE ? OR LOWER(abstract) LIKE ? OR LOWER(keywords) LIKE ? THEN 1 ELSE 0 END)"
                for _ in terms
            ])
            
            # 添加匹配度计算的参数
            for term in terms:
                params.extend([f"%{term}%"] * 3)
            
            sql = f"""
                SELECT * FROM (
                    SELECT *, ({match_counts}) as match_count
                    FROM paper_index
                    WHERE ({where_clause})
                )
                WHERE match_count >= ?
                ORDER BY match_count DESC, citations DESC, year DESC
                LIMIT ?
            """
            params.append(min_match)
            params.append(limit)
            
            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()
            return [self._row_to_paper(row) for row in rows]
    
    def get_stats(self) -> dict:
        """获取索引统计"""
        with sqlite3.connect(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM paper_index").fetchone()[0]
            sources = conn.execute("""
                SELECT source, COUNT(*) FROM paper_index GROUP BY source
            """).fetchall()
            year_range = conn.execute("""
                SELECT MIN(year), MAX(year) FROM paper_index WHERE year IS NOT NULL
            """).fetchone()
            
            return {
                "total_papers": total,
                "sources": {s: c for s, c in sources},
                "year_range": f"{year_range[0]}-{year_range[1]}" if year_range[0] else "N/A",
            }
    
    def cache_search_results(self, query: str, papers: list[Paper], ttl_days: int = 30):
        """缓存搜索结果

        ⚠️ 缓存键**只含 query，不含来源白名单**（见 _query_hash）：同一个 query 用不同
        来源组合搜出来的结果会互相覆盖。所以任何读这份缓存的代码都必须自己再过一遍
        来源白名单（`search_engines.filter_papers_by_sources`）—— issue #230 的教训就是
        "白名单只接了一条路径，缓存那条绕过去了"。
        """
        query_hash = self._query_hash(query)
        expires = datetime.now() + timedelta(days=ttl_days)
        
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT OR REPLACE INTO search_cache (query_hash, query, results, expires_at)
                VALUES (?, ?, ?, ?)
            """, (
                query_hash,
                query,
                json.dumps([p.to_dict() for p in papers], ensure_ascii=False),
                expires.isoformat()
            ))
            conn.commit()
    
    def get_cached_results(self, query: str) -> Optional[list[Paper]]:
        """获取缓存的搜索结果

        ⚠️ 返回的记录**混着各种来源**（缓存键不含来源白名单，见 cache_search_results）。
        当前无调用方；将来要用，必须先过 `filter_papers_by_sources` 再交给上层，
        否则就是 issue #230（禁用源从缓存漏出）的翻版。
        """
        query_hash = self._query_hash(query)
        
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("""
                SELECT results, expires_at FROM search_cache
                WHERE query_hash = ? AND expires_at > ?
            """, (query_hash, datetime.now().isoformat())).fetchone()
            
            if row:
                data = json.loads(row[0])
                return [Paper(**p) for p in data]
        
        return None
    
    def get_title_translations(self, titles: list[str]) -> dict[str, str]:
        """读取英文标题的中文译名缓存；键采用与写入一致的标题哈希。"""
        if not titles:
            return {}
        keyed = {self._title_hash(title): title for title in titles if title}
        if not keyed:
            return {}
        placeholders = ",".join("?" for _ in keyed)
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT title_key, translated_title FROM title_translations "
                f"WHERE title_key IN ({placeholders})",
                tuple(keyed),
            ).fetchall()
        return {
            keyed[key]: translated
            for key, translated in rows
            if translated and key in keyed
        }

    def save_title_translations(self, translations: dict[str, str]) -> None:
        """批量保存 {英文原题: 中文译名}，重复搜索直接复用。"""
        rows = [
            (self._title_hash(title), title, translated)
            for title, translated in translations.items()
            if title and translated
        ]
        if not rows:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """INSERT INTO title_translations
                   (title_key, original_title, translated_title, updated_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(title_key) DO UPDATE SET
                     original_title=excluded.original_title,
                     translated_title=excluded.translated_title,
                     updated_at=CURRENT_TIMESTAMP""",
                rows,
            )
            conn.commit()

    @staticmethod
    def _abstract_hash(abstract: str) -> str:
        """摘要内容哈希；原文变化后不会误用旧译文。"""
        import hashlib
        normalized = " ".join(str(abstract or "").split())
        return hashlib.sha256(normalized.encode()).hexdigest()[:24]

    def get_abstract_translations(self, abstracts: list[str]) -> dict[str, str]:
        """读取完整来源摘要的中文译文缓存。"""
        keyed = {self._abstract_hash(value): value for value in abstracts if value}
        if not keyed:
            return {}
        placeholders = ",".join("?" for _ in keyed)
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT abstract_key, translated_abstract FROM abstract_translations "
                f"WHERE abstract_key IN ({placeholders})",
                tuple(keyed),
            ).fetchall()
        return {
            keyed[key]: translated
            for key, translated in rows
            if translated and key in keyed
        }

    def save_abstract_translations(self, translations: dict[str, str]) -> None:
        """缓存来源摘要译文；不写入 papers.abstract。"""
        rows = [
            (self._abstract_hash(original), original, translated)
            for original, translated in translations.items()
            if original and translated
        ]
        if not rows:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """INSERT INTO abstract_translations
                   (abstract_key, original_abstract, translated_abstract, updated_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(abstract_key) DO UPDATE SET
                     original_abstract=excluded.original_abstract,
                     translated_abstract=excluded.translated_abstract,
                     updated_at=CURRENT_TIMESTAMP""",
                rows,
            )
            conn.commit()

    def get_ai_summaries(self, titles: list[str]) -> dict[str, str]:
        """读取缺摘要论文的 AI 总结缓存；与来源摘要严格分表。"""
        if not titles:
            return {}
        keyed = {self._title_hash(title): title for title in titles if title}
        if not keyed:
            return {}
        placeholders = ",".join("?" for _ in keyed)
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute(
                f"SELECT title_key, summary FROM ai_summaries "
                f"WHERE title_key IN ({placeholders})",
                tuple(keyed),
            ).fetchall()
        return {keyed[key]: summary for key, summary in rows if summary and key in keyed}

    def save_ai_summaries(self, summaries: dict[str, str]) -> None:
        """批量保存 {论文题名: AI总结}，不写入 papers.abstract。"""
        rows = [
            (self._title_hash(title), title, summary)
            for title, summary in summaries.items()
            if title and summary
        ]
        if not rows:
            return
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                """INSERT INTO ai_summaries
                   (title_key, original_title, summary, updated_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(title_key) DO UPDATE SET
                     original_title=excluded.original_title,
                     summary=excluded.summary,
                     updated_at=CURRENT_TIMESTAMP""",
                rows,
            )
            conn.commit()

    def clear_cache(self):
        """清理过期缓存"""
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM search_cache WHERE expires_at < ?", 
                        (datetime.now().isoformat(),))
            conn.commit()
    
    def _rebuild_fts(self, conn: sqlite3.Connection):
        """重建 FTS 索引"""
        try:
            conn.execute("DELETE FROM paper_index_fts")
            conn.execute("""
                INSERT INTO paper_index_fts(rowid, title, abstract, keywords)
                SELECT id, title, abstract, keywords FROM paper_index
            """)
            conn.commit()
        except Exception:
            pass
    
    def _row_to_paper(self, row: tuple) -> Paper:
        """数据库行转 Paper 对象"""
        # row: (id, title, authors, abstract, year, citations, url, doi, source, ...)
        return Paper(
            title=row[1] or "",
            authors=json.loads(row[2]) if row[2] else [],
            abstract=row[3] or "",
            year=row[4],
            citations=row[5] or 0,
            url=row[6] or "",
            doi=row[7] or "",
            source=row[8] or "",
            venue=row[9] or "",
            pdf_url=row[10] or "",
            fields_of_study=json.loads(row[11]) if row[11] else [],
            subjects=json.loads(row[12]) if row[12] else [],
            pub_type=row[13] or "journal-article",
            pub_date=row[14] or "",
            keywords=json.loads(row[15]) if row[15] else [],
        )
    
    @staticmethod
    def _title_hash(title: str) -> str:
        """标题哈希（用于无 DOI 论文的去重）"""
        import hashlib
        normalized = re.sub(r"[^a-zA-Z0-9]", "", title.lower())[:60]
        return hashlib.md5(normalized.encode()).hexdigest()[:16]
    
    @staticmethod
    def _query_hash(query: str) -> str:
        """查询哈希（用于缓存键）"""
        import hashlib
        normalized = re.sub(r"\s+", " ", query.lower().strip())
        return hashlib.md5(normalized.encode()).hexdigest()[:16]


# ---------- 便捷函数 ----------

def get_index() -> LocalPaperIndex:
    """获取全局索引实例"""
    return LocalPaperIndex()
