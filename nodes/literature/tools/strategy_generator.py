"""智能检索策略生成器

输入：研究问题（自然语言，中英文均可）
输出：结构化的检索策略（关键词、布尔逻辑、目标源、筛选条件）

支持两种模式：
1. LLM 模式 - 调用外部 LLM API 生成策略
2. 规则模式 - 基于中英文词典的自动关键词提取（兜底）
"""

import json
import os
import re
import time
from typing import Optional

import httpx

# ---------- 中英文学术关键词词典 ----------

CN_EN_DICT = {}  # 平台不内置任何领域特定词汇；英文词由模型根据用户输入生成。

# 需要补充更多领域的词可以继续扩展


def _cn_to_en(text: str) -> str:
    """中文 -> 英文学术关键词转换"""
    result = text
    # 优先处理长词，避免被短词部分匹配
    for cn, en in sorted(CN_EN_DICT.items(), key=lambda x: -len(x[0])):
        result = result.replace(cn, en)
    # 去掉剩余的中文字符和多余标点
    result = re.sub(r"[\u4e00-\u9fff]+", " ", result)
    # 清理多余空格和标点
    result = re.sub(r"[^\w\s-]", " ", result)
    result = re.sub(r"\s+", " ", result).strip()
    # 去重
    words = result.split()
    seen = set()
    unique_words = []
    for w in words:
        w_lower = w.lower()
        if w_lower not in seen and len(w) > 1:
            seen.add(w_lower)
            unique_words.append(w)
    return " ".join(unique_words)


def _extract_key_terms(text: str) -> list[str]:
    """从研究问题中提取关键词组"""
    # 按标点/连词/空格分割
    segments = re.split(r"[,，、；;。.与和及的]+", text)
    return [s.strip() for s in segments if len(s.strip()) > 1]


# ---------- LLM 配置 ----------

LLM_CONFIG = {
    "api_base": os.environ.get("STRATEGY_LLM_BASE", ""),
    "api_key": os.environ.get("STRATEGY_LLM_KEY", ""),
    "model": os.environ.get("STRATEGY_LLM_MODEL", ""),
}


def _call_llm(system_prompt: str, user_prompt: str, temperature: float = 0.1, diagnostics: dict | None = None) -> Optional[str]:
    """调用 LLM 获取回复，并记录 HTTP 阶段诊断。"""
    if not LLM_CONFIG.get("api_base") or not LLM_CONFIG.get("api_key"):
        if diagnostics is not None:
            diagnostics["status"] = "fallback_no_credentials"
        return None
    started = time.perf_counter()
    if diagnostics is not None:
        diagnostics["status"] = "started"

    def _on_response(response: httpx.Response) -> None:
        if diagnostics is not None:
            diagnostics["response_headers_elapsed"] = round(time.perf_counter() - started, 3)
            diagnostics["http_status"] = response.status_code

    try:
        # read 110s：icompify deepseek-v4-pro（thinking）拆词实测 ~40s，45s 老值
        # 一半概率压线超时 → 降级 fallback_rules 只出 1 组词，召回池缩 1/6
        # （2026-09-21 学术搜索偏少根因）。110s 与 bridge 总预算 120s 匹配。
        timeout = httpx.Timeout(connect=10.0, write=10.0, read=110.0, pool=5.0)
        with httpx.Client(timeout=timeout, event_hooks={"response": [_on_response]}) as client:
            resp = client.post(
                f"{LLM_CONFIG['api_base']}/chat/completions",
                json={
                    "model": LLM_CONFIG["model"],
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": temperature,
                    "max_tokens": 1024,
                },
                headers={"Authorization": f"Bearer {LLM_CONFIG['api_key']}"}
                if LLM_CONFIG["api_key"] else {},
            )
            if diagnostics is not None:
                diagnostics["response_read_elapsed"] = round(time.perf_counter() - started, 3)
                diagnostics["response_bytes"] = len(resp.content)
            if resp.status_code != 200:
                if diagnostics is not None:
                    diagnostics["status"] = "http_error"
                return None
            if diagnostics is not None:
                diagnostics["status"] = "ok"
            return resp.json()["choices"][0]["message"]["content"]
    except httpx.ConnectTimeout as exc:
        if diagnostics is not None:
            diagnostics.update({"status": "connect_timeout", "elapsed": round(time.perf_counter() - started, 3), "error": str(exc)})
        return None
    except httpx.ReadTimeout as exc:
        if diagnostics is not None:
            diagnostics.update({"status": "read_timeout", "elapsed": round(time.perf_counter() - started, 3), "error": str(exc)})
        return None
    except httpx.WriteTimeout as exc:
        if diagnostics is not None:
            diagnostics.update({"status": "write_timeout", "elapsed": round(time.perf_counter() - started, 3), "error": str(exc)})
        return None
    except httpx.PoolTimeout as exc:
        if diagnostics is not None:
            diagnostics.update({"status": "pool_timeout", "elapsed": round(time.perf_counter() - started, 3), "error": str(exc)})
        return None
    except Exception as exc:
        if diagnostics is not None:
            diagnostics.update({"status": "error", "elapsed": round(time.perf_counter() - started, 3), "error_type": type(exc).__name__, "error": str(exc)})
        return None


# ---------- Prompt 模板 ----------

SYSTEM_PROMPT = """你是一个科研文献检索专家。你的任务是根据用户的研究问题，生成最优的检索策略。

请严格按以下 JSON 格式输出，不要包含其他文字：

```json
{
  "research_question": "用户原文",
  "english_query": "最佳英文检索词（简练、命中率高）",
  "search_queries": [
    {
      "dimension": "core_problem",
      "query": "核心问题检索词",
      "sources": ["crossref", "semantic_scholar"],
      "weight": 1.0
    },
    {
      "dimension": "method_measurement",
      "query": "保留核心概念的方法检索词",
      "sources": ["crossref"],
      "weight": 0.8
    }
  ],
  "filters": {
    "min_citations": 0,
    "sort_by": "relevance"
  },
  "explanation": "简短说明检索策略的思路"
}
```

规则：
1. 依次检查 6 个候选维度，并在每条 query 的 dimension 字段中使用对应固定值：core_problem（核心问题）、object_attribute（对象属性）、condition_context（条件场景）、method_measurement（方法测量）、mechanism_relation（机制关系）、boundary_comparison（边界对照）。每个 dimension 最多出现一次，最终输出 1-6 条。
2. core_problem 必须保留。其余维度只有在用户输入已明确表达，或新增词语只是原问题的明确翻译、公认同义表达、标准学术术语或直接蕴含关系时才保留。仅仅属于同一领域、常与该主题一起研究或可能有用，不足以生成新的维度；不得替用户新增研究问题，也不得为凑满 6 条而补写。不得用多个同义改写重复占用同一维度。只有对象或主题名称、没有方法/条件/机制等信息的短输入，只输出 core_problem；例如抽象的“对象 A”不能擅自扩展成对象 A 的检测、来源、危害、应用等方向。
3. 每条扩展 query 都必须保留用户输入的核心对象或核心概念；不得引入原问题没有表达或直接蕴含的新对象、新方法、新条件、新指标、新应用场景或新研究目标。
4. 允许换一种方式表达同一个问题，不允许扩展成另一个相关问题；不得加入领域特定词典。重复或实质等价的 query 只保留一条。
5. weight 表示重要性（0-1）；sources 可选: crossref, semantic_scholar, arxiv, openalex。
6. 回复必须有且仅有一个 JSON 代码块"""


_QUERY_DIMENSIONS = {
    "core_problem",
    "object_attribute",
    "condition_context",
    "method_measurement",
    "mechanism_relation",
    "boundary_comparison",
}


def _strategy_group_cap(research_question: str) -> int:
    """按用户实际给出的信息量设置上限，避免短主题被模型硬扩成六个课题。"""
    question = research_question.split("\n\n检索硬约束：", 1)[0].strip()
    cjk = re.findall(r"[\u4e00-\u9fff]", question)
    if cjk:
        if len(cjk) <= 6:
            return 1
        if len(cjk) <= 12:
            return 3
        return 6

    words = re.findall(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)?", question)
    if len(words) <= 3:
        return 1
    if len(words) <= 7:
        return 3
    return 6


def _sanitize_llm_strategy(strategy: dict, research_question: str) -> dict:
    """机械落实组数和维度约束；模型负责语义判断，代码负责不越界。"""
    raw_queries = strategy.get("search_queries")
    if not isinstance(raw_queries, list):
        return strategy

    normalized: list[dict] = []
    seen_dimensions: set[str] = set()
    seen_queries: set[str] = set()
    for index, item in enumerate(raw_queries):
        if not isinstance(item, dict):
            continue
        query = " ".join(str(item.get("query") or "").split())
        if not query:
            continue
        query_key = query.casefold()
        if query_key in seen_queries:
            continue

        dimension = str(item.get("dimension") or "").strip()
        if index == 0 and dimension not in _QUERY_DIMENSIONS:
            dimension = "core_problem"
        if dimension not in _QUERY_DIMENSIONS or dimension in seen_dimensions:
            continue

        clean = dict(item)
        clean["dimension"] = dimension
        clean["query"] = query
        normalized.append(clean)
        seen_dimensions.add(dimension)
        seen_queries.add(query_key)

    normalized.sort(key=lambda item: item["dimension"] != "core_problem")
    strategy["search_queries"] = normalized[:_strategy_group_cap(research_question)]
    return strategy


def generate_strategy(research_question: str) -> dict:
    """根据研究问题生成检索策略"""
    strategy, _ = generate_strategy_detailed(research_question)
    return strategy


def generate_strategy_detailed(research_question: str) -> tuple[dict, dict]:
    """生成策略并返回不影响策略内容的模型请求诊断。"""
    diagnostics: dict = {}
    # 1. 尝试 LLM 模式
    if LLM_CONFIG.get("api_base"):
        user_prompt = f"研究问题：{research_question}\n\n请生成最优检索策略。"
        raw = _call_llm(SYSTEM_PROMPT, user_prompt, diagnostics=diagnostics)
        if raw:
            # 提取 JSON 代码块
            json_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
            if json_match:
                json_str = json_match.group(1)
            else:
                json_str = raw
            
            try:
                strategy = json.loads(json_str.strip())
                required = ["search_queries", "english_query"]
                if all(field in strategy for field in required):
                    strategy = _sanitize_llm_strategy(strategy, research_question)
                    if strategy.get("search_queries"):
                        return strategy, diagnostics
            except (json.JSONDecodeError, ValueError):
                diagnostics["status"] = "invalid_json"
    
    # 2. 无 LLM 时用规则引擎
    if not diagnostics or diagnostics.get("status") == "started":
        diagnostics["status"] = "fallback_rules"
    return _rule_based_strategy(research_question), diagnostics


def _rule_based_strategy(research_question: str) -> dict:
    """规则引擎：基于学术词典的关键词提取与策略生成"""
    
    has_chinese = bool(re.search(r"[\u4e00-\u9fff]", research_question))
    
    if has_chinese:
        # 中文：先提取子主题，再翻译成英文
        segments = _extract_key_terms(research_question)
        eng_query = _cn_to_en(research_question)
        
        # 如果词典翻译为空，直接使用原始中文（search_all 内部会 LLM 翻译）
        if not eng_query:
            eng_query = research_question
        
        # 生成多组检索词（不同角度）
        queries = []
        
        # 主检索词：完整翻译
        queries.append({
            "query": eng_query,
            "sources": ["crossref", "semantic_scholar"],
            "weight": 1.0,
        })
        
        # 如果分段后有不同的子主题，生成补充检索
        if len(segments) > 1:
            for seg in segments:
                eng_seg = _cn_to_en(seg) if _cn_to_en(seg) else seg
                if eng_seg and eng_seg != eng_query:
                    queries.append({
                        "query": eng_seg,
                        "sources": ["crossref", "semantic_scholar"],
                        "weight": 0.8,
                    })
        
        # 补一个短查询（只取前几个核心词）
        core_terms = eng_query.split()[:4]
        if len(core_terms) < len(eng_query.split()):
            queries.append({
                "query": " ".join(core_terms),
                "sources": ["crossref"],
                "weight": 0.7,
            })
    else:
        # 英文：直接生成多角度检索
        eng_query = research_question.strip()
        terms = eng_query.split()
        queries = []
        
        # 完整检索
        queries.append({
            "query": eng_query,
            "sources": ["crossref", "semantic_scholar"],
            "weight": 1.0,
        })
        
        # 短检索（去掉修饰词）
        if len(terms) > 4:
            short_q = " ".join(terms[:4])
            queries.append({
                "query": short_q,
                "sources": ["crossref", "semantic_scholar"],
                "weight": 0.8,
            })
        
        # 核心词
        if len(terms) > 2:
            core_q = " ".join(terms[:3])
            queries.append({
                "query": core_q,
                "sources": ["crossref"],
                "weight": 0.7,
            })
    
    # 去重
    seen = set()
    unique_queries = []
    for q in queries:
        key = q["query"].lower().strip()
        if key not in seen:
            seen.add(key)
            unique_queries.append(q)
    
    return {
        "research_question": research_question,
        "english_query": eng_query,
        "search_queries": unique_queries,
        "filters": {
            "min_citations": 0,
            "sort_by": "relevance",
        },
        "explanation": "规则引擎：基于中英文学术语典自动提取关键词，生成多组互补检索词",
    }


def display_strategy(strategy: dict) -> str:
    """格式化显示检索策略"""
    lines = []
    lines.append(f"📌 研究问题：{strategy['research_question']}")
    lines.append(f"🔤 核心检索词：{strategy.get('english_query', 'N/A')}")
    lines.append(f"📋 策略说明：{strategy.get('explanation', '')}")
    lines.append("")
    lines.append(f"{'─' * 50}")
    lines.append(f"检索计划 ({len(strategy['search_queries'])} 组):")
    lines.append("")
    for i, q in enumerate(strategy["search_queries"], 1):
        src_str = ", ".join(q["sources"])
        lines.append(f"  {i}. [权重 {q['weight']:.1f}] \"{q['query']}\"")
        lines.append(f"     目标源: {src_str}")
    
    filters = strategy.get("filters", {})
    if filters:
        lines.append("")
        lines.append(f"筛选条件:")
        if filters.get("min_year"):
            lines.append(f"  - 最低年份: {filters['min_year']}")
        lines.append(f"  - 排序方式: {filters.get('sort_by', 'relevance')}")
    
    return "\n".join(lines)
