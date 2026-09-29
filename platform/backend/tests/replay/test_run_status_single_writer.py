"""真实历史回放：`run.status` 只能由投影器写（RFC 异步运行时 D11）。

## 为什么是回放，不是单测

RFC 的验收条款写死了：

    验收必须是真实历史回放 —— 单测造不出这个形状（要连着 pause/resume/
    多轮 turn 才显形）。

它说的是实话。8-21 那条 run 收到**两次**终态、8-21 晚那条 run「出生 9 秒即被
判死」，都不是某一次调用的返回值错了，而是**一条 run 一生的状态序列**在多个
写者之间被交替覆盖。构造单测时人会不自觉地只造自己想到的那条路径
（「单测只验我想到的失败方式」），而这类缺陷恰恰活在没想到的那条上。

所以判据取自真实现场：`scripts/export_run_replay.py` 从库里导出一条 run 的
生命周期事件 + 落库状态，两者的**差集**就是证据。

## 不变量

    run.status 的每一个取值，都必须能在这条 run 自己的事件流里找到依据。

投影器（`execution_ingest`）把 worker 说的话翻译成 status —— 那是转述事实。
平台在投影器之外直接写 status，写的是**判决**（「我猜它没主了」）。判决写进
事实字段，错了也没人知道，而下游已经按它行动过了（[[证据可持久化，判决不可以]]）。

`born-dead` 这条把后果摆得最清楚 —— 同一行里两个互相矛盾的说法并存：

    status  = running                          ← 后来被改回来的事实
    summary = staleReason: app_server_restart  ← 当时平台下的判决
              resumable: false
    事件流  = run.started / completed / paused / resumed …
              **没有任何一条 run.status_unknown**

那个 `stale_unknown` 从头到尾没有事件依据。它把一条正在正常推进的研究（子节点
hypothesis→_reviewer→observation 全跑完了）在 UI 上判成了「这一轮没跑完」，
而且前端据此停止轮询五分钟。

## 为什么迁移做完了这两份仍然 xfail

**语料是迁移前的历史现场，代码改了不会让历史数据自己变。** 这两条 run 身上那些
无依据的 status 是 2026-08-21 当天写下的，永远留在那里 —— 它们的价值是**记住
病长什么样**，不是证明今天的代码对不对。

"今天的代码还会不会这么写"由另一道闸回答：`tests/test_run_status_has_one_writer.py`
（AST 扫盘，断言 `run.status` 的写点只剩投影器 + 两处写明理由的转述）。两者分工：

- 扫盘闸：**源码**层面，写点唯一 —— 防的是有人再加一个写者；
- 回放台：**数据**层面，历史现场留档 —— 防的是"这个病是什么样"被忘掉，
  以及迁移后新捕获的现场用同一套判据复验。

迁移后的现场要等部署到 node20 之后再用 `scripts/export_run_replay.py` 采一份
（`post-d11-*.json`），那一份必须**当场通过**，不进 KNOWN_D11_VIOLATIONS。
"""
from __future__ import annotations

import json
import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

#: 平台在投影器之外下判决时留在 summary 里的痕迹。
PLATFORM_VERDICT_FIELDS = ("staleReason", "staleFromStatus", "staleDetectedAt", "pauseAbandonedAt")

#: 事件 → 它能为哪个 status 提供依据。投影器的映射表（execution_ingest）。
EVENT_JUSTIFIES = {
    "run.started": {"running"},
    "run.completed": {"completed", "completed_with_warning"},
    "run.failed": {"failed"},
    "run.cancelled": {"cancelled"},
    "run.incomplete": {"incomplete"},
    "run.paused": {"waiting_human", "waiting_permission"},
    "run.resumed": {"running"},
    "run.retrying": {"retrying", "running"},
    "run.blocked": {"blocked", "running"},
    "run.status_unknown": {"stale_unknown"},
    "run.queued": {"queued"},
    "run.recovering": {"running", "retrying"},
}


def _cases() -> list[pathlib.Path]:
    return sorted(FIXTURES.glob("*.json"))


def _unjustified(case: dict) -> list[str]:
    """这份现场里，找不到事件依据的 status 取值。"""
    justified: dict[str, set[str]] = {}
    for event in case.get("lifecycle") or []:
        justified.setdefault(event["runId"], set()).update(
            EVENT_JUSTIFIES.get(event["kind"], set()))
    findings: list[str] = []
    for run in case.get("runs") or []:
        seen = justified.get(run["id"], set())
        summary = run.get("summary") or {}
        # 落库的终态本身。
        if run["status"] not in seen:
            findings.append(f"{run['nodeType']}: status={run['status']} 无事件依据")
        # summary 里留下的判决痕迹 —— 平台动过手的直接证据。
        verdicts = {k: summary[k] for k in PLATFORM_VERDICT_FIELDS if summary.get(k) is not None}
        if verdicts and "stale_unknown" not in seen:
            findings.append(f"{run['nodeType']}: 平台判决 {verdicts} 但事件流里没有 run.status_unknown")
    return findings


#: D11 迁移**之前**采到的现场，作为病例留档。历史数据不会因为代码改了而改变，
#: 所以它们永远 xfail —— 新采的现场不许进这张表（进了就是迁移没做对）。
#: 不在这张表里的用例必须当场通过 ——
#: 一个把所有输入都判成违规的护栏，等于没有护栏。`provider-connect-lost`
#: 就是阴性对照：它真的失败了，而且 status 有 `run.failed` 事件撑着，合法。
KNOWN_D11_VIOLATIONS = {"born-dead", "deploy-interrupted"}


def _expected_xfail(path: pathlib.Path):
    return pytest.mark.xfail(
        strict=True, reason="迁移前采到的历史现场：当时平台的猜测直接写进了 run.status",
    ) if path.stem in KNOWN_D11_VIOLATIONS else ()


@pytest.mark.parametrize(
    "path", [pytest.param(p, marks=_expected_xfail(p)) for p in _cases()],
    ids=lambda p: p.stem,
)
def test_every_recorded_status_has_an_event_behind_it(path: pathlib.Path) -> None:
    case = json.loads(path.read_text(encoding="utf-8"))
    findings = _unjustified(case)
    assert not findings, (
        f"{path.stem}：{len(findings)} 处 status 找不到事件依据 —— "
        f"投影器之外有人写了 run.status\n  " + "\n  ".join(findings)
    )


def test_the_replay_corpus_actually_contains_the_known_incidents() -> None:
    """用例本身要能证明它抓的是真现场，不是空文件。

    没有这条，上面那个 xfail 在 fixtures 被清空时会"通过"（无用例可跑），
    而护栏消失是静默的 —— 空集合上一切断言都成立，这是护栏最常见的死法。
    """
    cases = _cases()
    assert len(cases) >= 3, f"回放语料少于 3 份现场：{[p.stem for p in cases]}"
    total = sum(len(json.loads(p.read_text(encoding='utf-8')).get("lifecycle") or []) for p in cases)
    assert total >= 50, f"语料里只有 {total} 条生命周期事件，不足以构成回放"
    born_dead = json.loads((FIXTURES / "born-dead.json").read_text(encoding="utf-8"))
    findings = _unjustified(born_dead)
    assert findings, "born-dead 这份现场必须仍能复现出「无依据的 status」"
