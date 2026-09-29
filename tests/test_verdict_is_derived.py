"""证据可以持久化，判决不可以（E2E-5b 2026-08-03 死锁的根因）。

## 范畴错误

系统里有两个平行的"这个 run 成没成"，按构造必然漂移：

  `status`           finalize 那一刻用**当时的规则**盖的章，写进 summary.json 后永不更新
  `failure_signals`  每次读都按**当前规则**重算

于是每改进一条判定规则（今天改了两条：#259 judge 非法 JSON、#273 judge 截断
都不算节点的错），全部历史记录的 status 就悄悄作废一批 —— 而没人知道。

## 现场

writing run 1785726557-3743e6：手稿 36,618 字节存好、PDF 编好、12 项 QC 过 11
项，唯一挂的那项是 judge 把 2000 输出预算全烧在思维链上（completion=2000 /
reasoning=2000 / content=""，平台故障）。当时记 `status=incomplete`。

规则修好后 `failure_signals` 已经是**空的**，但那个章还在：

    08-03 11:09  status=incomplete   failure_signals=∅   ← 冤案
    熔断判据      → 仍触发 ['missing:manuscript', ...]（数的是 08-02 的三次旧失败）

`/continuous on` 一发就被同一段旧账重新掐死，无解循环 —— 5b 停摆的真因。

最尖锐的证据：`consecutive_failures` **在同一个循环里混用两套认识论** ——
断链读 `is_completed`（冻结的判决），计数读 `failure_signals`（活的事实）。

## 修法

不是再去通知一个消费者（那是打补丁，今天已经打了三次：统计层 #259、
完成度 #273、断链层）。而是把判决改成从证据现算：规则每次改进，全部历史
**自动重判**，不需要迁移脚本、不需要人工解锁、不需要挨个通知消费者。

复核是**不对称**的，对应两个方向截然不同的风险：
  completed  → 一律维持（推翻旧的成功判决会静默放行本该拦的 run）
  incomplete → 才复核（维持冤案只是保守，平反它只会纠错）

且只推翻**能完整解释**的依据 —— 编排闭环那道门照旧维持。
"""
from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest

from core import run_history
from core.bootstrap import bootstrap
from core.run_history import RunRecord

bootstrap()


def _rec(**kw) -> RunRecord:
    base = dict(run_id="1785726557-3743e6", state_dir=None, node_type="writing",
                status="incomplete")
    base.update(kw)
    return RunRecord(**base)


# ── 现场回放 ────────────────────────────────────────────────────────────────

def test_replays_e2e5b_the_wrongly_convicted_run():
    """E2E-5b 原形：11/12 QC 过、PDF 编好，唯一失败是 judge 自己崩了。

    旧代码盖的章是 incomplete；按当前规则复核 → 它本来就是成功的。
    """
    # QC 层删除（2026-08-22）后，legacy summary 里的 failed_quality_checks
    # 在加载层就被忽略 —— 这类冤案按构造不可能再产生。保留场景：章是
    # incomplete、按当前规则零可归因失败 → 自动平反。
    r = _rec()

    assert r.failure_signals == set(), "前提：按当前规则零可归因失败"
    assert r.status == "incomplete", "前提：文件里那个章不动"
    assert r.is_completed is True, "判决必须现算 —— 冤案要能自动平反"


def test_stale_verdict_no_longer_locks_the_breaker():
    """判决活过来 → 熔断链自动断 → 5b 不用改任何数据就能恢复。

    修前：08-02 三次真失败 + 中间一次零信号的 run 被 `continue` 跳过 →
    仍算"连续"→ /continuous on 一发即死。
    """
    def bad(rid):
        return RunRecord(run_id=rid, state_dir=None, node_type="writing",
                         status="incomplete",
                         missing_required_outputs=("manuscript",))

    exonerated = _rec(run_id="1785726557-3743e6")

    # load_runs 返回的是最新在前
    runs = [exonerated, bad("1785600000-c"), bad("1785500000-b"), bad("1785400000-a")]
    assert run_history.consecutive_failures(runs, "writing") is None, \
        "被平反的 run 必须断链 —— 否则旧账永远锁死这个节点"


def test_breaker_no_longer_mixes_two_epistemologies():
    """同一个判断不许一半读冻结的章、一半读活的事实。"""
    src = inspect.getsource(run_history.consecutive_failures)
    assert "is_completed" in src and "failure_signals" in src
    assert "self.status" not in src, "断链不许直接读 status"


# ── 不对称：只平反，不推翻 ──────────────────────────────────────────────────

def test_completed_verdict_is_never_overturned():
    """盖过的成功章一律维持 —— 推翻它会静默放行本该拦的 run。"""
    r = _rec(status="completed", missing_required_outputs=("manuscript",))
    assert r.is_completed is True


def test_real_failure_stays_failed():
    r = _rec(missing_required_outputs=("manuscript",))
    assert r.is_completed is False


def test_missing_output_stays_failed():
    """缺必需产出 → failure_signals 含 missing:*，照旧不通过。"""
    r = _rec(missing_required_outputs=("manuscript",))
    assert r.is_completed is False


def test_unevaluated_run_is_not_exonerated():
    """崩掉的 run（catch-all 收尸，status=error）没经过收尾评估 ——
    没有证据就没有复核。"""
    r = _rec(status="error")
    assert r.was_evaluated is False
    assert r.is_completed is False, "空账本不等于清白"


def test_closure_downgrade_is_not_overturned():
    """编排闭环没闭是另一道门，与 QC 无关 —— 复核绝不能变成"看不懂就当没有"。"""
    r = _rec(node_type="_orchestrator",
             raw={"orchestration_closure": {"downgrades_status": True,
                                            "open_item_count": 3}})
    assert r.failure_signals == set(), "前提：契约层面确实干净"
    assert r.closure_downgraded is True
    assert r.is_completed is False, "闭环门禁必须扛住复核"


def test_closure_present_but_closed_does_not_block():
    r = _rec(raw={"orchestration_closure": {"downgrades_status": False}})
    assert r.is_completed is True


# ── 防漂移：降级来源必须登记 ────────────────────────────────────────────────

def test_no_unregistered_downgrade_source():
    """机械数 executor 里把 status 降级为 incomplete 的赋值点。

    新增一个却不登记进 _INCOMPLETE_GROUNDS，复核就会把那道新门当作"看不懂
    就当没有"静默放行 —— 正是本次要根除的缺陷的镜像。加了不登记，这条红。
    """
    src = Path(inspect.getfile(run_history)).parent / "executor.py"
    # 只数**真赋值**：注释里提到这个字符串不算（本条守卫自己就先踩了一次 ——
    # 我写的告警注释里含 `final_status = "incomplete"`，第一版正则把它数了进去）。
    code = [ln for ln in src.read_text(encoding="utf-8").splitlines()
            if not ln.lstrip().startswith("#")]
    sites = re.findall(r'^\s*final_status\s*=\s*(?:"completed"\s*if.*?else\s*)?"incomplete"',
                       "\n".join(code), re.MULTILINE)
    assert len(sites) == len(run_history._INCOMPLETE_GROUNDS), (
        f"executor 里有 {len(sites)} 个降级点，_INCOMPLETE_GROUNDS 登记了 "
        f"{len(run_history._INCOMPLETE_GROUNDS)} 个。新增降级来源必须同时登记，"
        f"并教会 reevaluated_success 认它。")


def test_grounds_are_documented_not_just_counted():
    assert "orchestration_closure" in run_history._INCOMPLETE_GROUNDS
    assert "quality_or_missing" in run_history._INCOMPLETE_GROUNDS


# ── 真磁盘接缝：load_runs 出来的记录也要现算 ────────────────────────────────

def test_derived_verdict_survives_the_disk_round_trip(tmp_path):
    """本周两次教训：测了内部对象、没测真加载路径。这条走 load_runs。"""
    proj = tmp_path / "p"
    d = proj / "1785726557-3743e6"
    d.mkdir(parents=True)
    (d / "summary.json").write_text(json.dumps({
        "node_type": "writing", "project_id": "p", "status": "incomplete",
        "failed_quality_checks": ["manuscript_no_unverified_details"],
        "missing_required_outputs": [],
        "quality_check_results": [
            {"name": "manuscript_pdf_compiled", "passed": True},
            {"name": "manuscript_no_unverified_details", "passed": False,
             "failure_kind": "judge_output_truncated"}],
    }, ensure_ascii=False), encoding="utf-8")

    runs = run_history.load_runs(proj, project_id="p")
    assert len(runs) == 1
    r = runs[0]
    assert r.status == "incomplete", "磁盘上的章一个字节不动（审计要看得到原判）"
    # QC 层删除后 legacy 的 failed_quality_checks 在加载层被忽略 ——
    # 全部历史上被 judge 误判的 run 一次性自动平反，无迁移脚本。
    assert r.is_completed is True, "但决策读到的是按当前规则现算的判决"


def test_disk_closure_downgrade_survives_round_trip(tmp_path):
    proj = tmp_path / "p2"
    d = proj / "1785726000-abcdef"
    d.mkdir(parents=True)
    (d / "summary.json").write_text(json.dumps({
        "node_type": "_orchestrator", "project_id": "p2", "status": "incomplete",
        "failed_quality_checks": [], "missing_required_outputs": [],
        "quality_check_results": [{"name": "x", "passed": True}],
        "orchestration_closure": {"downgrades_status": True, "open_item_count": 2},
    }, ensure_ascii=False), encoding="utf-8")

    r = run_history.load_runs(proj, project_id="p2")[0]
    assert r.closure_downgraded is True
    assert r.is_completed is False


# judge_* 冤案参数化已删除（2026-08-22）：QC judge 不存在了，该冤案类别
# 按构造灭绝 —— 上面的 legacy 磁盘往返测试覆盖了历史数据的自动平反。
