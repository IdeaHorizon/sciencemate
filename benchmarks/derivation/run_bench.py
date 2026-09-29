"""两臂驱动：同样的任务，一边裸模型，一边过 derivation 节点。

## 公平性是这个文件的主要设计目标

对照实验最容易出的错是**把机制之差测成格式之差**：裸模型没被告知要列假设、
没被告知输出格式，于是它"漏了假设"——那测的是 prompt 工程，不是 harness。

所以 A0 的 prompt 里给足三样，与节点 system_prompt 承诺的口径一致：
  · 任务全文（与 A1 的 fixture 同一份 prompt 字段）
  · 明确要求列出用到的全部假设
  · 明确要求交代哪些步骤没被机械验证
  · 固定的 JSON 输出格式

**剩下的差异才是 harness 的效应。** A0 拿到的指令甚至比 A1 更直白——
A1 的模型要自己从 system_prompt 和工具反馈里学会这些，A0 是被直接告知的。
这个偏向对 harness 不利，是刻意的：宁可低估自己。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
REPO = ROOT.parent.parent

#: A0 的单轮输出预算。**必须与 A1 可比**：A1 是多轮的，每轮 16384，
#: 总量远超单轮。给 A0 一个小预算等于用"输出被切断"冒充"能力不行"。
#: 首跑实测：8000 不够 —— 推理模型把预算全花在 reasoning_content 上，
#: content 返回空串（tokens=8406，raw_text 长度 0）。
_A0_MAX_TOKENS = 32000

#: 两臂共用的输出契约。A0 靠它，A1 从 derivation_log 机械提取成同一形状。
_SUBMISSION_SPEC = """
最后用一个 ```json 代码块给出结论，字段固定：

{
  "final_expression": "主结果的最终表达式（Python/sympy 语法，如 k_B*x**2*exp(x)/(exp(x)-1)**2）；若命题被证伪，这里填**正确的**表达式，没有就填空串",
  "verdict": "derived | refuted | inconclusive",
  "assumptions": ["推导过程中用到的每一条假设，一条一项（包括你顺手用掉的：分母非零、级数收敛、独立性、适用域……）"],
  "unverified_steps": ["哪些步骤你没有做机械验证（只靠文字论证或引用），列出来；全都验过就填空数组"],
  "reasoning_summary": "两三句话说明结论怎么来的"
}
"""


def load_task(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _extract_json(text: str) -> dict | None:
    """从模型回复里挖出 JSON。容忍 ```json 围栏、裸对象、前后废话。"""
    if not text:
        return None
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    for blob in reversed(fenced):          # 取最后一个（通常是结论）
        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            continue
    # 没有围栏就找最后一个平衡的花括号块
    depth, start = 0, None
    best = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                best = text[start:i + 1]
    if best:
        try:
            return json.loads(best)
        except json.JSONDecodeError:
            return None
    return None


# ── A0：裸模型 ──────────────────────────────────────────────────────────────

def run_bare(task: dict, model: str) -> dict:
    import httpx

    key = os.environ.get("LLM_API_KEY") or _env_from_dotenv("LLM_API_KEY")
    base = (os.environ.get("LLM_BASE_URL")
            or _env_from_dotenv("LLM_BASE_URL") or "https://api.deepseek.com")
    prompt = (
        f"{task['prompt']}\n\n"
        "要求：\n"
        "1. 一步一步推导，每步说清凭什么走这一步。\n"
        "2. **列出你用到的每一条假设** —— 包括顺手用掉的那些"
        "（分母非零、级数收敛、独立性、展开的适用域……）。\n"
        "3. **交代哪些步骤你没有做机械验证**（只靠文字论证或引用文献）。\n"
        "4. 如果题目里的命题是错的，如实证伪并给出正确结论。\n"
        f"{_SUBMISSION_SPEC}"
    )
    t0 = time.time()
    try:
        resp = httpx.post(
            f"{base.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            json={"model": model, "max_tokens": _A0_MAX_TOKENS,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=900.0)
        resp.raise_for_status()
        body = resp.json()
        choice = body["choices"][0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        # 推理模型把思考放 reasoning_content、结论放 content。只当诊断信息用，
        # **不拿它当答案** —— 思考不是提交。
        thinking = message.get("reasoning_content") or ""
        finish = choice.get("finish_reason") or ""
        usage = body.get("usage") or {}
    except Exception as exc:
        return {"task_id": task["id"], "arm": "A0", "error": f"{type(exc).__name__}: {exc}"}

    # ⚠️ 截断 ≠ 能力不足。把被 max_tokens 切断的一次调用记成"模型没答对"，
    # 整个对比就是假的 —— 那测的是输出预算。基建故障必须与能力分开记账
    # （judge 侧有 test_a_crashed_run_is_an_error_not_a_zero 配套）。
    if finish == "length" or (not text.strip() and thinking):
        return {"task_id": task["id"], "arm": "A0", "model": model,
                "error": (f"输出被截断（finish_reason={finish or '空 content'}，"
                          f"预算 {_A0_MAX_TOKENS}，用了 {usage.get('total_tokens')}）"
                          " —— 不计入能力对比"),
                "finish_reason": finish, "tokens": usage.get("total_tokens"),
                "thinking_chars": len(thinking),
                "elapsed_s": round(time.time() - t0, 1)}

    sub = _extract_json(text) or {}
    return {
        "task_id": task["id"], "arm": "A0", "model": model,
        "final_expression": sub.get("final_expression", ""),
        "verdict": sub.get("verdict", ""),
        "assumptions": sub.get("assumptions", []),
        "unverified_steps": sub.get("unverified_steps"),
        "reasoning_summary": sub.get("reasoning_summary", ""),
        "raw_text": text[:6000],
        "thinking_chars": len(thinking),
        "finish_reason": finish,
        "turns": 1, "tool_calls": 0,
        "elapsed_s": round(time.time() - t0, 1),
        "tokens": usage.get("total_tokens"),
        "parse_ok": bool(sub),
    }


def _env_from_dotenv(name: str) -> str | None:
    f = REPO / ".env"
    if not f.exists():
        return None
    for line in f.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip()
    return None


# ── A1：过 derivation 节点 ──────────────────────────────────────────────────

def run_node_arm(task: dict, timeout_s: int = 2400) -> dict:
    """把任务写成临时 fixture 跑 run_node，再从冻结产物提取同形 submission。

    ⚠️ ground_truth 必须剥掉 —— 它是判分用的答案，进了 fixture 就是泄题。
    """
    fixture = {k: v for k, v in task.items()
               if k in ("node_inputs", "upstream_artifacts", "memory_entries")}
    fixture.setdefault("node_inputs", {})["task_prompt"] = task["prompt"]

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False,
                                     encoding="utf-8") as fh:
        yaml.safe_dump(fixture, fh, allow_unicode=True)
        fixture_path = fh.name

    t0 = time.time()
    try:
        proc = subprocess.run(
            [sys.executable, "run_node.py", "--harness", "derivation",
             "--fixture", fixture_path, "--sandbox", "--no-interactive"],
            cwd=REPO, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return {"task_id": task["id"], "arm": "A1",
                "error": f"timeout after {timeout_s}s"}
    finally:
        os.unlink(fixture_path)

    run_dir = _latest_run_dir()
    if run_dir is None:
        return {"task_id": task["id"], "arm": "A1",
                "error": f"找不到 run 目录；exit={proc.returncode}",
                "stderr_tail": proc.stderr[-800:]}
    return _submission_from_run(task, run_dir, round(time.time() - t0, 1))


def _latest_run_dir() -> Path | None:
    out = REPO / "output"
    dirs = [d for d in out.glob("*/") if (d / "transcript.jsonl").exists()]
    return max(dirs, key=lambda d: d.stat().st_mtime) if dirs else None


def _submission_from_run(task: dict, run_dir: Path, elapsed: float) -> dict:
    """从 derivation_log 机械提取 —— 与 A0 同形，且**不做任何美化**。

    提取规则刻意保守：拿不到就留空，不去别处找补。judge 判的是产物里
    真实写了什么，不是我能从 transcript 里挖出什么。
    """
    log = None
    for f in (run_dir / "artifacts").glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("type") == "derivation_log":
            log = data
            break

    events = []
    tpath = run_dir / "transcript.jsonl"
    if tpath.exists():
        for line in tpath.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    tool_calls = sum(1 for e in events if e.get("event") == "tool_call")
    turns = max((e.get("turn") or 0) for e in events) if events else None
    gate = [e for e in events if e.get("event") == "derivation_pre_freeze_gate"]
    derived = (gate[-1].get("derived") or {}) if gate else {}

    if log is None:
        return {"task_id": task["id"], "arm": "A1",
                "error": "run 没有产出 derivation_log",
                "turns": turns, "tool_calls": tool_calls,
                "elapsed_s": elapsed, "run_dir": str(run_dir)}

    meta = log.get("metadata") or {}
    steps = meta.get("steps") or []

    # 主结果**读节点显式声明的那个**。
    # 第一版从"末步 claim 的等号右边"猜，拿到的是
    # `(x/2)²/sinh²(x/2)（附加，独立路线；非承重）` —— 模型在链尾加了附加路线，
    # 于是 correctness 判成 0%，差点得出"harness 让模型算得更差"的假结论。
    # 猜末步是判据错误，不是提取不够聪明：**别让下游猜，让上游声明。**
    main = meta.get("main_result") or {}
    final = str(main.get("expression") or "").strip()
    final_source = "main_result"
    if not final:
        # ⚠️ 2026-08-23 删掉了"从末步 claim 猜"的退路。
        #
        # `expression` 留空是**合法且常见**的：存在性命题、终止性证明、
        # "该命题不成立"都没有可写的单一闭式。节点引导明确教了这一点，
        # 而真跑里模型正是这么做的 —— 它留空并写了 note 说明为什么。
        #
        # 结果是 driver 走进猜的退路，从末步 claim 的等号右边切了半句中文出来：
        #   "0 且 b >= 0，此时次可加性成立（derived，依据 S4-S9）……"
        # 一句被截断的散文进了报告的 final_expression 字段。
        #
        # 这跟 `main_result` 这个字段存在的**全部理由**是矛盾的：
        # 「别让下游猜，让上游声明」。上游诚实地声明了"没有表达式"，
        # 下游却把它当成"没声明"，然后自己发明了一个。
        # **测量工具不该发明数据** —— 上游说没有，报告里就是没有。
        final_source = "declared_empty" if isinstance(main, dict) and main else "absent"

    verdicts = meta.get("verdicts")
    verdict_text = json.dumps(verdicts, ensure_ascii=False) if verdicts else ""
    if "refut" in verdict_text.lower() or "证伪" in verdict_text:
        verdict = "refuted"
    elif "deriv" in verdict_text.lower():
        verdict = "derived"
    else:
        verdict = verdict_text[:60]

    # 验证声称的**独立证据**：每条 verification 章上的 probe（式子指纹）都能
    # 在本 run 的 derivation_check 账本里反查到。这是"报告不是事实"落成的数据 ——
    # A0 提供不了同类东西，不是因为它算得差，是因为它的声称只有自述。
    evidence = []
    ledger_probes = {e.get("probe") for e in events
                     if e.get("event") == "derivation_check"}

    def _verification_of(step: dict) -> dict:
        """step 的验证章 —— **不是 dict 的一律当没有**。

        2026-08-23：模型把 verification 写成字符串 `"verified"`（手抄结论、
        丢掉工具返回的章），driver 在 `ver.get("probe")` 上抛 AttributeError，
        整道题因此一个数都没记下来。**测量工具不能因为被测对象形状异常而失去
        全部数据** —— 那正是最该被记录下来的一次。

        形状异常本身是观测结果，下面单独计数。
        """
        ver = step.get("verification")
        return ver if isinstance(ver, dict) else {}

    malformed = sum(1 for s in steps
                    if s.get("verification") is not None
                    and not isinstance(s.get("verification"), dict))
    for step in steps:
        ver = _verification_of(step)
        probe = ver.get("probe")
        if probe:
            evidence.append({"step": step.get("id"), "probe": probe,
                             "status": ver.get("status"),
                             "in_ledger": probe in ledger_probes})
    claimed_verified = [s for s in steps
                        if str(_verification_of(s).get("status") or "")
                        in ("verified", "numerically_supported")]

    return {
        "task_id": task["id"], "arm": "A1",
        "final_expression": final,
        "final_expression_source": final_source,
        "main_result_statement": main.get("statement"),
        "verdict": verdict,
        "verification_evidence": evidence,
        "claimed_verified_steps": len(claimed_verified),
        "step_count": len(steps),
        # 形状异常的验证章条数。不为 0 = 这份产物绕过了写入门（历史数据），
        # 报告里必须看得见，否则它会被算成"这一步没验过"而已。
        "malformed_verification_blocks": malformed,
        "assumptions": [
            f"{a.get('id')}: {a.get('statement')}" for a in (meta.get("assumptions") or [])
            if isinstance(a, dict)],
        "unverified_steps": derived.get("unverified_steps"),
        "validity_domain": derived.get("validity_domain"),
        "credibility": meta.get("credibility"),
        "findings": meta.get("findings"),
        "frozen": bool(meta.get("frozen")),
        "turns": turns, "tool_calls": tool_calls,
        "elapsed_s": elapsed, "run_dir": str(run_dir),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", required=True, choices=["A0", "A1", "B0"],
                    help=("A0=flash 裸模型 / A1=flash+derivation 节点 / "
                          "B0=更强模型裸跑（默认 v4-pro）。"
                          "B0 vs A1 回答的是「弱模型+harness 能不能追平强模型裸跑」——"
                          "即 README 里那条 harness 抬下限的主张。"
                          "⚠️ B1（强模型+harness）跑不了：v4-pro 带工具会触发 "
                          "markup 崩坏（记忆在案，8-17 与 8-21 两次实测），"
                          "那是通道级问题不是本节点的锅。"))
    ap.add_argument("--tasks", default="all",
                    help="all 或逗号分隔的 task id")
    ap.add_argument("--model", default=None, help="A0 用哪个模型")
    ap.add_argument("--timeout", type=int, default=2400)
    ap.add_argument("--repeat", type=int, default=1,
                    help=("每题重复跑几次。>1 时结果文件带 __rN 后缀。"
                          "重复是为了测方差：同一道长题跑多次看结果稳不稳定 —— "
                          "单次跑不出方差，而「让错误被当场抓住」这条主张"
                          "恰恰应当表现为方差更小。"))
    args = ap.parse_args()

    model = args.model or ("deepseek-v4-pro" if args.arm == "B0"
                           else _env_from_dotenv("LLM_MODEL") or "deepseek-v4-flash")
    tasks = [load_task(f) for f in sorted((ROOT / "tasks").glob("*.yaml"))]
    if args.tasks != "all":
        want = {t.strip() for t in args.tasks.split(",")}
        tasks = [t for t in tasks if t["id"] in want]
    if not tasks:
        print("没有匹配的任务")
        return 1

    results_dir = ROOT / "results"
    results_dir.mkdir(exist_ok=True)

    total = len(tasks) * max(1, args.repeat)
    done = 0
    for task in tasks:
        for rep in range(max(1, args.repeat)):
            done += 1
            suffix = f"__r{rep+1}" if args.repeat > 1 else ""
            print(f"[{done}/{total}] {args.arm} {task['id']}{suffix} …", flush=True)
            result = (run_node_arm(task, args.timeout) if args.arm == "A1"
                      else run_bare(task, model))
            result["repeat_index"] = rep + 1
            result["arm"] = args.arm      # run_bare 里写死了 A0，按实际臂改回
            out = results_dir / f"{task['id']}__{args.arm}{suffix}.json"
            out.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            flag = "ERROR " + result["error"][:60] if result.get("error") else \
                f"verdict={result.get('verdict','')[:18]} turns={result.get('turns')}"
            print(f"      → {flag}  ({result.get('elapsed_s')}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
