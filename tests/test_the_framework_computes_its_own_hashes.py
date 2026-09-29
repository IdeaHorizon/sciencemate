"""摘要归框架算 —— 别逼模型 shell 出去拿一个纯机械的数。

## 病例（2026-09-07 真机，二维 Ising，100 个 npz）

冻结 raw_results manifest 要求逐文件 `sha256` + `bytes`。模型手里没有算它们的路，
只能 `safe_run_bash` 出去算 —— 三次都被执行路线闸拒（同一动作同一原因达上限），
于是手被彻底捆住：

    阻塞点：safe_run_bash 计算 100 个 npz 的 sha256 被 route gate 拒绝
           （同一动作已被同一原因拒绝 3 次，达上限），无法执行任何脚本/命令
    决策：不编造 source_hashes —— contract 要求真实 64-hex，伪造哈希会违反
         freeze contract，选择诚实标注而非形式完美

一趟本已算完的科研（Tc 已测得、图已出）卡在"我够不到一个纯机械的数"上，最后交了
一份空 `source_hashes`。

## 判据的形状

那道路线闸没错 —— 它管"这一轮计算按声明的路线跑"。**算摘要不是计算实验，是记账。**
框架冻结时本来就要逐字节重算这批摘要来核对，手里握着答案却只在对不上时才报。

所以这里测的是：框架给得出这个数、给的是真数、边界不漏、出口有界、半成功要吭声。
`role` / `retention` 那种"这个文件算不算必留"仍归模型 —— 不在这个工具里。
"""
from __future__ import annotations

import hashlib

import pytest

from core.tool_registry import execute, get_tool
import shared.tools  # noqa: F401  注册工具


class _State:
    def __init__(self, root):
        self.root = root
        self.project_worktree = None
        self.run_id = "r1"
        self.node_type = "experiment"


@pytest.fixture
def state(tmp_path):
    return _State(tmp_path)


async def _call(state, **kw):
    """走真入口 `execute()` —— 生产就是从这儿进来的。

    直接抓私有 executor 会绕过参数校验、见证记账、出口扫描那一整层，
    测的就不是模型真正会撞上的那条路。
    """
    return await execute("hash_files", state, **kw)


@pytest.mark.asyncio
async def test_it_hands_back_the_real_digest(state, tmp_path) -> None:
    """核心：给的必须是真摘要，不是占位、不是长度对的假串。"""
    f = tmp_path / "run_0.npz"
    f.write_bytes(b"\x89NPZ\x00binary payload")
    expected = hashlib.sha256(f.read_bytes()).hexdigest()

    out = await _call(state, paths=[str(f)])

    assert out["status"] == "ok" and out["count"] == 1
    row = out["files"][0]
    assert row["sha256"] == expected, "摘要不是真算的"
    assert row["bytes"] == f.stat().st_size
    assert len(row["sha256"]) == 64


@pytest.mark.asyncio
async def test_a_hundred_files_in_one_call(state, tmp_path) -> None:
    """真机就是 100 个 npz —— 这个规模必须一次调用拿得到。"""
    made = []
    for i in range(100):
        f = tmp_path / f"s_{i}.npz"
        f.write_bytes(f"payload-{i}".encode())
        made.append(str(f))

    out = await _call(state, paths=made)

    assert out["status"] == "ok" and out["count"] == 100
    assert len({r["sha256"] for r in out["files"]}) == 100, "不同内容出了相同摘要"


@pytest.mark.asyncio
async def test_it_never_reads_outside_the_project(state, tmp_path) -> None:
    """只读本流水线自己的文件。越界要拒，而且要说清越到哪去了。"""
    outside = tmp_path.parent / "not_ours.txt"
    outside.write_text("secret")
    state.project_worktree = tmp_path       # 边界 = 这个工作区

    out = await _call(state, paths=[str(outside)])

    assert out["status"] == "error"
    assert "不在本项目工作区" in out["error"], out["error"]


@pytest.mark.asyncio
async def test_the_output_is_bounded_and_says_how_to_proceed(state, tmp_path) -> None:
    """超量要**拒**，不许静默截断。

    截断的代价很隐蔽：下游覆盖检查会报"漏了几个文件"，而真因是这里少给了。
    拒的同时必须告诉模型怎么办 —— 分批，而不是少声明几个文件。
    """
    from shared.tools.library.file_digests import _MAX_FILES

    out = await _call(state, paths=[str(tmp_path / f"x{i}") for i in range(_MAX_FILES + 1)])

    assert out["status"] == "error"
    assert "分批" in out["error"], "只说了拒，没说怎么往下走"
    assert "不要减少要声明的文件数" in out["error"], (
        "没堵住那条捷径 —— 模型会以为『少声明几个就过了』"
    )


@pytest.mark.asyncio
async def test_a_partial_success_is_reported_not_swallowed(state, tmp_path) -> None:
    """算成一半要吭声：少一行摘要，冻结时的覆盖检查就会拒。"""
    good = tmp_path / "ok.npz"
    good.write_bytes(b"data")

    out = await _call(state, paths=[str(good), str(tmp_path / "missing.npz")])

    assert out["status"] == "ok" and out["count"] == 1
    assert out.get("skipped"), "漏掉的那个被静默吞了"
    assert "冻结" in out.get("note", ""), "没说漏一行的后果"


@pytest.mark.asyncio
async def test_it_does_not_decide_what_the_model_must_decide(state, tmp_path) -> None:
    """只出机械量。`role` / `retention` 是模型的判断，框架不许代答。"""
    f = tmp_path / "a.npz"
    f.write_bytes(b"x")

    row = (await _call(state, paths=[str(f)]))["files"][0]

    assert set(row) == {"path", "sha256", "bytes"}, (
        f"多给了模型该自己判的字段：{set(row) - {'path', 'sha256', 'bytes'}}"
    )


def test_the_tool_tells_the_model_not_to_shell_out() -> None:
    """工具描述必须指名那条走不通的老路 —— 否则模型照旧去 shell。

    「文案许诺能力 API 得给得出」的反面：能力给了，得让它知道该用这个。
    """
    desc = get_tool("hash_files").description
    # 只查"有没有 shell 这个词"是查不出东西的 —— 第一行「不起 shell」也含它。
    # 变异实测：把那句劝阻改成"自己看着办"，词还在，判据还是绿的。
    # 断言要落在**那句劝阻**上：既说别去（否定词），又说去了会撞上什么。
    assert "不要自己 shell 出去算" in desc, (
        f"没有劝阻模型自己 shell 出去算摘要：{desc!r}"
    )
    assert "执行路线闸" in desc, "没说清走老路会撞上什么 —— 模型不知道为什么要换"
    assert "raw_results" in desc or "manifest" in desc, "没说这是干什么用的"
