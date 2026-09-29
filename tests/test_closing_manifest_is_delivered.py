"""说了完成，交付物清单就得**机械地**递到调度器手上。

## 缺口（wangd 2026-08-23）

「你哪怕出了一篇论文、搞定了之后，调度器到最后是不是得给用户一个明确的答复
啊？说做了什么、输出有哪些，然后把 PDF、图片摆出来。现在是没有这一套。」

查证属实：机械的只有否定门（`core/closure.py`：没闭环不许说完成）。正面义务
为零。实测三种失败：v27 写了汇报但产物是**纯文本路径点不开**、v28 报了
completed 却根本没冻结、v29 卡死一个字都没有。

本文件钉的是"送达"这一半：框架扫盘算出清单并递过去，摆不摆、摆哪几件归模型
（同 PR#634 的分工）。

夹具按现行记录模型造：正文是节点目录下的原生文件（`paper/manuscript__paper.tex`），
冻结是工作区账本（`core/ledger`）上的一行 —— 清单读的就是这本账。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core import closing_manifest as cm
from core.artifact_provenance import produced
from core.ledger import workspace_store
from core.project_workspace import _NODE_WORKSPACES as _DIRS  # 节点 → 目录


def _workspace(tmp_path: Path, *, node: str = "writing") -> Path:
    (tmp_path / _DIRS[node]).mkdir(parents=True, exist_ok=True)
    return tmp_path


def _freeze(ws: Path, *, node: str, artifact_id: str, metadata: dict | None = None,
            version: int = 1, frozen_at: str = "2026-08-23T00:00:00+00:00") -> Path:
    """落一份记录并冻结：正文进节点目录，冻结是账本上的一行，文件一个字节不动。

    `version` 大于账本上的当前版本时，同一身份连续 save 到那一版再冻 —— 账本记
    版本，快照与 git 历史留旧版。返回正文文件的绝对路径。
    """
    # 夹具的 id 形状照真产物（`<type>__<slug>`）；没写类型前缀的就当论文。
    artifact_type = artifact_id.split("__", 1)[0] if "__" in artifact_id else "manuscript"
    store = workspace_store(ws)
    head = store.head(artifact_id)
    current = head.version if head is not None else 0
    for _ in range(max(version - current, 0 if head is not None else 1)):
        store.save(
            artifact_id=artifact_id, artifact_type=artifact_type, name=artifact_id,
            content="x", metadata=metadata or {}, directory=ws / _DIRS[node],
            created_at=frozen_at, provenance=produced(node, "fixture"),
            produced_by_node_type=node, produced_by_run_id="fixture",
            by_node=node, by_run="fixture",
        )
    store.freeze(artifact_id, metadata_patch={}, by_node=node, by_run="fixture",
                 frozen_at=frozen_at)
    return store.abs_path(store.head(artifact_id))


# ── 清单本身 ────────────────────────────────────────────────────────────────

def test_a_frozen_artifact_shows_up_with_a_workspace_relative_path(tmp_path) -> None:
    ws = _workspace(tmp_path)
    _freeze(ws, node="writing", artifact_id="manuscript__paper")
    [item] = cm.frozen_deliverables(ws)
    assert item.artifact_id == "manuscript__paper"
    assert item.path == "paper/manuscript__paper.tex"


def test_an_absolute_pdf_path_is_relativised(tmp_path) -> None:
    """真样本形状：frozen manuscript 的 `metadata.pdf_path` 是**绝对路径**。

    渲染层只放行工作区相对路径（那是零点击外泄的防线，不是格式讲究），
    所以原样抄进正文 = 用户看到一串点不开的字符。必须相对化。
    """
    ws = _workspace(tmp_path)
    pdf = ws / "paper" / "latex_build" / "paper_clean" / "main_clean.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4\n")
    _freeze(ws, node="writing", artifact_id="manuscript__paper",
            metadata={"pdf_path": str(pdf)})
    [item] = cm.frozen_deliverables(ws)
    assert item.companions == ["paper/latex_build/paper_clean/main_clean.pdf"]


def test_a_node_relative_figure_path_is_found(tmp_path) -> None:
    """真产物形状：图的路径相对于**产物自己的节点目录**，不是工作区根。

    真样本（RW_FPT clean_results v2）记的是 `runtime/clean/fig1_tail_loglog.png`，
    而产物在 `experiments/` —— 只按工作区根解析就永远找不到那两张图，
    而"把图片摆出来"正好是用户要的一半。这条是回归钉：跑真数据之前我写的
    第一版就是只按根解析，单测全绿。
    """
    ws = _workspace(tmp_path, node="experiment")
    fig = ws / "experiments" / "runtime" / "clean" / "fig1_tail_loglog.png"
    fig.parent.mkdir(parents=True)
    fig.write_bytes(b"\x89PNG")
    _freeze(ws, node="experiment", artifact_id="clean_results__x",
            metadata={"figures": ["runtime/clean/fig1_tail_loglog.png"]})
    [item] = cm.frozen_deliverables(ws)
    assert item.companions == ["experiments/runtime/clean/fig1_tail_loglog.png"]


def test_a_path_buried_in_a_descriptive_sentence_is_still_found(tmp_path) -> None:
    """真产物形状：图记在 metadata 里，但记成了**带说明的句子**。

    Buffon 课题实测（2026-08-23，部署之后拿刚跑完的真课题验才照出来）：

        "runtime/out/convergence.png (log-log, 9 点 chi2 CI 误差线, OLS 线, …)"

    整串不是路径。只把整个字符串当路径试，这个课题唯一的一张图一件都找不到 ——
    而"把图片摆出来"正好是用户要的一半。前一个课题（RW_FPT）记的是干净路径，
    所以第一版在它身上是好的：**两个真项目两种写法**，一个样本证明不了通用。

    误报由"这个文件在工作区里真的存在"兜住：捞错的字符串命中不了真文件。
    """
    ws = _workspace(tmp_path, node="experiment")
    fig = ws / "experiments" / "runtime" / "out" / "convergence.png"
    fig.parent.mkdir(parents=True)
    fig.write_bytes(b"\x89PNG")
    _freeze(ws, node="experiment", artifact_id="experiment_log__x", metadata={
        "figures": ["runtime/out/convergence.png (log-log, 9 点 chi2 CI 误差线, OLS 线)"],
    })
    [item] = cm.frozen_deliverables(ws)
    assert item.companions == ["experiments/runtime/out/convergence.png"]


def test_prose_that_names_no_real_file_yields_nothing(tmp_path) -> None:
    """从散文里捞路径**不能**变成瞎猜：捞出来的东西必须真在盘上。"""
    ws = _workspace(tmp_path)
    _freeze(ws, node="writing", artifact_id="m", metadata={
        "note": "见 v2.1 的 report.pdf 与 figure-3.png（都还没生成）",
        "version": "1.2.3",
    })
    [item] = cm.frozen_deliverables(ws)
    assert item.companions == []


def test_figures_and_pdfs_outrank_source_fragments(tmp_path) -> None:
    """伴随文件按**可呈现性**排序，不是按 dict 遍历顺序。

    真数据实测：manuscript v4 的 metadata 走一遍能命中十几个文件，按遍历顺序
    取前 6 选出来的是 main.tex / frontmatter.tex / abstract.tex 这些 tex 碎片，
    而两张真该摆的图被挤掉了。拿迭代顺序当重要性，和当初 orientation 拿字母序
    当重要性是同一个错。
    """
    ws = _workspace(tmp_path)
    files = ["a.tex", "b.tex", "c.tex", "d.tex", "e.tex", "f.tex",
             "main.pdf", "fig.png"]
    for name in files:
        (ws / "paper" / name).write_bytes(b"x")
    _freeze(ws, node="writing", artifact_id="m",
            metadata={"parts": [f"{n}" for n in files]})
    [item] = cm.frozen_deliverables(ws)
    assert item.companions[0] == "paper/fig.png"
    assert item.companions[1] == "paper/main.pdf"


def test_a_path_outside_the_workspace_is_dropped(tmp_path) -> None:
    """别人机器上的路径、或平台缓存里的东西，摆出来只会是死链。"""
    outside = tmp_path.parent / "elsewhere.pdf"
    outside.write_bytes(b"%PDF")
    ws = _workspace(tmp_path)
    _freeze(ws, node="writing", artifact_id="m", metadata={"pdf_path": str(outside)})
    [item] = cm.frozen_deliverables(ws)
    assert item.companions == []


def test_companions_are_found_by_scanning_values_not_a_key_list(tmp_path) -> None:
    """判据是"这个字符串是不是工作区里真实存在的文件"，不是查一份键名单。

    写死 `("pdf_path", "figure_paths", …)` 就是名单式护栏：新加的键默认漏过，
    而且漏了没人会发现。这里用一个**从没有人见过的键名**验它默认被覆盖。
    """
    ws = _workspace(tmp_path)
    fig = ws / "paper" / "figures" / "fig1.png"
    fig.parent.mkdir(parents=True)
    fig.write_bytes(b"\x89PNG")
    _freeze(ws, node="writing", artifact_id="m",
            metadata={"some_key_nobody_has_ever_written": {"nested": [str(fig)]}})
    [item] = cm.frozen_deliverables(ws)
    assert item.companions == ["paper/figures/fig1.png"]


def test_a_ledger_row_whose_file_is_gone_is_not_offered(tmp_path) -> None:
    """账本里有、盘上没有：报出去只会让模型引一个 404。"""
    ws = _workspace(tmp_path)
    path = _freeze(ws, node="writing", artifact_id="m")
    path.unlink()
    assert cm.frozen_deliverables(ws) == []


def test_the_latest_freeze_wins(tmp_path) -> None:
    """账本是 append-only：冻了、修订到 v7、再冻 → 多行 freeze，报最近冻结的那版。"""
    ws = _workspace(tmp_path)
    _freeze(ws, node="writing", artifact_id="m", version=1, frozen_at="2026-08-01T00:00:00+00:00")
    _freeze(ws, node="writing", artifact_id="m", version=7, frozen_at="2026-08-23T00:00:00+00:00")
    [item] = cm.frozen_deliverables(ws)
    assert item.version == 7


# ── 「摆出来了没有」的判据 ───────────────────────────────────────────────────

def test_a_bare_path_in_prose_does_not_count_as_presented(tmp_path) -> None:
    """v27 的收尾汇报就是这样写的：路径提了，但用户点不开。

    判据必须落在**效果**上（可点），不能落在"提没提过"上。
    """
    ws = _workspace(tmp_path)
    _freeze(ws, node="writing", artifact_id="m")
    items = cm.frozen_deliverables(ws)
    prose = "论文 PDF（终稿）：paper/m.tex"
    assert cm.unpresented(items, prose) == items


def test_presenting_the_pdf_settles_the_whole_deliverable(tmp_path) -> None:
    """论文的可点入口是 PDF，不是那份 .tex —— 要求两个都摆是形式主义。"""
    ws = _workspace(tmp_path)
    pdf = ws / "paper" / "latex_build" / "main.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF")
    _freeze(ws, node="writing", artifact_id="m", metadata={"pdf_path": str(pdf)})
    items = cm.frozen_deliverables(ws)
    text = "论文终稿：[UK food culture (PDF)](paper/latex_build/main.pdf)"
    assert cm.unpresented(items, text) == []


def test_an_inline_image_counts_too(tmp_path) -> None:
    ws = _workspace(tmp_path)
    fig = ws / "paper" / "fig1.png"
    fig.write_bytes(b"\x89PNG")
    _freeze(ws, node="writing", artifact_id="m", metadata={"figure": str(fig)})
    items = cm.frozen_deliverables(ws)
    assert cm.unpresented(items, "![尾部 log-log](paper/fig1.png)") == []


# ── 触发时机：判据宽一点就会把「只放一次」的收尾闸烧在半路 ──────────────────

def test_the_gate_only_fires_on_an_explicit_completion_claim(tmp_path, monkeypatch) -> None:
    """研究中段的普通回复不能触发 —— 那会烧掉唯一一次机会且不报错。

    `on_before_finish` 对 continuous orchestrator 来说每次回话都会跑，而框架侧
    `_finish_gate_used` 只放一次。这条钉的就是这个组合。
    """
    from core import loop_hooks_builtin as builtin

    ws = _workspace(tmp_path)
    _freeze(ws, node="writing", artifact_id="m")
    ctx = _ctx(ws, "literature 跑完了，我接着派 observation。\n\nCONTINUOUS_STATUS: continue")
    assert builtin._closing_manifest_before_finish(ctx) is None


def test_it_fires_when_completion_is_claimed_and_nothing_was_presented(tmp_path) -> None:
    from core import loop_hooks_builtin as builtin

    ws = _workspace(tmp_path)
    pdf = ws / "paper" / "latex_build" / "main.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF")
    _freeze(ws, node="writing", artifact_id="manuscript__uk", metadata={"pdf_path": str(pdf)})
    ctx = _ctx(ws, "课题全部交付。\n\nCONTINUOUS_STATUS: complete")
    out = builtin._closing_manifest_before_finish(ctx)
    assert out and "paper/latex_build/main.pdf" in out[0].content
    assert "manuscript__uk" in out[0].content


def test_it_stays_quiet_when_everything_is_already_presented(tmp_path) -> None:
    """已经摆好了还拦一次 = 白烧一轮，还把收尾闸用掉。"""
    from core import loop_hooks_builtin as builtin

    ws = _workspace(tmp_path)
    _freeze(ws, node="writing", artifact_id="m")
    ctx = _ctx(ws, "做完了：[稿件](paper/m.tex)\n\nCONTINUOUS_STATUS: complete")
    assert builtin._closing_manifest_before_finish(ctx) is None


def test_no_frozen_deliverables_is_someone_elses_problem(tmp_path) -> None:
    """宣称完成却一件都没冻，是 v28 那个病（closure + 冻结义务管），不在这里判。

    同一件事两个地方判，迟早分叉。
    """
    from core import loop_hooks_builtin as builtin

    ws = _workspace(tmp_path)
    ctx = _ctx(ws, "全部完成。\n\nCONTINUOUS_STATUS: complete")
    assert builtin._closing_manifest_before_finish(ctx) is None


def test_the_delivery_is_not_a_gate(tmp_path) -> None:
    """送达 ≠ 卡死。QC 层被删就是因为「一个自动判定能一票否决交付」走不通。"""
    from core import loop_hooks_builtin as builtin

    ws = _workspace(tmp_path)
    _freeze(ws, node="writing", artifact_id="m")
    ctx = _ctx(ws, "完成。\n\nCONTINUOUS_STATUS: complete")
    out = builtin._closing_manifest_before_finish(ctx)
    body = out[0].content
    assert "不是门禁" in body, "清单必须自己说清楚它不拦人 —— 否则模型会当成硬门"
    for banned in ("必须摆出全部", "否则不许收尾", "raise"):
        assert banned not in body


def test_the_hook_is_enabled_on_the_orchestrator() -> None:
    """写了 hook 没挂上 = 防线缺席，而缺席和"跑了但没效果"长得一模一样。"""
    import yaml

    spec = yaml.safe_load(Path("nodes/_orchestrator/harness.yaml").read_text(encoding="utf-8"))
    assert "closing_manifest" in (spec.get("loop_hooks") or [])

    # yaml 里写对了名字还不够：`list_hooks` 对认不出的名字是**警告 + 跳过**，
    # 一个错字就等于这道防线整个不在场，而且全绿。所以过一遍真注册表。
    from core import loop_hooks_builtin  # noqa: F401  （注册副作用）
    from core.loop_hooks import get_loop_hook

    hook = get_loop_hook("closing_manifest")
    assert hook is not None, "hook 没注册进全局表 —— yaml 里那一行是死的"
    assert hook.on_before_finish is not None


def _ctx(ws: Path, assistant_text: str):
    """最小 HookContext：本 hook 只用到 messages / state.project_worktree。"""
    from core.llm import LLMMessage
    from core.loop_hooks import HookContext

    class _State:
        project_worktree = ws
        hook_state: dict = {}

        def append_transcript(self, *_a, **_k):
            return None

    return HookContext(
        harness=None, state=_State(), turn=3,
        messages=[
            LLMMessage(role="user", content="继续"),
            LLMMessage(role="assistant", content=assistant_text),
            LLMMessage(role="system", content="别的 hook 追加的东西"),
        ],
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
