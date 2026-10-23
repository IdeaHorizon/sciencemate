"""智能检索策略生成器

输入：研究问题（自然语言，中英文均可）
输出：结构化的检索策略（关键词、布尔逻辑、目标源、筛选条件）

支持两种模式：
1. LLM 模式 - 调用外部 LLM API 生成策略
2. 规则模式 - 基于中英文词典的自动关键词提取（兜底）
"""

import hashlib
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


def _call_llm(
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.1,
    diagnostics: dict | None = None,
    max_tokens: int = 1024,
    read_timeout: float = 110.0,
) -> Optional[str]:
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
        timeout = httpx.Timeout(connect=10.0, write=10.0, read=read_timeout, pool=5.0)
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
                    "max_tokens": max_tokens,
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
            payload = resp.json()
            choice = payload["choices"][0]
            if diagnostics is not None:
                diagnostics["finish_reason"] = choice.get("finish_reason")
                usage = payload.get("usage") or {}
                diagnostics["completion_tokens"] = usage.get("completion_tokens")
                token_details = usage.get("completion_tokens_details") or {}
                diagnostics["reasoning_tokens"] = token_details.get("reasoning_tokens")
            return choice["message"].get("content") or ""
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
6. 用户用中文提问时，english_query 必须是保留核心对象、关系和场景的英文检索短语，不能照抄中文，也不能只给 DNA、AI 等单个宽泛词。search_queries 中供国际数据库使用的 query 也必须是英文；中文原问题由调用方单独用于中文来源。用户用英文提问时同样保留核心限定词。
7. 回复必须有且仅有一个 JSON 代码块"""


_QUERY_DIMENSIONS = {
    "core_problem",
    "object_attribute",
    "condition_context",
    "method_measurement",
    "mechanism_relation",
    "boundary_comparison",
}


def _english_query_ok(value: object) -> bool:
    """Return whether *value* is usable as an international academic query."""
    text = " ".join(str(value or "").split())
    if not text or re.search(r"[\u4e00-\u9fff]", text):
        return False
    words = re.findall(r"[A-Za-z][A-Za-z0-9-]{1,}", text)
    return len(words) >= 2


def _query_semantic_issues(source: str, candidate: str) -> list[str]:
    """Check language-independent evidence that a translation kept key constraints."""
    issues: list[str] = []
    if not _english_query_ok(candidate):
        return ["not_usable_english"]

    original = source.split("\n\n检索硬约束：", 1)[0]
    candidate_lower = candidate.lower()
    candidate_compact = re.sub(r"[^a-z0-9]", "", candidate_lower)
    anchors = re.findall(
        r"[A-Za-z][A-Za-z0-9]*(?:[.-][A-Za-z0-9]+)*|\d+(?:\.\d+)?",
        original,
    )
    ignored_anchors = {"and", "or", "not", "today", "this", "day"}
    missing_anchors = []
    for anchor in dict.fromkeys(anchors):
        compact = re.sub(r"[^a-z0-9]", "", anchor.lower())
        if compact in ignored_anchors or len(compact) < 2:
            continue
        if compact not in candidate_compact:
            missing_anchors.append(anchor)
    if missing_anchors:
        issues.append("missing_ascii_anchors:" + ",".join(missing_anchors[:6]))

    if re.search(r"排除|不包括|不要|不纳入|剔除|不算", original) and not re.search(
        r"\b(?:not|exclude|excluding|excluded|without|except)\b", candidate_lower
    ):
        issues.append("missing_exclusion")

    if re.search(r"比较|对比|哪种|关系|差异|相比", original) and not re.search(
        r"\b(?:and|versus|vs|compar\w*|relationship|association|effect|impact)\b",
        candidate_lower,
    ):
        issues.append("missing_relation")

    cjk_count = len(re.findall(r"[\u4e00-\u9fff]", original))
    content_words = re.findall(r"[A-Za-z][A-Za-z0-9-]{1,}", candidate)
    minimum_words = 8 if cjk_count > 60 else 5 if cjk_count > 30 else 3
    if len(content_words) < minimum_words:
        issues.append(f"insufficient_detail:{len(content_words)}<{minimum_words}")

    if re.search(
        r"\b(?:here is|the user|source text|translate|translation|academic database query)\b",
        candidate_lower,
    ):
        issues.append("contains_meta_explanation")
    return issues


def _response_diagnostics(raw: str) -> dict:
    """Keep enough evidence for debugging without retaining a full model reply."""
    normalized = " ".join(str(raw or "").split())
    preview = re.sub(r"(?i)\bbearer\s+\S+", "Bearer [REDACTED]", normalized[:160])
    preview = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[REDACTED]", preview)
    return {
        "output_chars": len(raw or ""),
        "output_sha256": hashlib.sha256((raw or "").encode("utf-8")).hexdigest()[:16],
        "output_has_cjk": bool(re.search(r"[\u4e00-\u9fff]", raw or "")),
        "output_preview": preview,
    }


def _extract_english_query(raw: str) -> str:
    """Extract an English query from JSON, fenced, labelled, or multiline output."""
    text = str(raw or "").strip()
    if not text:
        return ""

    json_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    json_text = json_match.group(1) if json_match else text
    try:
        parsed = json.loads(json_text.strip())
        if isinstance(parsed, dict):
            candidate = " ".join(str(parsed.get("english_query") or "").split())
            if _english_query_ok(candidate):
                return candidate
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    candidates: list[tuple[int, int, str]] = []
    for index, line in enumerate(text.splitlines()):
        candidate = line.strip().strip("`*# ")
        candidate = re.sub(
            r"^(?:[-*]\s*)?(?:english\s+(?:query|translation)|translation|query)\s*[:：-]\s*",
            "",
            candidate,
            flags=re.IGNORECASE,
        ).strip(" `\"'")
        candidate = " ".join(candidate.split())
        words = re.findall(r"[A-Za-z][A-Za-z0-9-]{1,}", candidate)
        # Detailed Boolean queries can legitimately exceed 40 words. The
        # translation prompt requests one line, so reject only implausibly large
        # prose rather than truncating a valid, constraint-rich query.
        if not _english_query_ok(candidate) or len(words) > 120 or len(candidate) > 1200:
            continue
        candidates.append((len(words), index, candidate))
    if not candidates:
        return ""
    return max(candidates, key=lambda item: (item[1], item[0]))[2]


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
    has_chinese = bool(re.search(r"[\u4e00-\u9fff]", research_question))
    # English input benefits from the full structured strategy. Chinese input goes
    # directly through the dedicated translation path below; in production the
    # structured call almost always fell through and only added latency.
    if LLM_CONFIG.get("api_base") and not has_chinese:
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
                    diagnostics["status"] = "invalid_strategy"
                else:
                    diagnostics["status"] = "invalid_strategy"
            except (json.JSONDecodeError, ValueError):
                diagnostics["status"] = "invalid_json"

    # A general English-language search phrase is still useful if structured
    # JSON generation fails. This does not use a benchmark-specific dictionary.
    if has_chinese and LLM_CONFIG.get("api_base"):
        attempts = [
            (
                "Translate the user's research question into a concise English academic "
                "database search query. Preserve every named entity, method, population, "
                "time/place condition, comparison, and exclusion. Express exclusions with "
                "NOT. Return only English search terms on one line; no JSON, explanation, "
                "quotation marks, or Chinese characters.",
                research_question,
                2048,
                25.0,
            ),
            (
                "Produce exactly one English academic database query. Preserve every named "
                "entity, method, population, relationship, time/place condition and "
                "comparison. Render every requested exclusion with NOT. Output the query "
                "only, with no introduction, commentary, JSON, or source-language text.",
                f"SOURCE TEXT BEGIN\n{research_question}\nSOURCE TEXT END",
                4096,
                30.0,
            ),
            (
                "You are a translation engine. Reply with English keywords only.",
                "Convert this Chinese research request into one English Boolean literature "
                "search query. Keep names, methods, populations, comparisons, conditions, "
                "and NOT exclusions. Do not explain:\n" + research_question,
                4096,
                35.0,
            ),
        ]
        any_translation_output = False
        for attempt_number, (system_prompt, user_prompt, max_tokens, read_timeout) in enumerate(attempts, 1):
            translation_diagnostics: dict = {}
            translation = _call_llm(
                system_prompt,
                user_prompt,
                temperature=0.0,
                diagnostics=translation_diagnostics,
                max_tokens=max_tokens,
                read_timeout=read_timeout,
            )
            diagnostics[f"translation_attempt_{attempt_number}_request_status"] = (
                translation_diagnostics.get("status")
            )
            for key in ("finish_reason", "completion_tokens", "reasoning_tokens"):
                diagnostics[f"translation_attempt_{attempt_number}_{key}"] = (
                    translation_diagnostics.get(key)
                )
            if translation:
                any_translation_output = True
                candidate = _extract_english_query(translation)
            else:
                candidate = ""
            semantic_issues = _query_semantic_issues(research_question, candidate)
            diagnostics[f"translation_attempt_{attempt_number}_semantic_issues"] = semantic_issues
            if candidate and not semantic_issues:
                diagnostics["status"] = (
                    "fallback_translated" if attempt_number == 1
                    else "fallback_translated_recovery"
                )
                diagnostics["translation_request_status"] = translation_diagnostics.get("status")
                diagnostics["translation_status"] = "valid"
                diagnostics["translation_valid"] = True
                diagnostics["translation_attempts"] = attempt_number
                return {
                    "research_question": research_question,
                    "english_query": candidate,
                    "search_queries": [{
                        "dimension": "core_problem", "query": candidate,
                        "sources": ["crossref", "openalex", "arxiv"], "weight": 1.0,
                    }],
                    "filters": {"min_citations": 0, "sort_by": "relevance"},
                }, diagnostics
            if translation:
                output_diagnostics = _response_diagnostics(translation)
                diagnostics.update({
                    f"translation_{key}": value
                    for key, value in output_diagnostics.items()
                })
                diagnostics.update({
                    f"translation_attempt_{attempt_number}_{key}": value
                    for key, value in output_diagnostics.items()
                })
        diagnostics["translation_status"] = (
            "invalid_output" if any_translation_output else "request_failed"
        )
        diagnostics["translation_request_status"] = translation_diagnostics.get("status")
        diagnostics["translation_valid"] = False
        diagnostics["translation_attempts"] = len(attempts)
    
    # 2. 无 LLM 时用规则引擎
    if not diagnostics or diagnostics.get("status") in {"started", "ok"}:
        diagnostics["status"] = "fallback_rules"
    strategy = _rule_based_strategy(research_question)
    if has_chinese and not _english_query_ok(
        strategy.get("english_query")
    ):
        diagnostics["status"] = "translation_failed"
    return strategy, diagnostics


def _rule_based_strategy(research_question: str) -> dict:
    """规则引擎：基于学术词典的关键词提取与策略生成"""
    
    has_chinese = bool(re.search(r"[\u4e00-\u9fff]", research_question))
    
    if has_chinese:
        # 中文：先提取子主题，再翻译成英文
        segments = _extract_key_terms(research_question)
        eng_query = _cn_to_en(research_question)
        if not _english_query_ok(eng_query):
            eng_query = ""
        
        # 生成多组检索词（不同角度）
        queries = []
        
        # 主检索词：完整翻译
        if eng_query:
            queries.append({
                "query": eng_query,
                "sources": ["crossref", "semantic_scholar"],
                "weight": 1.0,
            })
        
        # 如果分段后有不同的子主题，生成补充检索
        if len(segments) > 1:
            for seg in segments:
                eng_seg = _cn_to_en(seg)
                if _english_query_ok(eng_seg) and eng_seg != eng_query:
                    queries.append({
                        "query": eng_seg,
                        "sources": ["crossref", "semantic_scholar"],
                        "weight": 0.8,
                    })
        
        # 补一个短查询（只取前几个核心词）
        core_terms = eng_query.split()[:4]
        if core_terms and len(core_terms) < len(eng_query.split()):
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
