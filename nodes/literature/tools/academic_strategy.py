"""Reuse the interpretation of a question while still retrieving fresh papers."""
import hashlib
import json
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

from core.paths import literature_cache_dir
from shared.lib import filelock
from . import strategy_generator

_VERSION = "academic-bilingual-recall-aliases-v20"
_MAX_AGE_SECONDS = 86400


def generate_academic_strategy(question: str) -> tuple[dict, dict]:
    from .bilingual_strategy import PROMPT, recompile_chinese_queries
    config = strategy_generator.LLM_CONFIG

    def generate():
        return strategy_generator.generate_strategy_detailed(question, require_concepts=True, bilingual_plan=True)

    # Missing credentials must not accidentally reuse another account's cache.
    if not config.get("api_base") or not config.get("api_key"):
        return generate()
    identity = json.dumps(
        [_VERSION, hashlib.sha256(PROMPT.encode()).hexdigest(),
         " ".join(question.split()), config.get("api_base", "").rstrip("/"),
         config.get("model"), config.get("api_key")],
        ensure_ascii=False,
    )
    key = hashlib.sha256(identity.encode()).hexdigest()
    directory = literature_cache_dir() / "academic_strategies"
    path = directory / f"{key}.json"
    stack = ExitStack()
    try:
        stack.enter_context(filelock.exclusive(directory / f"{key}.lock"))
    except OSError:
        stack.close()
        return generate()
    with stack:
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            strategy = row["strategy"]
            age = time.time() - float(row["created_at"])
            if (
                row.get("version") == _VERSION
                and 0 <= age < _MAX_AGE_SECONDS
                and isinstance(strategy, dict)
                and strategy.get("search_queries")
                and (strategy.get("bilingual_queries") or not strategy_generator._query_semantic_issues(
                    question, str(strategy.get("english_query") or "")
                ))
            ):
                recompile_chinese_queries(strategy)
                return strategy, {
                    "status": "strategy_cache_hit", "strategy_cache_hit": True,
                    "strategy_cache_age_seconds": round(age, 3),
                    "translation_valid": True, "translation_status": "valid",
                    "translation_attempts": 0,
                }
        except (OSError, ValueError, KeyError, TypeError):
            pass

        strategy, diagnostics = generate()
        diagnostics["strategy_cache_hit"] = False
        if (
            strategy.get("search_queries")
            and (strategy.get("bilingual_queries") or not strategy_generator._query_semantic_issues(
                question, str(strategy.get("english_query") or "")
            ))
            and diagnostics.get("translation_valid", True)
        ):
            try:
                with tempfile.NamedTemporaryFile(
                    "w", encoding="utf-8", dir=directory, delete=False,
                ) as handle:
                    json.dump(
                        {"version": _VERSION, "created_at": time.time(), "strategy": strategy},
                        handle, ensure_ascii=False,
                    )
                    temporary = Path(handle.name)
                temporary.replace(path)
            except OSError:
                pass
        return strategy, diagnostics
