"""平台问知识，走的必须是 harness 自己那份实现。

## 现场

后端曾经有整套自己的 KB/memory 表。桥（platform_runtime）接管执行之后，KB 这半
没跟上：线上 15 个真实项目、那些表 0 行，UI 的知识页因此永远是空的。

第一版修法是在后端写个适配器直接解析 harness 的 JSONL —— 那等于把 `list_kb`
的 scope 路由、project shadow org、id 去重**又实现一遍**，harness 改规则它会
静默分叉。而 `core/api.py` 的模块文档早就写明自己是「平台只读 API —— hf CLI /
未来前端的统一查询入口」：接口本来就有，不该另造。

所以 `op=kb_query` 是一层**转发**，本文件锁的就是"它真的在转发"：
一份数据、一套规则，平台看到的和 agent 写下的必然一致。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records),
                    encoding="utf-8")


@pytest.fixture()
def harness_home(tmp_path):
    home = tmp_path / "hf"
    _write_jsonl(home / "projects" / "demo" / "kb_concepts.jsonl", [
        {"id": "c_proj", "canonical_name": "KA 模型", "concept_type": "method"},
        {"id": "c_dup", "canonical_name": "项目版本", "concept_type": "method"},
    ])
    _write_jsonl(home / "org" / "kb_concepts.jsonl", [
        {"id": "c_org", "canonical_name": "Lennard-Jones", "concept_type": "method"},
        {"id": "c_dup", "canonical_name": "org 版本", "concept_type": "method"},
    ])
    _write_jsonl(home / "projects" / "demo" / "kb_claims.jsonl", [
        {"id": "cl1", "claim_text": "冷却速率降低一个量级，Tg 下降约 2%"},
    ])
    # 记忆只有一个落盘物：MEMORY.md（见 core/memory.py）
    mem = home / "projects" / "demo" / "MEMORY.md"
    mem.parent.mkdir(parents=True, exist_ok=True)
    mem.write_text(
        "# 项目记忆\n\n"
        "<!-- section:goal -->\n测定冷却速率对 Tg 的影响\n<!-- /section:goal -->\n\n"
        "<!-- section:manual_pitfall -->\n"
        "- 单次降温轨迹读不出 Tg，必须扫多个速率\n"
        "  <!-- @ nodes=experiment | run=r1 | seen=1 -->\n"
        "<!-- /section:manual_pitfall -->\n", encoding="utf-8")
    _write_jsonl(home / "projects" / "demo" / "kb_proposals.jsonl", [
        {"id": "p_pending", "content": "待审", "status": "pending"},
        {"id": "p_done", "content": "已处理", "status": "accepted"},
    ])
    return home


def _ask(home: Path, **fields) -> dict:
    request = {"op": "kb_query", "request_id": "t1", "project_id": "demo",
               "home_dir": str(home), **fields}
    proc = subprocess.run(
        [sys.executable, "-m", "platform_runtime"],
        input=json.dumps(request) + "\n", capture_output=True, text=True,
        cwd=str(REPO), timeout=60,
    )
    for line in proc.stdout.splitlines():
        event = json.loads(line)
        if event.get("type") in ("kb_query_result", "error"):
            return event
    raise AssertionError(f"桥没有回结果：{proc.stdout!r} {proc.stderr[-400:]!r}")


def test_bridge_answers_from_the_harness_store(harness_home):
    """事故本体：平台问知识，得问到 agent 真写下的那份。"""
    event = _ask(harness_home, entity="concepts")
    assert event["type"] == "kb_query_result"
    names = {r["canonical_name"] for r in event["records"]}
    assert "KA 模型" in names
    assert "Lennard-Jones" in names, "org 层的跨项目积累也要看得见"


def test_project_shadows_org_exactly_like_the_harness(harness_home):
    """同 id 时项目层优先 —— 这条规则只能有一份实现，不能在平台再写一遍。"""
    records = _ask(harness_home, entity="concepts")["records"]
    dup = [r for r in records if r["id"] == "c_dup"]
    assert len(dup) == 1
    assert dup[0]["canonical_name"] == "项目版本"


def test_search_goes_through_the_same_api(harness_home):
    records = _ask(harness_home, entity="claims", query="冷却速率")["records"]
    assert [r["id"] for r in records] == ["cl1"]


def test_memory_reads_the_single_memory_md(harness_home):
    """记忆只有一个落盘物 —— MEMORY.md 的节，不是候选队列也不是 memory.jsonl。"""
    records = _ask(harness_home, entity="memory")["records"]
    kinds = {r.get("kind") for r in records}
    assert "goal" in kinds
    assert "manual_pitfall" in kinds
    assert any("扫多个速率" in str(r.get("text") or r.get("content") or "")
               for r in records)


def test_proposals_are_the_pending_queue(harness_home):
    records = _ask(harness_home, entity="proposals")["records"]
    assert [r["id"] for r in records] == ["p_pending"]


def test_stats_come_from_kb_stats(harness_home):
    stats = _ask(harness_home, entity="stats")["stats"]
    assert stats["concepts"] == 2      # 项目层文件里的条数
    assert stats["claims"] == 1


def test_unknown_entity_is_refused_not_guessed(harness_home):
    event = _ask(harness_home, entity="nonsense")
    assert event["type"] == "error"


def test_the_environment_cannot_move_the_org_layer(harness_home, monkeypatch):
    """环境里剩下的 `HARNESS_FRAMEWORK_ORG_HOME` 不许把这次请求的 org 层拽走。

    原来这条叫 `test_org_layer_follows_the_bound_home`，判据是"桥必须把 org 层绑成
    `home_dir/org`"。它防的事是对的（外部变量把一个人的跨项目知识写进别处），防的
    办法在 2026-09-16 被证明太紧：org 层是**这台安装**的、要跨人共享，而"绑成
    home_dir/org"恰恰是 node20 上 12 个人各自一个私有 org 层的成因。

    所以判据换了一句话问：**谁说了算**。说了算的是请求（`org_home` 字段），不是
    环境 —— 环境变量分不清「App Server 决定的」和「环境里剩下的」，而参数分得清。
    这条锁住"环境说了不算"，下一条锁住"请求说了算"。
    """
    monkeypatch.setenv("HARNESS_FRAMEWORK_ORG_HOME", "/tmp/somewhere-else-entirely")
    names = {r["canonical_name"] for r in _ask(harness_home, entity="concepts")["records"]}
    assert "Lennard-Jones" in names, "环境变量把 org 层拽走了 —— 桥不该嗅环境"


def test_the_request_decides_where_the_org_layer_is(harness_home, tmp_path):
    """请求说 org 层在哪，桥就读哪 —— 这是「一台安装一个 org 层」的落点。

    App Server 每次都把 `config.the_org_home()` 放进请求；于是同一台服务器上两个
    用户、两个 home，读到的是同一份晋升上来的知识。改这条之前，node20 上 12 个人
    的 16 条 claim 互相看不见。
    """
    shared = tmp_path / "installation-org"
    _write_jsonl(shared / "kb_concepts.jsonl", [
        {"id": "c_shared", "canonical_name": "共享层里的东西", "concept_type": "method"},
    ])
    names = {r["canonical_name"]
             for r in _ask(harness_home, entity="concepts", org_home=str(shared))["records"]}
    assert "共享层里的东西" in names, "请求指定的 org 层没被读到"
    assert "Lennard-Jones" not in names, "还在读 home 自己的 org/ —— 请求说了不算"


def test_project_without_sediment_is_empty_not_an_error(tmp_path):
    event = _ask(tmp_path / "empty-home", entity="claims")
    assert event["type"] == "kb_query_result"
    assert event["records"] == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
