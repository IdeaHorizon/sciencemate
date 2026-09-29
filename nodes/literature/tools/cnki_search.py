"""CNKI (中国知网) 学术搜索模块

通过 WebVPN/校园网 session cookie 调用 CNKI 内部 API 获取中文文献。
需要有效的 CNKI 登录 cookie（存于 ~/.hermes/cnki_cookies.json）。

使用方法:
    from cnki_search import CnkiSearch
    searcher = CnkiSearch()
    papers = await searcher.search("海洋微塑料的沉降模拟", max_results=50)

API 说明:
    - 使用 POST https://kns.cnki.net/kns8s/brief/grid 获取搜索结果
    - 需要提供有效的 LID 等认证 cookie
    - 返回 HTML 格式的结果，用正则解析
"""

import asyncio
import json
import os
import re
import time
from typing import Optional

import httpx

from core import paths as _paths

from .search_engines import Paper

# ---------- Cookie 管理 ----------

COOKIE_FILENAME = "cnki_cookies.json"


def cookie_file() -> str:
    """CNKI cookie 落点（真相源 core.paths，issue #166.4）。

    v0.9 之前硬编码 `~/.hermes/cnki_cookies.json` —— 那是别的项目的目录名，跟本框架
    无关，且在框架 root 之外。现在收到
    `$HARNESS_FRAMEWORK_HOME/literature/credentials/`。
    """
    return str(_paths.literature_credentials_dir() / COOKIE_FILENAME)


def _legacy_cookie_file() -> str | None:
    """旧 `~/.hermes/cnki_cookies.json`，存在才返回（读回退，不再往那写）。"""
    legacy = _paths.legacy_literature_dir("credentials")
    if legacy is None:
        return None
    hit = legacy / COOKIE_FILENAME
    return str(hit) if hit.exists() else None


def load_cookies() -> dict:
    """从文件加载 CNKI cookie（新路径优先，回退旧 ~/.hermes/）"""
    path = cookie_file()
    if not os.path.exists(path):
        legacy = _legacy_cookie_file()
        if legacy is None:
            print(
                "  [CnkiSearch] ❌ 未找到 cookie 文件，请先通过浏览器登录 CNKI 并导出 cookie 到 "
                f"{path}"
            )
            return {}
        path = legacy
    with open(path) as f:
        return json.load(f)


def save_cookies(cookie_dict: dict):
    """保存 CNKI cookie 到文件（一律写新路径）"""
    path = cookie_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(cookie_dict, f)
    print(f"  [CnkiSearch] ✅ Cookie 已保存到 {path}")


# ---------- 数据库代码映射 ----------

DB_CODES = {
    # 中文数据库
    "CJFQ": "学术期刊",
    "CJFN": "期刊（增刊）",
    "CDFD": "博士论文",
    "CMFD": "硕士论文",
    "CPFD": "会议论文",
    "IPFD": "国际会议论文",
    "CPVD": "会议论文（卷）",
    # 报纸
    "CCND": "报纸",
    # 外文
    "WWJD": "外文期刊",
}

# 中英对照
DB_LABELS = {
    "CJFQ": "期刊", "CJFN": "期刊", "CDFD": "博士", "CMFD": "硕士",
    "CPFD": "会议", "IPFD": "会议", "CPVD": "会议",
    "CCND": "报纸",
}

# 跨库代码（根据当前用户可用数据库自动填充）
# 从 cookie 中的 knsadv-searchtype 解析，或使用默认集
DEFAULT_CROSSDB = "YSTT4HG0,LSTPFY1C,JUP3MUPD,MPMFIG1A,EMRPGLPA,WQ0UVIAA,BLZOG7CK,PWFIRAGL,NN3FJMUV,NLBO1Z6R"
DEFAULT_PRODUCT = "YSTT4HG0,LSTPFY1C,RMJLXHZ3,JQIRZIYA,JUP3MUPD,1UR4K4HZ,BPBAFJ5S,R79MZMCB,MPMFIG1A,EMRPGLPA,J708GVCE,ML4DRIDX,WQ0UVIAA,NB3BWEHK,XVLO76FD,HR1YT1Z9,BLZOG7CK,PWFIRAGL,NN3FJMUV,NLBO1Z6R"
DEFAULT_CLASSID = "WD0FTY92"


# ---------- 搜索参数构建 ----------

def build_query_json(keyword: str) -> str:
    """构建 CNKI 搜索的 QueryJson 参数（SearchQueryState 的 JSON 字符串）"""
    search_state = {
        "Platform": "",
        "Resource": "CROSSDB",
        "Classid": DEFAULT_CLASSID,
        "Products": "",
        "QNode": {
            "QGroup": [{
                "Key": "Subject",
                "Title": "",
                "Logic": 0,
                "Items": [{
                    "Field": "SU",
                    "Value": keyword,
                    "Operator": "TOPRANK",
                    "Logic": 0,
                    "Title": "主题"
                }],
                "ChildItems": []
            }]
        },
        "ExScope": 1,
        "SearchType": 2,
        "Rlang": "CHINESE",
        "KuaKuCode": DEFAULT_CROSSDB,
        "Expands": {},
        "View": "changeDBCh",
        "SearchFrom": 1,
    }
    return json.dumps(search_state, ensure_ascii=False)


def build_post_data(keyword: str, page: int = 1, page_size: int = 20) -> dict:
    """构建 POST 到 brief/grid 的完整表单数据"""
    return {
        "boolSearch": "true",
        "QueryJson": build_query_json(keyword),
        "pageNum": str(page),
        "pageSize": str(page_size),
        "sortField": "",
        "sortType": "",
        "dstyle": "listmode",
        "productStr": DEFAULT_PRODUCT,
        "aside": f"(主题：{keyword})",
        "searchFrom": "资源范围：总库",
        "subject": "",
        "language": "",
        "uniplatform": "",
        "CurPage": str(page),
    }


# ---------- HTML 解析 ----------

def parse_papers(html: str) -> list[Paper]:
    """从 CNKI 的 brief/grid 返回的 HTML 中解析论文列表"""
    papers = []

    # 按 <tr> 分割（每个论文是一个表格行）
    trs = re.split(r'<tr>', html)

    for tr in trs:
        if 'class="fz14"' not in tr:
            continue

        # ---- 标题 ----
        title_m = re.search(r'<a class="fz14"[^>]*>([\s\S]*?)</a>', tr)
        if not title_m:
            continue
        title = re.sub(r'<[^>]+>', '', title_m.group(1)).strip()
        if not title:
            continue

        # ---- 作者 ----
        authors_m = re.findall(r'<a class="KnowledgeNetLink"[^>]*>([^<]+)</a>', tr)
        authors = [a.strip() for a in authors_m if a.strip()]

        # ---- 来源（期刊/学位授予单位） ----
        source_m = re.search(r'<td class="source"[^>]*>([\s\S]*?)</td>', tr)
        source = ""
        if source_m:
            source = re.sub(r'<[^>]+>', '', source_m.group(1)).strip()

        # ---- 日期 ----
        date_m = re.search(r'<td class="date"[^>]*>([^<]*)</td>', tr)
        date = date_m.group(1).strip() if date_m else ""
        year = None
        if date and len(date) >= 4:
            try:
                year = int(date[:4])
            except ValueError:
                year = None

        # ---- 数据库类型 ----
        db_m = re.search(r'data-dbname="([^"]+)"', tr)
        db_label = DB_LABELS.get(db_m.group(1), db_m.group(1)) if db_m else "未知"

        # ---- 引用数 ----
        cite_m = re.search(r'class="cited"[^>]*>([^<]*)</td>', tr)
        citations = 0
        if cite_m:
            try:
                citations = int(cite_m.group(1).strip())
            except ValueError:
                citations = 0

        # ---- 摘要链接 ----
        link_m = re.search(r'<a class="fz14"[^>]*href="([^"]+)"', tr)
        link = link_m.group(1) if link_m else ""

        paper = Paper(
            title=title,
            authors=authors,
            year=year,
            venue=source,
            source=f"cnki/{db_label}",
            citations=citations,
            url=link,
            abstract="",
        )
        papers.append(paper)

    # 从 html 提取总结果数
    total = 0
    total_m = re.search(r'共找到</span>\s*<em>(\d+)</em>', html)
    if total_m:
        try:
            total = int(total_m.group(1))
        except ValueError:
            pass

    return papers, total


# ---------- 搜索类 ----------

class CnkiSearch:
    """CNKI 搜索器"""

    def __init__(self, client: Optional[httpx.AsyncClient] = None):
        self.client = client or httpx.AsyncClient(timeout=15)
        self.base_url = "https://kns.cnki.net/kns8s/brief/grid"
        self.cookies = load_cookies()
        self._headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://kns.cnki.net",
            "Referer": "https://kns.cnki.net/kns8s/defaultresult/index",
        }

    @property
    def is_ready(self) -> bool:
        """检查是否已配置有效的 cookie"""
        return bool(self.cookies and "LID" in self.cookies)

    async def search(self, keyword: str, max_results: int = 50) -> list[Paper]:
        """搜索 CNKI 并返回 Paper 列表"""
        if not self.is_ready:
            print("  [CnkiSearch] ⚠️ 未配置 CNKI cookie，跳过 CNKI 搜索")
            return []

        page_size = min(max_results, 100)
        all_papers = []
        total_count = 0

        async with httpx.AsyncClient(timeout=15) as client:
            # 只取第一页（CNKI 通常一页 20 条够用）
            data = build_post_data(keyword, page=1, page_size=page_size)
            try:
                r = await client.post(
                    self.base_url, data=data, headers=self._headers, cookies=self.cookies
                )
                if r.status_code == 200 and '验证' not in r.text[:300]:
                    papers, total = parse_papers(r.text)
                    all_papers.extend(papers)
                    total_count = total
                    print(f"  [CnkiSearch] ✅ 找到 {len(papers)} 篇 (共 {total_count} 篇，搜索词: {keyword[:30]})")
                else:
                    print(f"  [CnkiSearch] ⚠️ 请求失败 (HTTP {r.status_code}) 或触发验证码")
            except Exception as e:
                print(f"  [CnkiSearch] ❌ 异常: {type(e).__name__}: {e}")

        return all_papers[:max_results]


# ---------- 独立测试 ----------

if __name__ == "__main__":
    async def test():
        searcher = CnkiSearch()
        papers = await searcher.search("海洋微塑料的沉降模拟")
        print(f"\n结果: {len(papers)} 篇")
        for p in papers[:5]:
            print(f"  [{p.source}] {p.title} | {', '.join(p.authors[:3])} | {p.venue}")

    asyncio.run(test())
