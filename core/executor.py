"""顶层 harness 执行器：把 loader + context + agent loop + 完成判定串起来。

`run_node.py` 调用的就是它。一次函数调用进 → 一份 summary dict 出。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import artifact_provenance as _provenance
from . import closure, paths
from .agent_loop import run_loop
from .context_engine import build_messages
from .harness import NodeHarness
from .llm import LLMClient, LLMMessage
from .loader import load_harness
from .pause import (
    ActiveRunInfo,
    register_active,
    unregister_active,
)
from .state import State

log = logging.getLogger("executor")


def _materialize_forwarded_file(
    *,
    child_state: State,
    parent_state: State,
    artifact: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Copy a run-scoped file-backed artifact into the child run.

    Artifact JSON alone is insufficient for images, NetCDF/Zarr, molecular
    structures, and other scientific files: their ``file_path`` is relative to
    the parent run.  Preserve the source artifact but rewrite only the child's
    run-local path to a collision-free location.  Paths outside the parent run
    (or its project workspace) are not copied implicitly.
    """

    raw_path = metadata.get("file_path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return metadata
    # `file_path` 是上游用 `paths.display_relpath()` 写下的（锚点 worktree 优先），
    # 所以只能用它的逆来解。自己拼 `parent_state.root / rel` 在绑了 worktree 的 run
    # 上必错，表现是"源文件不存在" —— 于是子节点静默收不到图/结构/NetCDF 附件。
    source = paths.resolve_display_relpath(parent_state, raw_path)
    if not paths.within_run_scope(parent_state, source):
        child_state.append_transcript(
            "forwarded_artifact_file_rejected",
            artifact_type=artifact.get("type"),
            artifact_name=artifact.get("name"),
            reason="file_path is outside the parent run's tree",
        )
        return metadata
    if not source.exists():
        child_state.append_transcript(
            "forwarded_artifact_file_missing",
            artifact_type=artifact.get("type"),
            artifact_name=artifact.get("name"),
            source_path=str(source),
            looked_in=[str(c) for c in paths.display_relpath_candidates(parent_state, raw_path)],
        )
        return metadata

    from core.state import _slug

    artifact_slug = _slug(f"{artifact.get('type', 'artifact')}__{artifact.get('name', 'input')}")
    destination = child_state.root / "forwarded_inputs" / artifact_slug / source.name
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, destination, dirs_exist_ok=True)
    else:
        shutil.copy2(source, destination)
    updated = dict(metadata)
    updated["forwarded_source_file_path"] = str(source)
    # 写回去也走同一个漏斗：自己算 `relative_to(child_state.root)` 就是在锚点
    # 之外再造一个字符串，读方按 display 锚点解会解到别处。
    updated["file_path"] = paths.display_relpath(child_state, destination)
    child_state.append_transcript(
        "forwarded_artifact_file_materialized",
        artifact_type=artifact.get("type"),
        artifact_name=artifact.get("name"),
        destination_path=updated["file_path"],
        is_directory=source.is_dir(),
    )
    return updated


def _inherit_sandbox_capability(child: State, parent: State) -> None:
    """子 run 拿谁的沙箱能力 —— 判据是**事实**：父的冻结 manifest 盖不盖得住我的根。

    两条不变量原来互相打架（#769，lujy 在 node20 wrf-compile 实测）：
      · RunAttempt 隔离：一份 manifest 属于且只属于一个 attempt、冻结后不可变；
      · spawn child 时把父的 manifest + hash **原样复制**（PR 8b866063 的副作用，
        没有任何测试钉过它）。
    平台上两者不冲突：dispatcher 签的 attempt manifest 把整个会话 worktree 冻成
    rw，子节点的 workspace_root / run_root 都在里面，全家共用一个容器是设计。
    CLI 上就撞了：orchestrator 的本地 manifest 只冻它自己的两个根，experiment
    子 run 连 `echo probe-ok` 都被 `writable root was not frozen` 拒掉，而 manifest
    不可变、hash 钉死，子 run 没有任何路径重签 —— 平台主委派路径整条断路。

    所以：盖得住 → 继承（同一个 attempt，同一个容器）；盖不住 → 不继承，
    子 run 在第一次 shell 调用时按自己的根铸自己的本地 manifest
    （`sandbox.manifest_for` 原有行为），并把这件事记进 transcript。
    """
    from . import sandbox as _sandbox

    child.platform_attempt_id = parent.platform_attempt_id
    inherited = parent.sandbox_manifest if isinstance(parent.sandbox_manifest, dict) else None
    if inherited is None:
        return
    missing = _sandbox.uncovered_write_roots(inherited, child)
    if not missing:
        child.sandbox_manifest = dict(inherited)
        child.sandbox_manifest_hash = parent.sandbox_manifest_hash
        return
    child.append_transcript(
        "sandbox_capability_reissued",
        parent_run_id=parent.run_id,
        parent_attempt_id=str(inherited.get("attempt_id") or ""),
        uncovered_write_roots=[str(path) for path in missing],
        note="父 run 的冻结 manifest 没有以 rw 覆盖本 run 的可写根；本 run 按自己的根铸 manifest",
    )


async def execute_node(
    node_type: str,
    *,
    state_dir: Path,
    project_id: str | None = None,
    node_inputs: dict | None = None,
    upstream_artifacts: list[dict] | None = None,
    upstream_memory: list[dict] | None = None,
    harness_override: NodeHarness | None = None,
    llm: LLMClient | None = None,
    parent_state: State | None = None,
    depth: int = 0,
    sub_run_id: str | None = None,
    resume_run_id: str | None = None,
    selected_input_ids: list[str] | None = None,
    deliverable: dict | None = None,
    planned_stop_authorized: bool = False,
    planned_stop_note: str = "",
    task_instance_uuid: str | None = None,
    task_contract_revision: int | None = None,
    task_contract_digest: str | None = None,
    parent_dispatch_id: str | None = None,
) -> dict[str, Any]:
    """端到端跑完一个节点。

    参数：
        node_type：加载哪个 harness yaml（如 "literature"）。
        state_dir：output/{run_id}/ 在哪里创建。
        node_inputs：harness 期望接收的字段（如 research_question）。
        upstream_artifacts：预先注入的"上游"artifact
            （list of {type, name, content, metadata}）。
        upstream_memory：预先注入的 memory 条目
            （list of {kind, text, tags?, applies_to_node?, expires_at?}，v2.1 schema）。
        harness_override：如果想直接传 NodeHarness 而不走 yaml 加载。
        llm：LLMClient（默认从环境变量构造）。

    返回：
        一份 summary dict —— 同时也写入 output/{run_id}/summary.json。
    """
    harness = harness_override or load_harness(node_type)
    # v3.1：per-harness 模型异构 —— harness 指定 model 时忽略传入的共享 llm，
    # 为该节点构造独立 client（api key 走 env 间接，yaml 不放密钥）。
    if harness.llm_model:
        import os as _os

        from .runtime_secrets import get as _runtime_secret

        _key = _os.environ.get(harness.llm_api_key_env) if harness.llm_api_key_env else None
        if not _key and harness.llm_api_key_env:
            _key = _runtime_secret(harness.llm_api_key_env)
        llm = LLMClient(
            model=harness.llm_model,
            base_url=harness.llm_base_url,
            api_key=_key,
        )
    else:
        llm = llm or LLMClient()

    # #426：dreaming 是后台代谢，不得与前台 run 抢账户级并发位。用 twin
    # 而不是改传入实例 —— 传入的可能是 orchestrator 正在用的共享 client。
    # 判据机械：_curator + mode=dreaming（chat.py 后台任务的唯一入口形状）。
    if (
        node_type == "_curator"
        and str((node_inputs or {}).get("mode") or "") == "dreaming"
    ):
        llm = llm.spawn_silent()
        llm.priority = "low"

    _state_kwargs = dict(
        node_type=node_type,
        base_dir=state_dir,
        project_id=project_id,
        tenant_id=(getattr(parent_state, "tenant_id", None) if parent_state else None),
        session_id=(getattr(parent_state, "session_id", None) if parent_state else None),
        project_worktree=(
            getattr(parent_state, "project_worktree", None) if parent_state else None
        ),
        # 任务与派发身份（#1080 第 4 条）：节点和审计读得到的是同一份。
        task_instance_uuid=task_instance_uuid or None,
        task_contract_revision=task_contract_revision,
        task_contract_digest=task_contract_digest or None,
        parent_dispatch_id=parent_dispatch_id or None,
    )
    # 续跑：**同一个 run** 接着走，不是新开一个（wangd 2026-08-18）。
    # 拿不到那个目录就退回新开 —— 续跑失败不该让这次派发整个死掉。
    resumed_messages: list[LLMMessage] | None = None
    resumed_from_turn = 0
    # 续跑成没成，是**调用方**要知道的事实（#1081）。此前退回新开只留一行
    # `log.warning`：返回的是新 run_id 的正常 `status=completed` summary，没有
    # 任何字段说明它换过 run，新 run 的 transcript 里也找不到"曾请求续跑"。
    # 于是"接着上一轮"和"从头来过"在调用方眼里一模一样 —— 而两者的产物、花费
    # 和上下文完全不同。
    #
    # 这条事实是全函数：没请求续跑也照写（requested=None），否则"没请求"和
    # "请求了但字段丢了"又分不开。
    resume_outcome: dict[str, Any] = {
        "requested_run_id": resume_run_id or None,
        "resumed": False,
        "reason": None,
    }
    if resume_run_id:
        try:
            state = State.reopen(run_id=resume_run_id, **_state_kwargs)
        except (FileNotFoundError, OSError) as exc:
            reason = f"{type(exc).__name__}: {exc}"
            log.warning("run %s 续跑失败，改为新开：%s", resume_run_id, exc)
            state = State.new(**_state_kwargs)
            resume_outcome["reason"] = reason[:400]
            state.append_transcript(
                "resume_failed_started_fresh",
                requested_run_id=resume_run_id,
                fresh_run_id=state.run_id,
                error=reason[:400],
            )
        else:
            from .agent_loop import load_messages_checkpoint

            resume_outcome["resumed"] = True
            restored = load_messages_checkpoint(state)
            if restored:
                resumed_messages, resumed_from_turn = restored
    else:
        state = State.new(**_state_kwargs)
    state.hook_state["_resume_outcome"] = resume_outcome
    # 「这个任务允许中途停下作业吗」—— 由派发方给出，开工前冻住，子 run 只能
    # 引用（#1084 第二节）。执行者不能自己给自己发授权。
    from .stop_authorization import freeze as _freeze_stop_authorization

    _freeze_stop_authorization(
        state,
        authorized=bool(planned_stop_authorized),
        note=planned_stop_note,
        authorized_by_run_id=(parent_state.run_id if parent_state else None),
    )
    state.depth = depth
    if parent_state is not None:
        _inherit_sandbox_capability(state, parent_state)
    state.sub_run_id = sub_run_id
    state.parent_run_id = parent_state.run_id if parent_state else None
    state.append_transcript(
        "run_start",
        node_type=node_type,
        harness=harness.describe(),
        depth=depth,
        sub_run_id=sub_run_id,
        parent_run_id=state.parent_run_id,
        # 四个字段与派发参数**逐字相等**（#1080 验收 4）。恒写 —— 缺席
        # （None）和"没安排"是同一件事，而"没记"是另一件事，两者不能长得一样。
        task_instance_uuid=state.task_instance_uuid,
        task_contract_revision=state.task_contract_revision,
        task_contract_digest=state.task_contract_digest,
        parent_dispatch_id=state.parent_dispatch_id,
    )
    # 注册到 active runs registry —— 让 inject_into_node / cancel_node 工具能找到本 run。
    # paused 时仍保留 active 登记（pause registry 也有，find_child_state 双查）。
    # finalize_fn / pause unwind 退出本函数时 unregister。
    register_active(
        ActiveRunInfo(
            run_id=state.run_id,
            node_type=node_type,
            state=state,
            parent_run_id=state.parent_run_id,
            sub_run_id=sub_run_id,
            started_at=datetime.now(UTC).isoformat(),
        )
    )

    # Runtime test modes are a process-owner capability, never an LLM-grantable
    # node input.  Enforce this before forwarding artifacts or entering the LLM
    # loop so a production orchestrator cannot activate a deterministic fixture.
    from .runtime_capabilities import validate_node_runtime_inputs

    # v2.1：请求模式是 **run 级状态**，记在 state 上而不是靠穿参数 ——
    # finalize_run 拿不到 node_inputs（它也被 pause_driver 在 resume 后调用，
    # 那时参数早没了），而"本次该交付什么"必须在收尾时还能算得出来。
    _mode = (node_inputs or {}).get("mode")
    if isinstance(_mode, str) and _mode:
        state.hook_state["_request_mode"] = _mode

    runtime_input_error = validate_node_runtime_inputs(
        node_type,
        node_inputs,
        capabilities=state.runtime_capabilities,
    )
    if runtime_input_error:
        state.append_transcript(
            "runtime_input_denied",
            reason=runtime_input_error,
            node_inputs_preview=str(node_inputs or {})[:300],
        )
        summary = {
            "run_id": state.run_id,
            "node_type": node_type,
            "project_id": state.project_id,
            "status": "blocked_unauthorized_test_mode",
            "missing_input_artifact_types": [],
            "unfrozen_required_inputs": [],
            "missing_required_outputs": list(harness.required_output_artifact_types or []),
            "turns": 0,
            "tool_call_count": 0,
            "tokens_used": state.tokens_used,
            "artifacts": [],
            "final_text_preview": runtime_input_error,
            "state_dir": str(state.root),
            "project_root": str(state.project_root) if state.project_root else None,
            "depth": depth,
            "sub_run_id": sub_run_id,
        }
        state.write_summary(summary)
        state.append_transcript("run_end", **{k: v for k, v in summary.items() if k != "artifacts"})
        unregister_active(state.run_id)
        return summary
    # 子调用：tokens_used 从 0 起算（只记本次调用），tokens_limit 继承父的
    # 这样并行子节点也能各自独立计数；结束时把 delta 加回父，由父汇总。
    if parent_state is not None:
        state.tokens_used = 0
        state.tokens_limit = parent_state.tokens_limit
        state.tool_calls_made = 0

    # 预先注入上游 artifact + memory。
    # 上游 artifact 是**本 run 的输入**，不是产出 —— 标 `_forwarded_input=True`，
    # 让完成度门禁把它们排除在"本 run 产出"之外（见 finalize_run）。否则转发一个
    # 与本节点 required_output 同类型的 frozen artifact，就能让门禁在 agent 还没
    # 干活前判定"完成"（实测根因：experiment 6 次 revision 全靠转发同一份旧
    # experiment_log 蒙过完成度门禁，agent 编数值/不执行也照样 completed）。
    # agent 若在本 run 真的 save_artifact 覆盖它 → 新记录不带此标记 → 重新计入产出。
    # 2026-08-04：转发**不改变产出方**。此前这里不传 provenance，save_artifact
    # 就把接收方记成产出者 —— 外部导入的 experiment_log 转发进 writing 子 run
    # 一次就被洗成"平台自产"，防编数据那道门等于白设。
    project_workspace_mode = getattr(state, "project_worktree", None) is not None
    forwarded_input_ids: list[str] = []
    for a in () if project_workspace_mode else (upstream_artifacts or []):
        _fwd_md = dict(a.get("metadata") or {})
        _fwd_md["_forwarded_input"] = True
        if parent_state is not None:
            _fwd_md = _materialize_forwarded_file(
                child_state=state,
                parent_state=parent_state,
                artifact=a,
                metadata=_fwd_md,
            )
        saved = state.save_artifact(
            artifact_type=a["type"],
            name=a.get("name", a["type"]),
            content=a.get("content", ""),
            metadata=_fwd_md,
            provenance=_provenance.forwarded(
                a.get("provenance"), via_node_type=state.node_type, via_run_id=state.run_id
            ),
        )
        # 冻结的事实只出自账本的 freeze 行（save 行里的 frozen 键一律剥掉）：上游那份
        # 是冻结的，就在本 run 的本地账本里也落一行 freeze，时间戳沿用上游的 ——
        # 否则转发进来的预注册永远"未冻结"，防篡改门把每个带 prereg 输入的 run 全拦。
        if _fwd_md.get("frozen"):
            state.mark_frozen(
                str(saved.get("id") or ""),
                {k: _fwd_md[k] for k in ("freeze_reason",) if k in _fwd_md},
                frozen_at=str(_fwd_md.get("frozen_at") or "") or None,
            )
        forwarded_input_ids.append(str(saved.get("id") or ""))
    # Project 模式不搬文件，caller 的选择经 selected_input_ids 送达（run_node
    # 已验过每个 id 在共享 worktree 可见）。两种模式殊途同归：hook_state 里
    # `forwarded_input_ids` 就是"caller 为本 run 选定的输入"的机械真相源，
    # 下游审计（writing input_audit 等）读这里，不再要求 caller 另行申报。
    if project_workspace_mode and selected_input_ids:
        forwarded_input_ids = [str(aid) for aid in selected_input_ids if str(aid)]
    forwarded_input_ids = [aid for aid in forwarded_input_ids if aid]
    if forwarded_input_ids:
        state.hook_state["forwarded_input_ids"] = forwarded_input_ids
        state.append_transcript("forwarded_inputs_recorded", artifact_ids=forwarded_input_ids)
    # fixture 回放注入的 memory：落手册，与 agent 自己 `memory_note` 写的
    # 是同一处，回放出来的状态才跟真实 run 一致。
    if upstream_memory and getattr(state, "project_worktree", None):
        from core import memory as _M

        for m in upstream_memory:
            text = str(m.get("text") or "").strip()
            kind = str(m.get("kind") or "")
            section = (_M.SECTION_METHOD
                       if kind in ("method", "workflow", "workflow_hint")
                       else _M.SECTION_PITFALL)
            try:
                _M.append_manual(state, text=text, section=section,
                                 nodes=[state.node_type],
                                 run_id=str(state.run_id or "fixture"))
            except _M.MemoryError_:
                continue          # 回放数据不合约束就跳过，不该拖垮 run

    # ── 输入契约的两类，判据不同（v2.1 P-QC3）─────────────────────────────
    # (1) **文件传递约定**："起 writing 之前父 state 里得有 experiment_log 这个
    #     类型" —— 这是 v1 点对点总线的产物。Workspace-First 下游直接读上游目录，
    #     材料够不够由节点自己看、不够就 report_blocker，框架不替它判。v2 关闭。
    # (2) **科学凭据**："预注册必须已冻结才能开跑" —— 防确认偏误的时序
    #     （先冻结、后实验），是这个平台的科学地基之一，与传递方式无关。
    #     它此前和 (1) 写在一起，于是 v2 把两个一起关掉了 —— 又一次"排除新架构
    #     而不是修判据"。跨节点读通了之后，experiment 看得见
    #     hypothesis/artifacts/ 里的 pre_registration，(2) 完全可以照常执行。
    upstream_types = {a["type"] for a in state.list_artifacts()}
    missing_inputs = (
        []
        if project_workspace_mode
        else [t for t in harness.required_input_artifact_types if t not in upstream_types]
    )

    # v3.1（审计 高危#4）：tamper-evident 类输入必须已 freeze 才能开跑。
    # 典型：experiment 节点的 pre_registration —— 未冻结的预注册等于没有预注册
    # （跑完还能改），防确认偏误的时序（先冻结、后实验）在这里机械把关。
    unfrozen_inputs: list[str] = []
    _TAMPER_EVIDENT_INPUT_TYPES = {"pre_registration"}
    for a in state.list_artifacts():
        if (
            a["type"] in harness.required_input_artifact_types
            and a["type"] in _TAMPER_EVIDENT_INPUT_TYPES
        ):
            # ⚠️ v3.1 hotfix：list_artifacts() 只返 id/type/name，不含 metadata。
            # 早前这里直接 a.get("metadata") 永远拿到空 → frozen 的预注册被误判
            # 未冻结 → 每个带 pre_registration 输入的节点 100% blocked。必须 read
            # 完整记录取真 metadata。（同事 2026-07 报的回归）
            rec = state.read_artifact(a["id"]) or {}
            meta = rec.get("metadata") or {}
            if not meta.get("frozen"):
                unfrozen_inputs.append(a["type"])

    if missing_inputs or unfrozen_inputs:
        summary = {
            "run_id": state.run_id,
            "node_type": node_type,
            "project_id": state.project_id,
            "status": "blocked_missing_inputs",
            "missing_input_artifact_types": missing_inputs,
            "unfrozen_required_inputs": unfrozen_inputs,
            "missing_required_outputs": [],
            "turns": 0,
            "tool_call_count": 0,
            "tokens_used": state.tokens_used,
            "artifacts": state.list_artifacts(own_only=True),  # summary 只报自己的
            "final_text_preview": (
                ""
                if not unfrozen_inputs
                else f"输入 {unfrozen_inputs} 未 freeze —— 先 freeze_artifact 再启动本节点"
            ),
            "state_dir": str(state.root),
            "project_root": str(state.project_root) if state.project_root else None,
            "depth": depth,
            "sub_run_id": sub_run_id,
        }
        state.write_summary(summary)
        state.append_transcript("run_end", **{k: v for k, v in summary.items() if k != "artifacts"})
        if parent_state is not None:
            # v3.1 fix：原来是 "="，把父节点已累计的计数直接清零（审计 correctness）
            parent_state.tokens_used += state.tokens_used
            parent_state.tool_calls_made += state.tool_calls_made
        unregister_active(state.run_id)
        return summary

    # v1.0: 把 node_inputs 落 hook_state，让 hook 能拿到
    if node_inputs:
        state.hook_state["node_inputs"] = dict(node_inputs)
    # 2026-08-31：用户点名的交付文件（run_node 的 deliverable 契约）。
    # save_artifact 落账时由框架把 content 投影到这个路径 —— 模型不搬字节。
    if deliverable:
        state.hook_state["_deliverable_projection"] = dict(deliverable)

    messages = build_messages(harness, state, node_inputs)
    if resumed_messages:
        # 「恢复不就是把之前那堆 message 再输入给大模型让它继续推进吗」——
        # 正是。system prompt 用**当前**的（harness 可能已更新），其余接上
        # checkpoint 里的历史；工具协议用与主循环同一份修复逻辑补齐，
        # 免得尾部半截的 tool_call 让 provider 400。
        system_head = [m for m in messages if m.role == "system"]
        tail = [m for m in resumed_messages if m.role != "system"]
        messages = system_head + tail
        try:
            from chat import _repair_message_tool_protocol

            _repair_message_tool_protocol(messages)
        except Exception:  # noqa: BLE001 —— 修不了也要让它跑起来
            pass
        # 这次派发的要求也要送进去 —— 续跑不等于忽略新指令。上次这条 run 可能
        # 由别人派、做的是别的角度；调用方这次说了什么，必须让它看到，否则
        # "续跑"就变成了"吞掉新请求"。
        fresh_request = ""
        if node_inputs:
            try:
                fresh_request = json.dumps(node_inputs, ensure_ascii=False)[:2000]
            except (TypeError, ValueError):
                fresh_request = str(node_inputs)[:2000]
        messages.append(LLMMessage(
            role="user",
            content=(
                "（系统）上一次执行在第 "
                f"{resumed_from_turn} 轮之后被进程中断，以上是你当时的完整上下文。"
                "**接着往下做**：先确认你最后那步工具调用是否已经生效"
                "（产物可能已保存、KB 可能已注册 —— 别重复做不幂等的操作），"
                "然后继续推进未完成的部分。不要从头再来。"
                + (
                    f"\n\n本次调用方给的要求（若与你原来的任务一致就当作确认，"
                    f"不一致就以这条为准）：{fresh_request}"
                    if fresh_request else ""
                )
            ),
        ))
        state.append_transcript(
            "run_resumed_after_interruption",
            resumed_from_turn=resumed_from_turn,
            n_messages=len(messages),
        )

    # ── opt-in custom agent_loop override ─────────────────────────────────
    # 节点目录下若有 agent_loop.py:run_loop，用它而非 framework 默认 loop。
    # 见 templates/agent_loop.py.template 的警告（owner 接受减弱的 framework 保证）。
    from .custom_loop import resolve_custom_loop

    custom = resolve_custom_loop(node_type)

    # ── QC 契约启动期校验（此处 = 唯一对所有 loop 实现都生效的位置）────────
    # PR#164 把校验放在 core.agent_loop.run_loop 里，但 custom_loop 节点（data
    # 的 data_agent_loop.py 有 6 条早退路径直接 return LoopResult，从不调
    # run_default）整个绕过它 —— "不可能契约"在这些节点上仍要跑完整个 run 才
    # fail-closed 暴露。校验是纯函数、幂等、极便宜，放在 dispatch 之前对
    # custom / default 一视同仁；default loop 内那次重复校验无害（同样结论）。
    # ── catch-all：run_loop 抛任何异常（最常见：LLM retry 耗尽后的网络错）→
    # 写一份 status="error" summary + transcript run_end，不让 run "无状态消失"。
    # 同事反馈：长 run 跑到 turn N 撞 RemoteProtocolError，executor 没 wrap →
    # 异常冒泡 → summary.json 永远不写 → 事后只能靠 transcript 拼现场。
    from core import cancellation as _cancel

    try:
        # #284：绑在**这一层**而不是只绑 run_loop —— 自定义 loop（data 就是）
        # 整个绕过 run_loop，它自己直调的 planner / 模型请求也必须被咽喉罩住。
        # 绑定只覆盖 loop 本体，不覆盖下面的 finalize：取消了照样要能写 summary、
        # 跑 run-end hook（那些也调模型），否则"取消"会退化成"卡死"。
        with _cancel.bind_run(state):
            if custom is not None:
                state.append_transcript(
                    "agent_loop_mode", mode="custom", source=f"nodes/{node_type}/agent_loop.py"
                )
                log.info("node %s using custom agent_loop", node_type)
                loop_result = await custom(harness, state, messages, llm)
            else:
                loop_result = await run_loop(harness, state, messages, llm)
    except _cancel.RunCancelled as exc:
        # 自定义 loop 没有 run_loop 的收口，取消会一路冒到这里 —— 同样收成
        # cancelled 的 LoopResult，接着走**统一 finalize**（qinp 的验收 3：
        # run 与 task 账本都要转终态，不许留 in_progress）。
        from .agent_loop import LoopResult as _LR

        state.append_transcript(
            "loop_cancelled_at_chokepoint",
            where=exc.where,
            reason=exc.signal.get("reason"),
            requested_by=exc.signal.get("requested_by"),
        )
        _cancel.mark_cancelled(state, exc.signal)
        _turn = int(state.hook_state.get("_current_turn") or 0)
        loop_result = _LR(
            final_text=f"(cancelled: {exc.signal.get('reason') or 'run 已取消'})",
            turns=_turn,
            tool_calls=[],
            messages=messages,
            status="cancelled",
            cancel_meta={**exc.signal, "cancelled_at": exc.where, "cancelled_at_turn": _turn},
        )
    except Exception as exc:
        # 只 catch Exception，CancelledError/KeyboardInterrupt 等 BaseException
        # 让 cancellation/中断语义照常向上冒，不在 except 里做 I/O。
        # 写完 summary 后 re-raise —— run_node.py 仍会看到 traceback 退非零，
        # 但磁盘上现在有 summary.json + transcript run_end 可供事后排查。
        import traceback as _tb

        log.error(
            "run_loop raised %s in node=%s run=%s", type(exc).__name__, node_type, state.run_id
        )
        # 准确的 turn 数在 transcript 里（最后一个带 turn 字段的 event）；
        # state 上没维护 turn，所以 summary 不记 turn —— 走 tool_call_count 当代理。
        # #480：provider 断流/超时打穿重试预算后，异常照旧往上抛，而上面没有
        # 任何一层认得出它是什么 —— 于是"模型服务暂时挂了"被记成"这个节点失败
        # 了"，进熔断统计、逼 orchestrator 去退回上游补料。这里认出来并归类：
        #   · failure_category=provider_unavailable（EXTERNAL_FAILURE_CATEGORIES
        #     的一员，不进节点卡死统计）
        #   · 现场信息落盘：provider / 模型 / 最后一次错误 / 已落地产物 /
        #     有没有可续跑的 checkpoint —— 排查和续跑都不必再去翻 traceback
        from core.llm import describe_provider_error, is_transient_provider_error

        _provider_down = is_transient_provider_error(exc)
        summary = {
            "run_id": state.run_id,
            "node_type": node_type,
            "project_id": state.project_id,
            "status": "error",
            "failure_category": "provider_unavailable" if _provider_down else None,
            "failure_subcategory": type(exc).__name__ if _provider_down else None,
            "provider_error": describe_provider_error(exc) if _provider_down else None,
            # #490：provider 自己说的限流原因（error.type / code / Retry-After /
            # 模型 / 粗估 prompt token）—— 没有它就分不清限的是 TPM、RPM 还是并发位。
            "provider_error_detail": getattr(exc, "detail", None) if _provider_down else None,
            "provider_base_url": os.getenv("LLM_BASE_URL") if _provider_down else None,
            "provider_model": os.getenv("LLM_MODEL") if _provider_down else None,
            # 已经落地的产物不会因为这次异常消失（save_artifact 当场写盘），
            # 但"还剩下什么"必须在 summary 里说清，否则只能靠人去翻目录。
            "has_checkpoint": (state.root / "messages_checkpoint.json").is_file(),
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:2000],
            "error_traceback": _tb.format_exc()[:8000],
            "missing_required_outputs": [],
            "tool_call_count": state.tool_calls_made,
            "tokens_used": state.tokens_used,
            # 归属问题必须用 own_only —— 见 State.list_artifacts 的契约。
            "artifacts": state.list_artifacts(own_only=True),
            "final_text_preview": "",
            "state_dir": str(state.root),
            "project_root": str(state.project_root) if state.project_root else None,
            "depth": depth,
            "sub_run_id": sub_run_id,
        }
        try:
            state.write_summary(summary)
            state.append_transcript(
                "run_end",
                **{k: v for k, v in summary.items() if k not in ("artifacts", "error_traceback")},
            )
        except Exception:
            # write_summary / append_transcript 自身炸了不能让 re-raise 丢原异常
            log.exception("failed to persist error summary for run=%s", state.run_id)
        if parent_state is not None:
            parent_state.tokens_used += state.tokens_used
            parent_state.tool_calls_made += state.tool_calls_made
        unregister_active(state.run_id)
        raise

    # ── cancel 传播：子 run 被外部 cancel_node 中止 ─────────────────────────
    if loop_result.status == "cancelled":
        if parent_state is not None:
            parent_state.tokens_used += state.tokens_used
            parent_state.tool_calls_made += state.tool_calls_made
        summary = {
            "run_id": state.run_id,
            "node_type": node_type,
            "project_id": state.project_id,
            "status": "cancelled",
            # 取消不是"没跑过"：轮次照写。这个字段缺席过一次，代价是
            # 下游 run_node 的 `summary["turns"]` 抛 KeyError，**整条子 run 的
            # 结局连同 failure_category 一起丢掉**，调度器只看到一句 KeyError
            # （2026-08-20 前实测 8 次）。消费侧已改成 .get()，产出侧也别缺。
            "turns": loop_result.turns,
            # getattr：custom loop / 测试替身可能交鸭子类型的 result —— 边界上
            # 缺章读成空串（"未盖章"本来就是合法状态），不炸。
            "loop_terminal": {"kind": getattr(loop_result, "terminal_kind", ""),
                              "cause": getattr(loop_result, "terminal_cause", "")},
            "cancel_meta": loop_result.cancel_meta,
            # v3.9（E2E-4 实测）：cancel 路径的 summary 以前硬编码 missing=[] 且
            # 完全不带 upstream_rework_requests。后果：data 节点被自己的 terminal
            # guard 取消了 3 次、每次都调了 request_upstream_rework 申诉，但
            # ① 申诉在 summary 里是 null → _pending_upstream_requests 扫不到 →
            #    永远浮不到 orchestrator 面前（不变量①在 cancel 路径上被违背）；
            # ② 无 missing/failed 信号 → 派发拦截统计里 3 次取消完全隐形，
            #    同一个节点可以被无限重派。
            # cancel ≠ 什么都没发生 —— 该带的账一样要带。
            "missing_required_outputs": [
                _t
                for _t in (
                    harness.required_outputs_for(
                        {"mode": state.hook_state.get("_request_mode")}
                    )
                    or []
                )
                if _t
                not in {
                    a.get("type")
                    for a in state.list_artifacts()
                    if not ((state.read_artifact(a["id"]) or {}).get("metadata") or {}).get(
                        "_forwarded_input"
                    )
                }
            ],
            "upstream_rework_requests": list(
                state.hook_state.get("upstream_rework_requests") or []
            ),
            "turns": loop_result.turns,
            # 「做完了」和「被切断」是两件事 —— 只记 turns 等于逼下游
            # 拿它跟一个它不知道的上限去比（实测：没人比，空转三小时）。
            "max_turns": getattr(loop_result, "max_turns", 0) or 0,
            "stop_reason": summarize_stop_reason(
                turns=loop_result.turns,
                max_turns=getattr(loop_result, "max_turns", 0) or 0),
            "tool_call_count": len(loop_result.tool_calls),
            "tokens_used": state.tokens_used,
            # 归属问题必须用 own_only —— 见 State.list_artifacts 的契约。
            "artifacts": state.list_artifacts(own_only=True),
            "final_text_preview": (loop_result.final_text or "")[:1000],
            "state_dir": str(state.root),
            "project_root": str(state.project_root) if state.project_root else None,
            "depth": depth,
            "sub_run_id": sub_run_id,
        }
        state.write_summary(summary)
        state.append_transcript(
            "run_end",
            **{k: v for k, v in summary.items() if k != "artifacts"},
        )
        unregister_active(state.run_id)
        return summary

    # ── pause 传播：子 run 暂停就把 status 一路冒泡 ─────────────────────────
    if loop_result.status == "paused":
        if parent_state is not None:
            parent_state.tokens_used += state.tokens_used
            parent_state.tool_calls_made += state.tool_calls_made
        summary = {
            "run_id": state.run_id,
            "node_type": node_type,
            "project_id": state.project_id,
            "status": "paused",
            "pause_event": loop_result.pause_event.to_dict() if loop_result.pause_event else None,
            "paused_run_id": state.run_id,  # 这个 run 的 id 也是 pause registry 的 key
            "missing_required_outputs": [],
            "turns": loop_result.turns,
            # 「做完了」和「被切断」是两件事 —— 只记 turns 等于逼下游
            # 拿它跟一个它不知道的上限去比（实测：没人比，空转三小时）。
            "max_turns": getattr(loop_result, "max_turns", 0) or 0,
            "stop_reason": summarize_stop_reason(
                turns=loop_result.turns,
                max_turns=getattr(loop_result, "max_turns", 0) or 0),
            "tool_call_count": len(loop_result.tool_calls),
            "tokens_used": state.tokens_used,
            # 归属问题必须用 own_only —— 见 State.list_artifacts 的契约。
            "artifacts": state.list_artifacts(own_only=True),
            "final_text_preview": "",
            "state_dir": str(state.root),
            "project_root": str(state.project_root) if state.project_root else None,
            "depth": depth,
            "sub_run_id": sub_run_id,
        }
        # 停下来等人之前，先把干出来的活落盘。
        #
        # 「等人」是**无界**的等待：可能几分钟，也可能没人来（2026-08-10 实测
        # 一次无人值守 E2E 在审批门上静默挂了两小时）。把未提交的工作扣在一个
        # 无界等待里，等于把它押在"这个进程能活到有人回答"上。
        #
        # 那次的账单：experiment 节点跑了 40 分钟、提交了真实作业、产出 7 个
        # 产物，进程一没，`experiments/artifacts/`、`repro/`、`runtime/` 全是
        # untracked —— 恢复从源会话 head 开分支，而 head 还停在 experiment
        # 开始**之前**的那次 hypothesis checkpoint 上。全丢。
        #
        # 根因是 checkpoint 只挂在"节点跑完"上，而最长、最贵、最容易被打断的
        # 节点恰恰是最不容易跑完的那个。所以：**任何无界等待之前必须先落盘。**
        # 收尾时还会再请求一次；平台侧对同一批路径是幂等的（没有 diff 就不提交）。
        try:
            from .project_workspace import request_completion_checkpoint

            request_completion_checkpoint(state, "paused")
        except Exception as exc:  # 落盘失败不能反过来把这次暂停搞没
            state.append_transcript(
                "workspace_checkpoint_failed", phase="pause", error=repr(exc)
            )
        # 故意不 write_summary（run 还没结束）
        state.append_transcript(
            "run_paused",
            pending_tool_call_id=loop_result.pause_event.pending_tool_call_id
            if loop_result.pause_event
            else None,
        )
        return summary

    summary = await finalize_run(
        state,
        harness,
        loop_result,
        llm,
        depth=depth,
        sub_run_id=sub_run_id,
    )
    # 子调用结束：把 delta 加回父（并行安全：每个子只算自己的份）
    if parent_state is not None:
        parent_state.tokens_used += state.tokens_used
        parent_state.tool_calls_made += state.tool_calls_made

    return summary


# ── incomplete run 失败分类（issue #106 → #184 扩展）──────────────────────
#
# 背景（2026-07 dogfood 实测，writing 节点 Step 5/6）：部分 OpenAI-compatible
# provider 偶发返回 DSML 协议残片文本（`</｜DSML｜tool_calls>` 等），但
# finish_reason=stop 且 tool_calls=[]。agent_loop 把这当模型自然结束，run
# 进 incomplete —— 这不是节点 prompt/写作质量问题，是 provider 的 tool-call
# 协议兼容性故障，混在一起会让测试记录和排查方向都跑偏。
#
# issue #184（qinp 2026-07 带证据实测）：上面这一种只是**三种**协议失败签名里的
# 一种，另外两种当前全部漏网、被误呈成"节点质量/prompt 问题"：
#
#   - blank_stop：`finish_reason=stop` + `tool_calls==[]` + content 空 + 必需
#     产出缺失。实测 glm-5.1 reviewer run `1785036378-5104c6` 跑 11 轮后空转，
#     `missing_required_outputs=["review_critique"]`。旧逻辑因为文本里没有 DSML
#     marker 直接返回 None。
#   - malformed_tool_args：tool_call 结构上存在、但 args JSON 反序列化失败
#     （实测：`参数 JSON 解析失败：Expecting ',' delimiter: line 1 column 7711`，
#     一份 7.7KB 的 review_critique）。旧逻辑第一行 `if loop_result.tool_calls:
#     return None` 提前返回，**完全命不中**。模型收到 error 后下一轮直接空 stop。
#
# 另外旧逻辑还漏了一种已被 agent_loop 明确识别过的情况：llm.py 的 leak 重试耗尽
# 后，agent_loop 会把 final_text 换成 `[provider tool-call 协议错误] …` /
# `[近乎空响应] …` 兜底文案 —— 那两段文案里**没有** DSML marker，所以走到这里
# 反而分类不出来。一并认掉。
#
# 兼容性硬约束：`failure_category` 的取值保持 `"provider_tool_call_protocol_error"`
# 不变（run_node.py:410 和 tests/test_incomplete_failure_classification.py 在读它，
# `==` 比较）。子类另开一个 `failure_subcategory` 字段承载，不动主类别。
FAILURE_CATEGORY_PROTOCOL = "provider_tool_call_protocol_error"

# issue #426：run 的力气耗在框架自己的门禁上（路径角色拒绝 / 长任务强制走
# submit_job / 派发熔断 …），产出缺失的成因不是节点能力。这类失败**不进**
# 节点的卡死统计（run_history.EXTERNAL_FAILURE_CATEGORIES），否则框架用
# 自己造成的失败把节点永久锁死（nidy2 实测：experiment 连续 6 次
# missing:clean_results 全部死在 submit_job↔safe_run_bash 的门禁死锁上，
# 环境修好后节点仍被拒绝派发）。
# 与 PROTOCOL 不同的是它**不该被机械重派**：门禁拒绝是确定性的，原样重跑
# 必然再撞（见 run_node._is_retryable_infra_failure —— 那个集合不含本类别）。
FAILURE_CATEGORY_FRAMEWORK_GATE = "framework_gate_blocked"

_DSML_TOOLCALL_PROTOCOL_MARKERS = (
    "｜DSML｜tool_calls>",
    "｜DSML｜invoke>",
    "｜DSML｜parameter>",
)
# agent_loop 在 leak 重试耗尽后写进 final_text 的兜底文案前缀（见 agent_loop
# "provider_protocol_leak_surfaced" 分支）。这两条是 provider 故障的**确证**，
# 不是模型没话说。
_SURFACED_MARKUP_LEAK_MARKER = "[provider tool-call 协议错误]"
_SURFACED_EMPTY_LEAK_MARKER = "[近乎空响应]"


def _is_malformed_args_record(record: Any) -> bool:
    """一条 all_tool_calls 记录是不是"调用存在、但 args JSON 解析失败"。

    agent_loop 在 args 解析失败时不执行工具，直接把
    `{"status": "error", "error": "参数 JSON 解析失败：…"}` 当 result 记进
    all_tool_calls（见 agent_loop 里的 `except json.JSONDecodeError`）。
    这与"调用成功执行、只是工具自己返回 error"是两回事 —— 后者是正常业务
    失败，绝不能算协议故障。
    """
    if not isinstance(record, dict):
        return False
    from .tool_call_recovery import is_malformed_args_error

    return is_malformed_args_error(record.get("result"))


def _is_gate_blocked_record(record: Any) -> bool:
    """一条 all_tool_calls 记录是不是"被框架门禁机械拒绝"。

    判据是**结构化字段**，不是工具名单：凡是门禁类拒绝
    （managed_submission_required / unmanaged_background_launch /
    repeated_failure …）都在 result 里带 `blocker: {kind: ...}`（fail-closed
    的机械门约定）。新增一道门只要遵守这个约定就自动被覆盖 —— 不用回来
    改这里（护栏扫结构，不写名单）。

    与 `report_blocker`（agent 主动申报，result.status=="success"）不同：
    这里只认 **status=="error"** 的机械拒绝，两者不会混淆。
    """
    if not isinstance(record, dict):
        return False
    result = record.get("result")
    if not isinstance(result, dict) or result.get("status") != "error":
        return False
    blocker = result.get("blocker")
    return isinstance(blocker, dict) and bool(blocker.get("kind"))


#: 同一种门禁拒绝出现几次算"重定向循环没有收敛"。1 次是门在正常引导
#: （报错文案让 agent 换路径走），3 次同类 = 引导指向的出路不存在或够不着
#: —— 那是框架的账，不是节点的（issue #426：submit_job 拒路径 ↔
#: safe_run_bash 要求走 submit_job，互相指向，agent 撞了几十轮）。
_GATE_BLOCK_PERSISTENT_MIN = 3


def _gate_block_evidence(records: list) -> dict | None:
    """本 run 是否把力气耗在了框架门禁上 → {kind, tool, count} 或 None。

    两条各自封闭的入口（同 malformed_tool_args 的"终结在"判定思路）：

      1. **终结在门上**：最后一次工具调用就是门禁拒绝 —— run 死的时候门
         还关着。
      2. **同类拒绝 ≥ _GATE_BLOCK_PERSISTENT_MIN 次**：agent 换了几种姿势
         仍撞在同一种门上。此时即使 run 尾部有别的收尾调用（写 scratchpad、
         存报告），失败的成因仍是门 —— 产出要是真做出来了，status 就不会是
         incomplete，这个分类器根本不会被调用。

    中途撞了一两次门、之后正常干完活的 run（门在正常工作）两条都不沾。
    """
    blocked = [r for r in records if _is_gate_blocked_record(r)]
    if not blocked:
        return None
    last = records[-1] if records else None
    if _is_gate_blocked_record(last):
        return {
            "kind": str(last["result"]["blocker"].get("kind")),
            "tool": str(last.get("name") or ""),
            "count": sum(
                1 for r in blocked
                if r["result"]["blocker"].get("kind")
                == last["result"]["blocker"].get("kind")
            ),
        }
    by_kind: dict[str, list] = {}
    for r in blocked:
        by_kind.setdefault(str(r["result"]["blocker"].get("kind")), []).append(r)
    kind, hits = max(by_kind.items(), key=lambda kv: len(kv[1]))
    if len(hits) >= _GATE_BLOCK_PERSISTENT_MIN:
        return {
            "kind": kind,
            "tool": str(hits[-1].get("name") or ""),
            "count": len(hits),
        }
    return None


def _classify_incomplete_failure_detail(
    loop_result,
    *,
    missing_required_outputs: list[str] | None = None,
) -> tuple[str | None, str | None]:
    """incomplete run 的失败分类 → (failure_category, failure_subcategory)。

    识别不了返回 (None, None)（不是"没有失败"，是"这个分类器不认识"）。

    误分类防线（验收硬要求：普通材料不足的 blocked run、正常成功 run 绝不能
    被误分类）：

      1. 只在 caller 判定 `status == "incomplete"` 时被调用。
      2. `blank_stop` / `malformed_tool_args` 额外要求 **必需产出确实缺失**
         （`missing_required_outputs` 非空）。产出齐了只是 QC 挂了 = 节点质量
         问题，不是协议问题。`missing_required_outputs=None` 表示"调用方没告诉
         我"，此时这两条一律不触发（保持老签名 `_classify_incomplete_failure(lr)`
         的行为不变）。
      3. `malformed_tool_args` 要求**本 run 最后一次工具调用**就是 args 解析
         失败 —— 即 run 是**终结在**协议故障上的。中途某次 args 坏掉、后面又
         成功干了活的 run（正常自我纠正）不算；"成功调过工具、只是最后没产出"
         的普通 incomplete 也不算（它最后一次调用是成功执行的）。
      4. `blank_stop` 有两条各自封闭的入口，都要求必需产出缺失：
         (i) 本 run **一次工具都没调成**且 final_text 为空；
         (ii) `loop_result.status == "void"` —— agent_loop 的空轮重试用尽的
              机械确证（#253）。这条不看 tool_calls：空停发生在跑了 8 轮检索
              之后照样是空停。
         有正文的 blocked run（"材料不足，无法继续…"）两条都不沾 → 不触发。
    """
    from .tool_call_recovery import PROTOCOL_BREAKER_MARKER

    text = loop_result.final_text or ""
    records = list(getattr(loop_result, "tool_calls", None) or [])
    missing = list(missing_required_outputs or [])

    # (a) 熔断器主动停机 —— 已由 ProtocolFailureBreaker 给出确证诊断，最优先。
    #     （不看 tool_calls：熔断可能发生在跑过若干成功调用之后。）
    if PROTOCOL_BREAKER_MARKER in text:
        return FAILURE_CATEGORY_PROTOCOL, "circuit_break"

    # (b) malformed_tool_args —— 必须在下面的 `if records: return` 之前判，
    #     否则永远命不中（这正是 #184 报的漏洞）。
    if missing and records and _is_malformed_args_record(records[-1]):
        return FAILURE_CATEGORY_PROTOCOL, "malformed_tool_args"

    # (b') 空轮确证 —— agent_loop 的空轮重试（回滚 + 退避重放）已经用尽，仍是
    #      `content="" + tool_calls=[] + finish_reason=stop`。这是**框架自己观测
    #      到的**协议故障，跟"本 run 之前调过多少工具"无关，所以必须排在下面
    #      `if records` 之前。
    #
    #      issue #253（jicq E2E 实测，5 个 project 中 2 个）：literature 跑了
    #      8 轮检索、一个 artifact 都没落地，第 8 轮空 stop。旧逻辑因为
    #      `records` 非空直接返回 (None, None) → 这次 run 只留下"incomplete，
    #      缺 survey_report"，看起来像节点质量问题，实为 provider 空停。
    #      合法收工的模型会说话；空 stop + 必需产出缺失 = 它没收工。
    if missing and getattr(loop_result, "status", None) == "void":
        return FAILURE_CATEGORY_PROTOCOL, "blank_stop"

    # (b'') 框架门禁 —— run 终结在机械门禁上，或同类门禁拒绝达到阈值
    #      （issue #426）。必须排在 `if records` 之前：门禁拒绝的 run 几乎
    #      总是成功调过别的工具。产出缺失 + 力气耗在门上 = 门的账。
    if missing and records:
        gate = _gate_block_evidence(records)
        if gate is not None:
            return FAILURE_CATEGORY_FRAMEWORK_GATE, gate["kind"]

    if records:
        # 本 run 至少成功调过一次工具、且不是终结在 args 解析失败上
        # → 不是协议层失效，交给节点质量归因。
        return None, None

    # (c) DSML 残片 / markup leak 兜底文案
    if (
        any(m in text for m in _DSML_TOOLCALL_PROTOCOL_MARKERS)
        or _SURFACED_MARKUP_LEAK_MARKER in text
    ):
        return FAILURE_CATEGORY_PROTOCOL, "dsml_leak"

    # (d) blank_stop：零工具调用 + 无正文（或"近乎空响应"兜底文案）+ 必需产出缺失
    if missing and (_SURFACED_EMPTY_LEAK_MARKER in text or not text.strip()):
        return FAILURE_CATEGORY_PROTOCOL, "blank_stop"

    return None, None


def _classify_incomplete_failure(
    loop_result,
    *,
    missing_required_outputs: list[str] | None = None,
) -> str | None:
    """向后兼容入口：只返回主类别（老调用方 / 老测试用的就是这个签名）。"""
    return _classify_incomplete_failure_detail(
        loop_result,
        missing_required_outputs=missing_required_outputs,
    )[0]


PROVENANCE_CHECK_NAME = "data_provenance_declared"


def _data_provenance_check(state, harness) -> dict | None:
    """always-on 机械判据：复用了比本 run 还老的外部数据就必须声明来源。

    只管 producing 节点 —— 系统节点（_curator/_reviewer/_orchestrator）本来就是
    读全项目历史的，它们"读旧东西"是职责不是问题。

    `HARNESS_PROVENANCE_GATE=warn` 可降级为只留痕不拦（应急用；默认拦）。
    """
    import os as _os

    from core import data_provenance as _dp

    if str(harness.node_type or "").startswith("_"):
        return None
    rows = _dp.undeclared_stale_inputs(state)
    if not rows:
        reads = _dp.external_reads(state)
        auto = _dp.auto_provenance(state)
        _auto_note = ""
        if auto:
            _srcs = sorted({a["source_run_id"] for a in auto})
            _auto_note = (
                f"另有 {len(auto)} 个文件来自本项目前序 run"
                f"（{', '.join(_srcs[:4])}），provenance 由框架自动出具，"
                "无需手工声明。"
            )
        return {
            "name": PROVENANCE_CHECK_NAME,
            "passed": True,
            "mechanical": True,
            "dimension": "scientific",
            "auto_provenance": auto,
            "reasoning": (
                f"机械判定：本 run 碰过 {len(reads)} 个外部文件，其中比 run 起始"
                f"更早且未声明来源的 0 个。{_auto_note}"
            ),
        }
    mode = (_os.getenv("HARNESS_PROVENANCE_GATE") or "enforce").strip().lower()
    msg = _dp.format_violation(rows, started_at=_dp.run_started_at(state))
    state.append_transcript(
        "undeclared_stale_inputs",
        count=len(rows),
        mode=mode,
        paths=[r["path"] for r in rows[:10]],
    )
    return {
        "name": PROVENANCE_CHECK_NAME,
        "passed": (mode == "warn"),
        "mechanical": True,
        "dimension": "scientific",
        "reasoning": (("[warn 模式，未拦截] " if mode == "warn" else "") + msg),
    }


# ══ issue #221：协调者的 run status 必须反映"它编排的工作闭环了没" ══════════
#
# 实测（jicq E2E，基线 4a484ee）：orchestrator 依次派了 literature → hypothesis
# → experiment → …，其中关键下游节点 experiment（run `1785307479-d15500`）明确
# `status=incomplete`、最终交付物没齐，而 **orchestrator 自己 run 的 summary.json
# 写的是 `completed`**。平台汇总只读这个 status → 向用户展示"任务已完成"；自动化
# 系统据此停止重试 / 提前通知完成。要发现流程根本没闭环，只能人工翻节点级 summary。
#
# 根因是**判据的覆盖面**，不是哪一行逻辑写错：`final_status` 只看两样东西 ——
# `missing`（本 run 自己的 `required_output_artifact_types`）和 `qc_failed`。而
# `nodes/_orchestrator/harness.yaml` 里 `required_output_artifact_types: []`、
# 协调者本来就不自己产 artifact、不
# 自己做研究。于是对它来说"进程正常退出"就等于 completed。
#
# 修法：给协调者补一条**它专属**的完成度判据 —— 它编排出去的工作有没有闭环。
# 判据只读机械账本（flow 账本 / TaskList / 子 run 的 summary.json），一个字都不
# 问 LLM。"LLM 说已完成"从来不是这里的输入 —— chat.py 的 continuous 终态门禁早就
# 用同一批机械信号驳回过模型自报的 `CONTINUOUS_STATUS: complete`，缺的只是把同
# 一条不变量接到 **run status** 这条路径上（"机制存在但没接到路径"的老毛病）。
#
# ⚠️ 判据本体已经**不在这个文件里**了：接到两条路径之后，"编排的工作闭环了没"就有
# 了两份实现（这里 + chat.py 的终态门禁），口径今天一致、明天没人保证 —— 那正是
# `core/run_history.py` 模块文档写的那件事（9 处各自推导 → 一天四个 PR 收拾后果）。
# 现在唯一权威推导在 **`core/closure.py`**（本路径用 `closure.RUN_STATUS` 口径）。
# 这里只剩本路径专属的两件事：投影成 summary.json 的形状，以及下面这道防误杀门。
#
# ── 为什么必须有"本 run 编排过 producing 节点"这道前置门（防误杀）──────────
#
# orchestrator 是长驻交互节点：chat.py 一个 session 复用同一个 state / run_id
# （`orchestrator__<project>`），每次 pause 链被解开都会重写一次它的 summary.json。
# 也就是说 finalize_run 对它而言**不是**"会话结束"，而是"某个人工决策点刚被答复"。
# 那种时刻"项目里还有 pending task"是完全正常的中间态 —— 用户只是问了句状态、
# 刚起完一个节点等下一轮，都会命中。若不加门，每一轮正常对话都会变 incomplete：
# 那是把一个误报换成另一个误报，用户照样不能信这个字段。
#
# 所以降级需要**两个条件同时成立**：
#   ① 本 run 真的编排过 producing 节点（transcript 里有 producing 子 run 事件，
#      或 hook_state 里有 post-producing flow 账本条目）；
#   ② 它留下了未闭环的工作。
# 纯对话轮永远不满足 ①，status 一律不动 —— 但未闭环项**照样如实写进**
# `summary["orchestration_closure"]`。"不降级"不等于"不告诉你"。

ORCHESTRATOR_NODE_TYPE = "_orchestrator"

ORCHESTRATION_CLOSURE_KEY = "orchestration_closure"

# incomplete 的归因字段。新增取值是安全的：现有两个消费方分别是
# `run_node.py:410`（`== "provider_tool_call_protocol_error"`）和
# `shared/tools/run_node.py:518`（`not in _INFRA_FAILURE_CATEGORIES`）——
# 新值在前者不命中、在后者等价于今天的 None。
FAILURE_CATEGORY_ORCHESTRATION_NOT_CLOSED = "orchestration_not_closed"

# summary / transcript 里每类 open item 最多带几条（长 session 的 task 列表可能
# 很长；这里是给人和 UI 定位用的清单，不是账本本身）。
_CLOSURE_ITEM_CAP = 20




def summarize_stop_reason(*, turns: int, max_turns: int) -> str:
    """这次循环是**做完了**还是**被轮次上限切断**。

    判据是框架自己的循环出口，不是模型自述 —— 模型说"我做完了"和它其实是
    被切断，在 transcript 里长得一模一样。
    """
    from core import agent_loop as _al

    if max_turns and int(turns or 0) >= int(max_turns):
        return _al.STOP_MAX_TURNS
    return _al.STOP_FINISHED


def truncated_by_turn_cap(summary: dict) -> bool:
    """这份 summary 描述的 run 是被轮次上限切断的吗。

    缺 `max_turns` 时返回 **False** —— 把正常完成误判成残缺会让流程无限
    revise，那比漏判更贵（漏判只是退回今天的行为）。
    """
    try:
        cap = int(summary.get("max_turns") or 0)
        used = int(summary.get("turns") or 0)
    except (TypeError, ValueError):
        return False
    return bool(cap) and used >= cap

def _closure_open_items(work: closure.OpenWork) -> list[dict]:
    """把权威判定结果投影成 summary.json 里的 `open_items` 形状。

    **什么算 open 不在这里判** —— 那是 `core/closure.py` 的事（同一个判据的另一个
    消费方是 chat.py 的 continuous 终态门禁，两处各写一遍就是 run_history 那篇模块
    文档描述的接缝）。这里只负责**呈现**：字段名是 summary.json 的对外契约，平台
    汇总 / UI / `nodes/_orchestrator/harness.yaml` 都按它读。
    """
    items: list[dict] = [
        {
            "kind": "post_node_flow",
            "producing_node": e.get("producing_node"),
            "producing_run_id": e.get("producing_run_id"),
            # 只用来给人**定位**卡在 3 步的哪一步，不参与"算不算 open"。
            "open_step": closure.flow_open_step(e),
            "review_state": e.get("review_state"),
            "decision_state": e.get("decision_state"),
            "task_id": e.get("task_id"),
        }
        for e in work.open_flow_entries
    ]
    # kind="curator_integration" 的未闭合项已删除：curator 退出 flow 后
    # `open_curator_entries` 恒空（core/closure.py），留着只是一段永不触发的分支。
    items += [
        {
            "kind": "child_run",
            "node_type": a.node_type,
            "run_id": a.run_id,
            "status": a.status,
            "started_attempts": work.started_attempts.get(a.node_type, 0),
            "missing_required_outputs": list(a.missing_required_outputs),
        }
        for a in work.unresolved_producers
    ]
    items += [
        {
            "kind": "child_run_no_terminal_record",
            "node_type": s.node_type,
            "started_attempts": s.started_attempts,
            "note": "本 run 起过它，但 transcript 和磁盘上都没有任何终态/暂停记录"
            "（后台仍在跑 / 崩溃 / 落盘前进程结束）",
        }
        for s in work.started_without_terminal_record
    ]
    items += [
        {
            "kind": "open_task",
            "task_id": t.task_id,
            "status": t.status,
            "title": t.title,
            "owner_node": t.owner_node,
            "blocked_reason": t.blocked_reason,
        }
        for t in work.open_tasks
    ]
    # 未了结的 blocking 义务（申诉 / 重复失败 / 冻结承诺）。chat.py 的终态门禁一直
    # 看它；run status 侧原来不看 —— 于是"终态门禁拒绝 complete，同一刻
    # summary.json 写 completed"这种自相矛盾是可能的，那就是 #221 换了个字段复发。
    items += [
        {
            "kind": "blocking_obligation",
            "obligation_kind": o.kind,
            "owed_by": o.owed_by,
            "claimed_by": o.claimed_by,
            "what": o.what[:200],
            "source_run_id": o.source_run_id,
        }
        for o in work.blocking_obligations
    ]
    return items


def _render_closure_note(open_items: list[dict]) -> str:
    """给用户/UI 看的一句实话 —— 未闭环时具体卡在哪。"""
    if not open_items:
        return ""
    parts: list[str] = []
    for item in open_items[:8]:
        kind = item.get("kind")
        if kind == "child_run":
            parts.append(
                f"{item.get('node_type')}[{item.get('run_id')}] "
                f"status={item.get('status')}"
                + (
                    f"，缺 {item['missing_required_outputs']}"
                    if item.get("missing_required_outputs")
                    else ""
                )
            )
        elif kind == "child_run_no_terminal_record":
            parts.append(f"{item.get('node_type')} 起过但无终态记录")
        elif kind == "post_node_flow":
            parts.append(
                f"{item.get('producing_node')} 的 post-producing flow 卡在 {item.get('open_step')}"
            )
        elif kind == "curator_integration":
            parts.append(f"{item.get('producing_node')} 的 KB 整合未完成")
        elif kind == "open_task":
            parts.append(f"task {item.get('task_id')}({item.get('status')}) {item.get('title')}")
        elif kind == "blocking_obligation":
            parts.append(
                f"未了结义务[{item.get('obligation_kind')}] "
                f"{item.get('owed_by') or '归属待定'} 欠：{item.get('what')}"
            )
        else:
            # 新增 kind 忘了写渲染分支时，宁可渲染得丑，也不能让它从这句实话里
            # 消失（"没渲染"读起来跟"没这回事"一样）。
            parts.append(f"{kind}: {item}")
    more = len(open_items) - len(parts)
    tail = f"；另有 {more} 项" if more > 0 else ""
    return (
        "⚠️ 本 run 编排的工作尚未闭环（共 "
        f"{len(open_items)} 项）："
        + "；".join(parts)
        + tail
        + "。向用户汇报时必须如实说明卡在哪，不得声称任务已完成。"
    )


def compute_orchestration_closure(state: State, harness: NodeHarness) -> dict | None:
    """协调者的闭环账：它编排出去的工作到底完没完。

    `None` = 判据不适用（非协调者节点）。producing 节点的完成度由它自己的
    `required_outputs` 判，不走这条。

    暂时只对 `_orchestrator` 生效：它是唯一"自己不产 artifact、全部工作靠派子
    节点"的节点，也就是唯一会被"进程没崩 = completed"这条判据完全放空的节点。
    逻辑本身对任何会派子节点的 harness 都成立，要推广时把这里的 node_type 判断
    换成"有没有 run_node 工具"即可。

    事实来源是 `core/closure.py`（`RUN_STATUS` 口径）—— 与 chat.py 的 continuous
    终态门禁同一个判据、同一份实现。这里只剩两件本路径专属的事：
      ① 投影成 summary.json 的 `open_items` 形状（对外契约）；
      ② **防误杀的降级决定** —— 见 `OpenWork.orchestrated_producing_work` 与本节
         顶部那段长注释：纯对话轮永远不降级 status，但未闭环项照样如实写进
         summary。"不降级"不等于"不告诉你"。
    """
    if str(harness.node_type or "") != ORCHESTRATOR_NODE_TYPE:
        return None

    work = closure.open_work(state, closure.RUN_STATUS)
    open_items = _closure_open_items(work)
    orchestrated_producing = work.orchestrated_producing_work
    # 用投影后的 items 判 closed（而不是 work.any_open）：这样 summary 里
    # `closed` 与 `open_items` 永远自洽 —— 下游读到 closed=False 就一定能在
    # open_items 里看到具体是什么，不会出现"说没闭环但列不出来"。
    closed = not open_items
    return {
        "node_type": ORCHESTRATOR_NODE_TYPE,
        "closed": closed,
        "orchestrated_producing_nodes": list(work.orchestrated_producing_nodes),
        "orchestrated_producing_work": orchestrated_producing,
        # ② 只有"编排过 + 有未闭环项"才降级 status（防误杀，见本节顶部长注释）。
        "downgrades_status": bool(orchestrated_producing and not closed),
        "open_item_count": len(open_items),
        "open_items": open_items[:_CLOSURE_ITEM_CAP],
        "open_items_truncated": max(0, len(open_items) - _CLOSURE_ITEM_CAP),
        "user_facing_note": _render_closure_note(open_items),
    }


async def finalize_run(
    state: State,
    harness: NodeHarness,
    loop_result,
    llm: LLMClient,
    *,
    depth: int = 0,
    sub_run_id: str | None = None,
) -> dict[str, Any]:
    """run_loop 完成后校验必需产物 + 写 summary.json。

    被 execute_node 内部调用；也被 pause_driver 在 resume cascade 完成 leaf child 后调用，
    确保 paused → resumed → completed 的 run 也写完整 summary.json。
    """
    project_workspace_mode = getattr(state, "project_worktree", None) is not None

    # Legacy CLI runs still use the run-local typed-artifact contract. Project
    # v2 runs use owned Git directories as the durable handoff and therefore do
    # not treat an absent save_artifact call as missing work.
    # v3.2 provenance 审计：artifacts/ 里凡不在账本上的文件 = 绕过框架直写
    # （shell 重定向 / 动态拼路径的 python 写 —— pattern 拦截层漏掉的兜底）。
    # 来路不明的 artifact 不算合法 deliverable：不满足 required_outputs。
    # 出处审计已并入账本（core/ledger）：每条记录都带产出方，没有账外记录这回事。
    unverified: list = []
    unverified_ids = {u["id"] for u in unverified}
    if unverified:
        state.append_transcript(
            "artifact_provenance_unverified",
            artifacts=unverified,
            note="文件存在于 artifacts/ 但无 save_artifact 账本记录 —— 不计入合法产出",
        )

    # 完成度门禁只认**本 run 真产出**：排除 (a) unverified（绕过框架直写的 ghost）
    # + (b) forwarded input（转发进来的上游输入，标了 `_forwarded_input`）。agent 若
    # 本 run 真 save_artifact 覆盖了转发进来的那份，覆盖写入不带该标记的新 metadata
    # → 重新计入（合法 revise 照常通过；只是"啥都不产、白蹭转发件"不再算完成）。
    def _is_forwarded_input(aid: str) -> bool:
        rec = state.read_artifact(aid)
        return bool(rec and (rec.get("metadata") or {}).get("_forwarded_input"))

    produced_types = {
        a["type"]
        for a in state.list_artifacts(own_only=True)   # 归属判定：只算自己产的
        if a["id"] not in unverified_ids and not _is_forwarded_input(a["id"])
    }
    # Workspace-First 下**框架不判存在性**。
    #
    # 判据的归属：
    #   生产方不知道有没有人需要这个产物 —— 它判"我该不该产 X"是在猜，
    #     猜错的代价是连败 4 次触发熔断（v22：writing 13 次、postprocess 7 次
    #     「⛔ 拒绝再次启动」）。
    #   消费方知道自己要什么，而且**就坐在自己的 loop 里等结果** —— 它拒绝
    #     开工，损失是零，没人白跑。
    #
    # 这些门本来就全都在，而且是 fail-closed 的：experiment 的
    # `audit_experiment_preflight`（pre_registration 不存在或没冻结 →
    # blocking）、writing 的 `blocked_missing_required_upstream` /
    # `blocked_insufficient_kb_claims`、hypothesis / data / postprocess 各有
    # 自己的。生产方那份是纯冗余，而且是会死锁的那份冗余。
    #
    # ⚠️ 别再用"节点按模式声明该产什么"来替代 —— 那就是节点间交付 artifacts
    # 契约换了个名字，v2.1 拆掉它是有意的决定（"以后没有强制产物，每个节点
    # 维护一个自己的文件目录"）。我第一版就是这么修的，是错的。
    missing = (
        []
        if project_workspace_mode
        else [
            t
            for t in harness.required_outputs_for(
                {"mode": state.hook_state.get("_request_mode")}
            )
            if t not in produced_types
        ]
    )

    project_workspace_result = None
    if project_workspace_mode:
        try:
            from .project_workspace import workspace_snapshot

            project_workspace_result = workspace_snapshot(state)
        except Exception as exc:  # summary must survive observation failure
            state.append_transcript(
                "workspace_summary_failed",
                error=f"{type(exc).__name__}: {exc}"[:500],
            )

    blockers = list(state.hook_state.get("blockers") or [])
    final_status = "completed" if not missing else "incomplete"
    if blockers:
        final_status = "blocked"

    # ── 第二条轴：任务结局（#1086）──────────────────────────────────────
    # `status` 回答的是「收尾收干净了吗」。「交代的活干成了吗」是另一个问题，
    # 此前没有字段，节点想让它可见只能写一条 blocker —— 一写就是 `blocked`，
    # 于是任务 failed / 任务 partial / 真 blocked / 收尾故障四种局面共用一个词。
    from .task_outcome import status_floor, task_outcome as _task_outcome

    node_task_outcome = _task_outcome(state)
    final_status = status_floor(
        (node_task_outcome or {}).get("outcome"), final_status)

    # ── 交付地板（v2.1 P-QC1 机械半边）────────────────────────────────────
    # workspace 模式下 required_outputs 不再当存在性门（missing 恒 []）、
    # figure 类完整性检查也改成条件触发（require_if_present）——那"这次到底
    # 交没交东西"就必须有人问，否则空跑也 completed。最低判据是机械事实：
    # 自己的 Git 作用域里**一个未提交的改动都没有**（snapshot=None 已含
    # artifacts —— save_artifact 落的就是这个目录）。
    # "交的东西对不对口"归调用方（服务）/ reviewer（producing）判；地板只拦
    # "什么都没交"。系统节点（_orchestrator 有 closure 判据、_curator 写
    # MEMORY.md/KB、_reviewer 产 critique）不适用。
    delivery_empty = False
    if (
        final_status == "completed"
        and project_workspace_mode
        and not str(harness.node_type or "").startswith("_")
        and project_workspace_result is None
    ):
        delivery_empty = True
        final_status = "incomplete"
        state.append_transcript(
            "delivery_empty",
            note="run 判 completed 但工作区作用域零改动 —— 没有交付物",
        )

    # ── issue #221：协调者的完成度判据（详见 compute_orchestration_closure）──
    # 判据自身炸了**不能**让 finalize 挂（否则连 summary.json 都没有，比误报更糟）。
    # 但也绝不吞：写一条带 error 的 closure + transcript 事件，并明说 status 未经
    # 这条判据校验 —— 这样"判据哑了"是看得见的，不会静默退回 #221 那个老 bug。
    # 局部名刻意不叫 `closure` —— 那是 core/closure.py 这个模块名。
    orch_closure: dict | None
    try:
        orch_closure = compute_orchestration_closure(state, harness)
    except Exception as _exc:  # noqa: BLE001
        log.warning("orchestration closure 判定失败 run=%s: %s", state.run_id, _exc, exc_info=True)
        state.append_transcript(
            "orchestration_closure_failed",
            error_type=type(_exc).__name__,
            error_message=str(_exc)[:500],
        )
        orch_closure = {
            "node_type": str(harness.node_type or ""),
            "closed": None,
            "downgrades_status": False,
            "error": f"{type(_exc).__name__}: {_exc}"[:500],
            "user_facing_note": (
                "⚠️ 闭环判据自身执行失败 —— 本 run 的 status **未经**"
                "「编排的工作是否闭环」校验，不能当作已闭环的证据。"
            ),
        }
    if orch_closure is not None and orch_closure.get("downgrades_status"):
        final_status = "incomplete"
        state.append_transcript(
            "orchestration_not_closed",
            open_item_count=orch_closure["open_item_count"],
            open_items=orch_closure["open_items"],
            orchestrated_producing_nodes=orch_closure["orchestrated_producing_nodes"],
        )

    # issue #184：主类别保持不变（下游 `== "provider_tool_call_protocol_error"`
    # 的消费方不能被破坏），子类另开字段。missing 传进去是关键判据之一 ——
    # "产出齐了只是 QC 挂了"永远不该被归到 provider 协议故障头上。
    failure_category = None
    failure_subcategory = None
    # 门禁证据无论分类结果如何都落盘（证据可持久化，判决不可以 ——
    # 分类规则将来改进时，历史 run 凭这份证据仍可被重判）。
    gate_block_evidence = _gate_block_evidence(
        list(getattr(loop_result, "tool_calls", None) or [])
    )
    if final_status == "incomplete":
        failure_category, failure_subcategory = _classify_incomplete_failure_detail(
            loop_result,
            missing_required_outputs=missing,
        )
        # E2E-5b：连续撞满输出预算且 content/tool_calls 全空、恢复重试用尽 ——
        # 判决性重放证明是 provider 瞬态故障（同请求事后正常完成）。归 infra 账：
        # 派发层机械重派（换个时刻本身就是对瞬态故障的有效扰动），不进节点的
        # 卡死统计。沿用既有主类别，消费方（_INFRA_FAILURE_CATEGORIES）零改动。
        if failure_category is None and state.hook_state.get("_zero_output_truncation"):
            failure_category = FAILURE_CATEGORY_PROTOCOL
            failure_subcategory = "zero_output_truncation"
        # #501：空 SSE 走完非流式回退**仍然**没有正文/推理/工具调用（或 provider
        # 200 却一个 choice 都没有）。同一件事的另一条入口：成因在 provider 侧，
        # 归 infra 账、可机械重派，不进节点卡死统计。
        if failure_category is None and state.hook_state.get("_provider_void_response"):
            failure_category = FAILURE_CATEGORY_PROTOCOL
            failure_subcategory = "provider_void_response"
    # #221：协调者被闭环判据降级时，标准归因字段本来会是全空（它没有 required
    # output，也不是 provider 故障）——"incomplete 但查不到
    # 原因"等于把排查成本又推回人工翻日志。给它一个明确类别。
    if (
        final_status == "incomplete"
        and failure_category is None
        and orch_closure is not None
        and orch_closure.get("downgrades_status")
    ):
        failure_category = FAILURE_CATEGORY_ORCHESTRATION_NOT_CLOSED

    # ── #748：已裁决的命题投影成 KB claim —— 框架做，不等模型想起来起 curator ──
    # 判据是事实不是名单：本 run 自己产出了 research_state，就把里面的裁决投影
    # 出去（同一条 write_claim / update_claim_status 路径，闸一道不少）。幂等，
    # 失败只记账不杀 finalize —— summary.json 比一次投影重要。
    kb_projection: list[dict] | None = None
    if "research_state" in produced_types:
        try:
            from shared.tools.library.kb import project_research_state_to_kb

            _rs_entries = [a for a in state.list_artifacts(artifact_type="research_state",
                                                            own_only=True)]
            _rs = state.read_artifact(_rs_entries[-1]["id"]) if _rs_entries else None
            if isinstance(_rs, dict):
                kb_projection = await project_research_state_to_kb(state, _rs)
        except Exception as _exc:  # noqa: BLE001
            state.append_transcript(
                "kb_claim_projection_failed",
                error=f"{type(_exc).__name__}: {_exc}"[:500],
            )

    summary = {
        "run_id": state.run_id,
        # #1081：这个 run 是续上去的还是新开的。`requested_run_id` 有值而
        # `resumed` 为 False，就是「请求续跑 X，没续上，换了这个 run」——
        # 调用方据此决定要不要按新开处理，而不是拿参数和返回值人工比对。
        "resume": dict(state.hook_state.get("_resume_outcome")
                       or {"requested_run_id": None, "resumed": False, "reason": None}),
        # #748：本 run 的裁决投影结果（None = 本 run 没产 research_state）。
        "kb_claim_projection": kb_projection,
        "node_type": harness.node_type,
        "project_id": state.project_id,
        "status": final_status,
        "failure_category": failure_category,
        "failure_subcategory": failure_subcategory,
        # #426：门禁拒绝的机械证据（{kind, tool, count} 或 None）。None 也照写
        # —— "扫过了，没有"和"根本没扫"（老 summary 缺字段）是两回事。
        "gate_block_evidence": gate_block_evidence,
        # #1086：**任务**干成了没有（success/partial/failed/blocked/cancelled），
        # 与上面的 `status`（收尾健不健康）各答各的。`None` = 节点没报告过，
        # **不等于 success** —— 恒写，缺席要看得见。
        "node_task_outcome": node_task_outcome,
        # #1097 第 4 条：这一趟**关掉了父任务的哪一部分**。由节点的收尾收据派生，
        # 不是自由文本。`None` = 没报告，父侧 reducer 据此保守处理（不替它声明）。
        "child_obligation_effect": (
            dict(state.hook_state.get("child_obligation_effect") or {}) or None),
        "missing_required_outputs": missing,
        # 本 run **自己**落地的 artifact 类型（已排除 ghost 与转发件）—— 与上面
        # 完成度门禁用的是同一个 `produced_types`，不另算一遍。消费方：
        # run_node 的基础设施重试判据（"重跑会不会毁掉已有成果"的唯一权威依据，
        # 见 _is_retryable_infra_failure）。
        "produced_artifact_types": sorted(produced_types),
        "unverified_artifacts": unverified,  # v3.2 provenance 审计（空=全部合法）
        "delivery_empty": delivery_empty,  # v2.1 交付地板（见上）
        "turns": loop_result.turns,
        # #735：怎么结束的、算谁的（stop/cancelled/failure）。status 是老词表
        # （max_turns 在里面叫 completed），新消费者请读这两个字段。
        "loop_terminal": {"kind": getattr(loop_result, "terminal_kind", ""),
                          "cause": getattr(loop_result, "terminal_cause", "")},
        "tool_call_count": len(loop_result.tool_calls),
        "tokens_used": state.tokens_used,
        # 归属问题必须用 own_only —— 见 State.list_artifacts 的契约。
        "artifacts": state.list_artifacts(own_only=True),
        "final_text_preview": (loop_result.final_text or "")[:1000],
        "state_dir": str(state.root),
        "project_root": str(state.project_root) if state.project_root else None,
        "depth": depth,
        "sub_run_id": sub_run_id,
        # v3.6：节点申诉"根因在上游"（request_upstream_rework 登记的诉求）。
        # 调度权仍归 orchestrator —— 这里只把诊断结论如实带出去，避免它像前三轮
        # 那样丢失，然后被无意义地重跑。
        "upstream_rework_requests": list(state.hook_state.get("upstream_rework_requests") or []),
        "blockers": blockers,
        "memory_candidates": list(state.hook_state.get("memory_candidates") or []),
        "project_workspace": project_workspace_result,
    }
    # #221：协调者独有的字段（其它节点的 summary 形状一个字节都不变）。
    # 即使 closed=True 也照写 —— "查过了，闭环了"和"根本没查"是两回事，
    # 只在异常时才出现的字段没法被下游当成契约用。
    if orch_closure is not None:
        summary[ORCHESTRATION_CLOSURE_KEY] = orch_closure
    # #524 / #395-9：报了 blocker 就把"我卡在什么局面上"一起落盘。派发闸据此
    # 回答"再跑一次值不值"（core/dispatch_gate.py）。只在真有 blocker 时算 ——
    # 正常路径零开销。
    if blockers:
        try:
            from .dispatch_gate import BLOCKED_SITUATION_KEY, capture

            situation = capture(
                state, harness.node_type, state.hook_state.get("node_inputs"))
            if situation:
                summary[BLOCKED_SITUATION_KEY] = situation
        except Exception as exc:  # noqa: BLE001
            state.append_transcript(
                "blocked_situation_capture_failed",
                error=f"{type(exc).__name__}: {exc}"[:300],
            )
    state.write_summary(summary)
    state.append_transcript(
        "run_end",
        **{k: v for k, v in summary.items() if k != "artifacts"},
    )

    # run-end episode 层已整体删除（2026-08-21 记忆重建）。
    # 它每个 producing run 烧 1–2 次 LLM call 写一份叙事摘要，而读取方接近零：
    # 无任何自动注入路径、只有 5 个节点有读工具、索引里连文件名都不出现 ——
    # 审计认定是整个记忆系统里投入产出比最差的一段。
    # 日志已经有三份权威载体（transcript / Git history / 决策账本），
    # 第四份不产生信息。要叙事摘要就从 transcript 现生成 —— 那是投影，
    # 不是新的真相源。

    # Git is a Platform-owned workspace concern, not a node concern.  Every
    # node reaches this point, including pause-resumed runs, so one shared hook
    # can request a node-level checkpoint without editing six node packages.
    try:
        from .project_workspace import request_completion_checkpoint

        request_completion_checkpoint(state, final_status)
    except Exception as e:  # a missing observer must not erase the run summary
        state.append_transcript(
            "workspace_checkpoint_request_failed",
            error=f"{type(e).__name__}: {e}"[:500],
        )

    # Active 注册表撤销 —— run 真正结束（completed/incomplete）。
    # paused run 在这里 NOT 到达；resume 后 pause_driver 调本函数才到达。
    unregister_active(state.run_id)
    return summary
