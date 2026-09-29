"""上下文引擎：为 agent loop 拼装 system + 第一条 user prompt。

输入：
  - NodeHarness（system prompt、rules、guidelines、skills、expected_outputs）
  - State（上游 artifact、memory）
  - 可选的用户提供的节点输入（`fixture.node_inputs`）

输出：
  - 一组可直接喂给 agent loop 的 LLMMessage。

完整平台的上下文引擎有 6 个检索槽 + token 预算 + KB 混合搜索 + skill 激活
规则。这里是教学版本 —— 读一遍就能理解完整版的形状。
"""

from __future__ import annotations

from pathlib import Path

from .harness import NodeHarness
from .llm import LLMMessage, opening_system_prompt
from .skill_registry import render_skills
from .state import State


def build_messages(
    harness: NodeHarness,
    state: State,
    node_inputs: dict | None = None,
) -> list[LLMMessage]:
    """为 agent loop 拼出初始消息。

    结构：
      [
        system:  harness.system_prompt + rules + guidelines + skills,
        user:    expected_outputs + 近期 memory + 上游 artifact 列表 + node_inputs,
      ]
    """
    system = _build_system_prompt(harness, state)
    user = _build_user_prompt(harness, state, node_inputs or {})
    # ── 启动注入清单（issue #737，抄 CC SubAgent 的"拿到/拿不到什么是清单"）──
    # 跨节点交接失败史（experiment 读不到 prereg、#621 豁免命名空间错位）的
    # 共同根因：子侧启动上下文没有契约，缺了不报错。清单由**真实 append 位点**
    # 逐处标记而来（不是事后扫文案），落 transcript ——「这个 run 开工时收到了
    # 什么」从此可查可测。
    if state is not None:
        manifest = {
            "system": list(state.hook_state.pop("_ctx_manifest_system", [])),
            "user": list(state.hook_state.pop("_ctx_manifest_user", [])),
            "node_input_keys": sorted((node_inputs or {}).keys()),
            # 「这一轮读到的指令是哪一份」是**证据**，所以它跟着这一轮走，落在
            # 同一条清单上（RFC X3）。从前的答案在会话行上冻着一列，与本轮无关：
            # 它记的是三个星期前建会话那一刻的事，回答不了"这一轮用的是什么"。
            "instruction_digests": dict(state.hook_state.pop("_ctx_instruction_digests", {})),
        }
        state.hook_state["_startup_injection_manifest"] = manifest
        try:
            state.append_transcript("startup_injection_manifest", **manifest)
        except Exception:
            pass       # 观察不打断主流程（fixture state 可能没有 transcript）
        _write_first_turn_node_inputs_receipt(state, node_inputs)
    return [
        opening_system_prompt(system),
        LLMMessage(role="user", content=user),
    ]


#: 首轮 node_inputs 收据的契约名（#1084）。节点按这个名字找它。
TYPED_FIRST_TURN_NODE_INPUTS_RECEIPT = "typed_first_turn_node_inputs_receipt"


def _write_first_turn_node_inputs_receipt(state, node_inputs: dict | None) -> None:
    """把「这个 run 首轮实际收到了什么」写成一份**带类型、只写一次**的收据。

    ## 为什么渲染文本不够

    首轮输入以前只有两种留痕：`startup_injection_manifest` 只有排好序的**键名**，
    `loop_seed` 是渲染后的 Markdown 全文。带类型的值只在 `hook_state["node_inputs"]`
    里，而那是进程内可改写、不落盘的东西，resume 时 `State.reopen` 也不恢复它。

    于是节点要确认「模型首轮拿到的任务正文」只能去解析 `## 节点输入` 那一段，而
    那一段靠行首 `- **键**：` 划分字段 —— **任何输入值里都能写出同样的一行**。
    正反两面都成立：合法任务正文第二行恰好长这样就被误拒（#1084 给了复现），
    在别的输入里写一行 `- **experiment_spec**：…` 就能冒充任务正文。节点区分不了，
    只能一起拒。这不是节点写错了，是 Core 没给它一个可认证的事实。

    ## 三条判据

    - **带类型**：原值按 JSON 原样留着，外加每个键的 sha256 和一个整体摘要
      （引文比对要原文，防篡改要摘要，两样都要）。
    - **只写一次**：靠「已经有了就不写」保证，不靠 resume 标志 —— `resume_run_id`
      为真时 reopen 也可能失败、退回新开一个 run（见 `core/executor.py`），
      那一刻它其实是首轮。resume 带来的新请求另记一条 `resumed_node_inputs`，
      不覆盖这份收据。
    - **读不到要报错**：`read_first_turn_node_inputs_receipt` 读不到或读到多份时
      抛异常，不返回空 —— 空值会被当成"这个 run 没有输入"，而那是另一件事。
    """
    try:
        import hashlib
        import json as _json

        path = state.root / "first_turn_node_inputs.json"
        values = dict(node_inputs or {})

        def _sha(value) -> str:
            raw = _json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()

        receipt = {
            "contract": TYPED_FIRST_TURN_NODE_INPUTS_RECEIPT,
            "run_id": getattr(state, "run_id", None),
            "node_type": getattr(state, "node_type", None),
            "values": values,
            "value_sha256": {k: _sha(v) for k, v in values.items()},
            "digest": _sha(values),
        }
        if path.exists():
            # 已经有了 —— 这不是首轮。把这次派发带来的输入另记一条，
            # 收据本身一个字节不动。
            try:
                state.append_transcript(
                    "resumed_node_inputs",
                    keys=sorted(values),
                    digest=receipt["digest"],
                    first_turn_digest=_json.loads(
                        path.read_text(encoding="utf-8")).get("digest"),
                )
            except Exception:
                pass
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            _json.dumps(receipt, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")
        state.hook_state[TYPED_FIRST_TURN_NODE_INPUTS_RECEIPT] = receipt
        try:
            state.append_transcript(
                TYPED_FIRST_TURN_NODE_INPUTS_RECEIPT,
                keys=sorted(values),
                value_sha256=receipt["value_sha256"],
                digest=receipt["digest"],
            )
        except Exception:
            pass
    except Exception as exc:      # noqa: BLE001
        # 收据写不成不能打断这一轮；但**必须留声**，否则"没写"和"写了"在
        # 事后长得一模一样，而消费方读不到时会以为自己拿错了路径。
        try:
            state.append_transcript(
                "first_turn_node_inputs_receipt_failed",
                error=f"{type(exc).__name__}: {exc}"[:300])
        except Exception:
            pass


def read_first_turn_node_inputs_receipt(state) -> dict:
    """读回首轮输入收据。**读不到就抛** —— 不返回空。

    空值会被消费方当成"这个 run 首轮没有输入"，而那是另一件事。
    """
    import json as _json

    path = state.root / "first_turn_node_inputs.json"
    if not path.exists():
        raise FileNotFoundError(
            f"run {getattr(state, 'run_id', '?')} 没有 "
            f"{TYPED_FIRST_TURN_NODE_INPUTS_RECEIPT} —— 它在 build_messages 里写，"
            "这个 run 要么还没走到首轮，要么收据写失败了"
            "（查 transcript 的 first_turn_node_inputs_receipt_failed）")
    receipt = _json.loads(path.read_text(encoding="utf-8"))
    if receipt.get("contract") != TYPED_FIRST_TURN_NODE_INPUTS_RECEIPT:
        raise ValueError(f"收据契约名不对：{receipt.get('contract')!r}")
    return receipt


def _workspace_and_provenance_section(state: State | None) -> str | None:
    """Inject the concrete Project v2 collaboration boundary.

    Provenance is now structural: every Session has an isolated Git worktree
    and every node owns one stable directory.  We no longer guess provenance
    from mtimes or copy files through an artifact bus.
    """
    import os as _os
    from pathlib import Path as _Path

    project_worktree = (
        _Path(state.project_worktree)
        if state is not None and getattr(state, "project_worktree", None)
        else None
    )

    lines = ["## 📁 Project v2 工作区（机器生成）"]
    if project_worktree is not None:
        lines.append(f"本 Session 的完整 Project Git worktree：`{project_worktree}`")
        # 坐标和权限是两件事，必须分两句说。合成一句"你的唯一可写目录 X，
        # 相对路径从这里解析"，读者会以为"能读的地方"也从 X 解析 —— 而下面
        # 列的上游目录是**工作区根**相对的。v22 实测：89 次 list_files
        # "找不到目录"，形状全是 `<自己节点名>/<上游节点名>/…`，即照抄了下面
        # 这份清单再被上面那句锚到自己目录。所以上游一律给**绝对路径**。
        lines.append(
            f"相对路径的锚点：`{state.workspace_root}`（你自己的目录）。"
            "跨节点取料请直接用下面的绝对路径，别写相对路径。"
        )
        lines.append(f"可写范围：只有 `{state.workspace_root}` 及其子目录。")
        # 依赖去处必须在这里说，不能等模型撞沙箱。2026-08-22 实测：没有这句
        # 时，`pip install --user` 被沙箱拒绝（$HOME 不可写），模型的下一步
        # 是 `pip install --target <自己目录>` —— 5792 个依赖文件涌进 Git
        # 记录。绕行的第一因是框架报错没给合法去处（契约必须送到调用方）。
        # `.venv` 是现成的合法去处：平台建项目时写的 .gitignore 就带
        # `**/.venv/`，装在那里既可写、又不进科研记录。
        lines.append(
            f"安装 python 依赖：在自己目录里建虚拟环境用 —— "
            f"`python3 -m venv {state.workspace_root}/.venv` 然后用 "
            f"`{state.workspace_root}/.venv/bin/pip install <包>`（或直接用该 venv 的 "
            "python 跑脚本）。`.venv` 已被 .gitignore 覆盖，不会进科研记录。"
            "不要 `pip install --user`（HOME 不可写，会被沙箱拒绝）；"
            "不要 `--target` 装进产物目录（整棵依赖树会被当作科研产物入库）。"
        )
        # 名单从 project_bootstrap 现取 —— 那里是"项目里有哪些节点目录"的
        # 真相源（它负责建这些目录）。以前这里自己抄了一份，两份各自演化：
        # 加一个节点要改两处，漏改哪处，模型就看不见那个目录的存在。
        from core.project_bootstrap import _NODE_DIRS

        upstream = ", ".join(f"`{project_worktree / name}`" for name in _NODE_DIRS)
        lines.append(
            f"上游目录可直接读取（写不进去）：{upstream}；"
            "先看各目录 README，再按需读文件。"
        )
        lines.append(
            "目录内部结构由对应节点负责，不由框架规定。不要复制上游文件；直接读取"
            "其版本化路径，并在自己的 README/报告中引用路径和证据。"
        )
        lines.append(
            "Git 由 Platform 独占。不要运行 git add/commit/reset/checkout；本 run "
            "结束时 Platform 会统一 checkpoint，失败或阻塞的有效工作也会保留。"
        )
    else:
        run_root = getattr(state, "root", None) if state is not None else None
        lines.append(f"本 run 工作区（无 project，run 级）：`{run_root}/workspace`")

    shared = [s for s in (_os.getenv("HARNESS_SHARED_TOOL_ROOTS") or "").split(":") if s]
    if shared:
        lines.append(
            "**跨项目共享目录**（平台级软件安装点，所有项目共用）："
            + "、".join(f"`{s}`" for s in shared)
        )
        lines.append(
            "共享工具默认会把输出写进自己的安装目录（如 `<tool>/logs/`）——那是"
            "**所有项目共用的落盘池**，里面的东西很可能是别的项目、更早的 run 留下的。"
            "跑共享工具时必须用它的输出目录参数（`--log-dir` / `--output-dir` 等）"
            "指向上面的本项目工作区。"
        )

    lines.append(
        "用户交来的文件落在 `sources/`（平台上传即到位，出处随 `.ref` "
        "指针进 Git）；项目内来源由 Git commit + 路径给出，不用文件时间戳猜测。"
    )
    return "\n".join(lines)


def _platform_capability_section() -> str | None:
    """机器生成的"平台有什么"段。实验设计/预注册只能承诺这里存在的资源。"""
    import os as _os

    lines = ["## 🧰 平台资源登记（机器生成 —— 实验设计与预注册只能承诺此处存在的资源）"]
    # 模型角色：**可用与不可用都列**，缺的那些把出路一起带上。
    #
    # 只列可用的，等于让节点在开工之后才发现缺口 —— 2026-08-22 实测代价：
    # postprocess 渲染完三张 publication 图，才在 finalize 处知道审图角色
    # 不可用，白烧一轮 51 个动作再重跑一遍。能在开局机械回答的事，别让模型
    # 用一次失败去问。
    try:
        from core.model_roles import render_role_section

        lines.extend(render_role_section())
    except Exception as e:  # 目录/通道炸了不摧毁 system prompt，但要吵
        lines.append(f"⚠️ 模型角色登记异常（{e.__class__.__name__}），本次未注入")
    # LLM_PROVIDERS_JSON 里额外注册的后端（给 consult_other_model 用）。角色
    # 已经在上面列过，这里按目录**扫掉**它们 —— 写死排除 "reasoning" 会让
    # 以后每个新角色都在这里重复一遍。
    try:
        from core.llm_providers import list_providers
        from core.model_roles import catalog

        role_ids = {item.id for item in catalog()}
        extra = [p for p in list_providers() if p.name not in role_ids]
    except Exception:
        extra = []
    if extra:
        lines.append("其它已注册 LLM 后端（LLM_PROVIDERS_JSON）：")
        lines.extend(f"- {p.name}（model={p.model}）" for p in extra)
    ann = _os.getenv("HARNESS_HUMAN_ANNOTATION_CHANNEL", "").strip()
    annotation_status = ann or (
        "未配置 —— 不得把人工标注写成必要步骤（可显式声明 deferred 到后续 Phase）"
    )
    lines.append(f"人工标注通道：{annotation_status}")
    # 算力/硬件：grants（钥匙）+ 现场探针（现状）。见 core/capabilities.py。
    try:
        from core.capabilities import render_compute_section

        lines.extend(render_compute_section())
    except Exception as e:  # 探针崩了不摧毁 system prompt，但要吵
        lines.append(f"⚠️ 算力登记探测异常（{e.__class__.__name__}），本次未注入")
    # 出 PDF：最近一次**实测**的结果（shared/lib/pdf_toolchain），不按编译器文件在不在猜。
    # 2026-09-23 那台 Windows 上 writing 写了 45 分钟才在最后一步发现出不了 PDF —— 能在
    # 开局机械回答的事，别让模型用一次失败去问。这里只读记录，不在拼提示时编译。
    try:
        from shared.lib.pdf_toolchain import describe_for_prompt

        lines.append(describe_for_prompt())
    except Exception as e:  # 读不出来不摧毁 system prompt，但要吵
        lines.append(f"⚠️ PDF 排版能力读取异常（{e.__class__.__name__}），本次未注入")
    lines.append(
        "上表之外的模型/API（GPT-4o、Claude、Gemini 等）一律视为不可用；"
        "写进 pre_registration 而不在 metadata.execution_commitment 里声明 "
        "substitutes/deferred 的，freeze 会被框架机械拒绝。"
    )
    return "\n".join(lines)


PREFIX_BOUNDARY = "<!-- ─── 以上整 run 稳定 · 以下每轮可变 ─── -->"


def stable_prefix_enabled() -> bool:
    """`HARNESS_STABLE_PREFIX=off` 退回旧的单一拼装顺序。"""
    import os

    return (os.environ.get("HARNESS_STABLE_PREFIX") or "on").strip().lower() != "off"


def _frozen(state: State | None, key: str, compute):
    """run 开始时算一次就冻住，整个 run 不再重算。

    MEMORY.md 和 directives 是**状态文件**：每轮重读磁盘，run 中途任何一次写入
    都会改掉 system_prompt 的中段，把它后面的缓存全部作废。冻结之后中途写入
    照常落盘（工具读到的仍是最新值），只是本 run 的 prompt 不再跟着变 —— 下个
    run 自然拿到新快照。这是拿"本 run 内的新鲜度"换"整段前缀可复用"。
    """
    if state is None:
        return compute()
    try:
        cache = state.hook_state.setdefault("_prompt_frozen", {})
    except Exception:
        return compute()
    if key not in cache:
        cache[key] = compute()
    return cache[key]


def _build_system_prompt(harness: NodeHarness, state: State | None = None) -> str:
    sections: list[str] = []
    # #737：每个真实 append 位点标记通道名，build_messages 出口收成清单。
    _manifest: list[str] = []
    if state is not None:
        state.hook_state["_ctx_manifest_system"] = _manifest
    _mark = _manifest.append
    # 每轮都会变的段落走这里，最后统一拼到末尾。
    # 缓存在**第一个变化的字节**处断掉 —— 所以决定收益的不只是"冻结了什么"，
    # 还有"变化的东西排在哪"。把易变段落留在中间，等于让它后面的一切每轮重算。
    volatile: list[str] = []
    if harness.system_prompt.strip():
        sections.append(harness.system_prompt.strip())
        _mark("system_prompt")

    if harness.rules:
        sections.append("## 硬约束（你必须遵守）")
        sections.extend(f"- {r}" for r in harness.rules)
        _mark("rules")

    if harness.guidelines:
        sections.append("## 建议（推荐但非强制）")
        sections.extend(f"- {g}" for g in harness.guidelines)
        _mark("guidelines")

    # v3.4 平台资源登记（E2E#1/#2：prereg 两次承诺平台没有的 GPT-4o/人工标注员，
    # 且节点无从知道"这里有什么"—— 资源可用性是运行时事实，必须由框架主动注入，
    # 不能指望节点自己去查）。prereg 冻结门禁（artifacts_extra）按同一登记核对。
    cap = _platform_capability_section()
    if cap:
        sections.append(cap)
        _mark("platform_capabilities")

    # v3.8：工作区绝对路径 + 溯源判据。producing 节点才需要（系统节点不跑实验）。
    if not harness.node_type.startswith("_") or (
        state is not None and getattr(state, "project_worktree", None) is not None
    ):
        ws = _workspace_and_provenance_section(state)
        if ws:
            sections.append(ws)
            _mark("workspace_provenance")

    # v3.8 承诺账：预注册冻结之后，"我承诺要测什么、测到哪一步了"必须一直在场。
    # E2E-3 的 H3 判据是合取，第二项从没测过却被写成 refuted —— 没人问过。
    if state is not None:
        try:
            from core.prereg_commitments import render_commitment_brief

            brief = render_commitment_brief(state)
            if brief:
                # 兑现进度每轮都在动 —— 属于可变段。
                volatile.append(brief)
                _mark("prereg_commitments")
        except Exception:
            pass

    # Generic blocker feedback is a basic node right.  The reporting node
    # supplies evidence; the orchestrator, not a fixed dependency graph,
    # decides whether to repair upstream work, change the environment, wait,
    # redirect, or ask a human.
    if not harness.node_type.startswith("_"):
        sections.append(
            "## 🆘 当前节点解决不了的问题\n"
            "先按正常 ReAct 方式读取报错并在你的权限内尝试解决。若缺少材料、环境、"
            "能力、权限，或必须等待外部作业，调用 `report_blocker(...)`：给出具体错误、"
            "证据路径和需要发生的变化。不要把解决方案写成固定机器/软件决策树，也不要"
            "无变化地重复同一失败。保存已完成的有效工作后结束本 run；调度器会收到"
            "结构化 blocker，并自行判断下一步。"
        )
        _mark("blocker_rights")

    # v3.7 L1 少翻译：用户原始输入逐字注入**每个**节点。
    # 三轮实测漂移的主信道是转述链（用户原文 → orchestrator 措辞 → 节点操作化）：
    # V 刻度被反转、"低难度"维度消失，都发生在"节点只见到转述版"那一步。
    # 放在 PROFILE/PROJECT 之前 —— 它的权威性高于任何被模型改写过的状态文件。
    if state is not None and state.project_root is not None:
        try:
            from core.research_intake import render_intake_section

            # 带上**这一轮是哪个会话**（#974）：这份记录是项目级的，同项目的
            # 每个会话都会读到它，不标来源就会被读成"当前用户刚说的"。
            intake_md = render_intake_section(
                state.project_root,
                current_session_id=str(getattr(state, "session_id", "") or ""),
            )
        except Exception:
            intake_md = None
        if intake_md:
            sections.append(intake_md)
            _mark("user_intake_verbatim")

    # 平台与 CLI 走**同一条**加载路径：文件是权威，读的是当下那一份。
    # 项目层的冻结由 git 给（会话的 worktree 分支），不靠抄一份进数据库。
    if state is not None:
        from core.directives_loader import load_directives_for_node

        # **不冻结 directives**：它们每轮从同一批文件读出来，改了就该下一轮
        # 生效。冻结它只会端出陈旧值（test_profile_project 抓到过一次）。
        directives = load_directives_for_node(state, harness.node_type)
        state.hook_state["_ctx_instruction_digests"] = directives.get("digests") or {}
        if directives.get("profile"):
            # 措辞按事实来：这是**现在**那份文件，不是三周前冻下来的抄件。
            sections.append("## 个人设置（当前的 PROFILE.md）")
            sections.append(directives["profile"].strip())
            _mark("profile_directives")
        if directives.get("project"):
            sections.append("## 项目设置（本会话 worktree 里的 PROJECT.md）")
            sections.append(directives["project"].strip())
            _mark("project_directives")

        # v2.0：first-class TaskList 注入（owner-filtered + byte-capped）
        # 任务状态每轮都在动 —— 可变段。
        if state.project_root is not None:
            task_block = _render_task_injection(state, harness.node_type)
            if task_block:
                volatile.append("## 📋 当前任务清单（owner-filtered；详情用 task(action='list')）")
                volatile.append(task_block)
                _mark("task_list")

        # ── 记忆送达（见 core.memory_delivery）────────────────────────────
        #
        # 这里曾经是 `if worktree: … elif project_root: …` 两条分支，而所有
        # 断点都长同一个形状：新分支加在 if 上、旧逻辑烂在 elif 里没人再进、
        # **测试全跑在 elif 那侧**（不绑 worktree 的 State），于是静默失效
        # 且全绿。2026-08-21 审计实测：9 个节点里 8 个裸调度收不到任何记忆。
        #
        # 现在只有一条路径。没绑 worktree = 没有项目记忆（匿名 run 本来就
        # 不该有），不再退回任何"兼容位置"—— 那正是上一代分叉出两份互不
        # 相干记忆的起点。
        from core.memory_delivery import constitution_block, situation_block

        # 宪法：用户所立，权威最高，排在被模型改写过的任何状态之前。
        constitution = _frozen(
            state, "memory_constitution", lambda: constitution_block(state) or "")
        if constitution:
            sections.append(constitution)
            _mark("memory_constitution")

        # 局面：**每轮现算**，所以进 volatile 段（写下来的现状必然漂移）。
        situation = situation_block(state, harness.node_type)
        if situation:
            volatile.append(situation)
            _mark("research_situation")

    if harness.skills:
        # 传 node_type 让 render_skills 过滤掉**别的节点的 node-local skill**
        rendered = render_skills(harness.skills, node_type=harness.node_type)
        if rendered:
            from core.skill_registry import render_mode

            if render_mode() == "index":
                # 索引模式：正文不在场，必须把"怎么拿到正文"送到调用方面前。
                # 只列名字不给取法 = 把"上下文太贵"换成"模型不知道有这东西"。
                sections.append(
                    "## 可用 skill（索引）\n"
                    "下面只有每个 skill 的用途和适用场景。**判断某个 skill 与当前这一步相关时，"
                    "先 `load_skill(name='<名字>')` 读完正文再动手** —— 正文里有步骤、模板和"
                    "反面案例，只看这段索引不足以正确执行。\n"
                    "（skill 装在框架目录里，不在本项目工作区内，用 read_file 取不到正文；"
                    "load_skill 是唯一入口，assets 也走它的 asset 参数。）"
                )
            else:
                sections.append("## 可用 skill")
            sections.append(rendered)
            _mark("skills")

    if harness.expected_outputs:
        sections.append("## 你必须产出这些 artifact")
        for k, desc in harness.expected_outputs.items():
            sections.append(f"- `{k}`：{desc}")
        _mark("expected_outputs")

    # ── Phase E（v0.3.2+）：framework 自动 inject KB 启发式 rule ──
    # 给所有 producing 节点 + curator 注入一段"启发式用 KB"原则。
    # 这是用户的明确设计选择：不要 challenge_kb_claim 硬工具，而是用 rule + curator
    # 反向扫 artifact 找 disagreement。orchestrator 不注入（它本来不直接用 KB claim）。
    if _should_inject_kb_heuristic(harness.node_type):
        sections.append(_KB_HEURISTIC_RULE)
        _mark("kb_heuristic")

    if not volatile:
        return "\n\n".join(sections)
    if not stable_prefix_enabled():
        # kill switch：退回"按出现顺序拼"，可变段仍在原来的位置附近。
        return "\n\n".join(sections + volatile)
    return "\n\n".join(sections + [PREFIX_BOUNDARY] + volatile)


# Phase E: 启发式用 KB rule —— 所有 producing 节点 + curator 自动注入
_KB_HEURISTIC_RULE = """\
## 💡 KB 使用启发式（framework 自动注入）

KB 注入的 claim / concept 是**历史记录参考**，不是 ground truth。**使用原则**：

1. **scope 比 conclusion 重要** —— 看每条 claim 的 `scope_dimensions`（dataset /
   regime / metric / sample_size 等）。你当前任务跟它**同 scope** 吗？
   - 同 scope → 可作 evidence 用
   - 不同 scope → 你的当前判断**优先**，不必受 KB 约束（dead_end 也一样：仅在其
     scope 成立，scope 外不必避开）
2. **不要静默 ignore 也不要默认 trust** —— 看到 KB 里 claim 你认为错 / 过时 / 不
   适用当前任务，在你产出的 artifact 中**显式写**：
   - `I disagree with claim_<id> because <具体理由 + 你的 scope>`
   - 或 `claim_<id> may not apply to <你的 scope> because <理由>`
   - curator 会扫到 → propose status review。少数派意见**有通道**。
3. **看标签**：`⚠️ disputed` = 历史多次 flip，active controversy，不是定论；
   `superseded_by` = 跳到指的新版；`replications=N` = 跨项目独立复现 N 次（高
   replication + 多 indep_sources = 稳）。
4. **写 claim 前先 search_kb 找已有** —— 即使你确信是新发现也先查（你可能错过近似）。
   找到的 concept_id 必须复用 —— create_claim 的 concept_ids 不能填新的。
"""


def _should_inject_kb_heuristic(node_type: str) -> bool:
    """决定哪些节点该自动 inject KB 启发式 rule。

    Producing 节点 + curator + reviewer 注入（reviewer 把 KB 当参考审稿）。
    Orchestrator 不注（它不直接用 KB claim 做研究）。
    """
    if node_type.startswith("_"):
        return node_type in {"_curator", "_reviewer"}
    return True  # 所有 producing 节点都注


def _owner_review_spec_section(
    harness: NodeHarness, node_inputs: dict, state: State | None = None
) -> str | None:
    """把被审节点 owner 的 review_spec 组进 reviewer 的 context。

    与上面的平台资源登记同一条原则：**判据是运行时事实，必须由框架主动注入，
    不能指望节点自己去查**。

    E2E v13 实测事故：spec 住在 harness 仓（`nodes/<source>/review_spec.md`），
    而 Project v2 下 reviewer 的 `read_file` 锚在 project worktree，于是它连试
    `plan/review_spec.md` → `../hypothesis/review_spec.md` → 绝对路径 →
    `plan/README.md` **四次全失败**，最后凭通用 rubric 审完，critique 里
    如实记着 `owner_spec_loaded: false`。"owner 定义自己节点审查标准"这套自治
    机制在 v2 下整个失效了，而且失效是静默的。

    修在 context 组装层而不是 loop hook：hook 是"prompt 建好之后再贴一段"，
    判据本就该是这个节点 context 的构成部分，不是补丁。
    """
    if harness.node_type not in ("_reviewer", "reviewer"):
        return None
    source = str((node_inputs or {}).get("source_node_type") or "").strip()
    if not source or source.startswith("_"):
        return None      # project_synthesis scope 用架构 spec，另一条路径

    from pathlib import Path

    from core.loader import node_dir

    try:
        spec_path = Path(node_dir(source)) / "review_spec.md"
    except Exception:
        return None
    if state is not None:
        # 交付事实盖章：spec 是否送达是**框架知道的事实**，critique 里的
        # rubric_source 由 compose_review_critique 从这里机械读取 ——
        # 不让模型手写（实测：模型手写把 JSON 写坏，字段成了废纸）。
        state.hook_state["_owner_review_spec_delivery"] = {
            "owner_spec_loaded": spec_path.is_file(),
            "owner_spec_path": str(spec_path) if spec_path.is_file() else None,
            "source_node_type": source,
        }
    if not spec_path.is_file():
        return (
            f"## 📋 `{source}` 的 owner review_spec\n"
            f"owner 没有写 `review_spec.md` —— 按通用 rubric 审，并在 critique 的 "
            f"`rubric_source.owner_spec_loaded` 如实记 false。"
        )
    try:
        body = spec_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    truncated = len(body) > _REVIEW_SPEC_MAX_CHARS
    return "\n".join([
        f"## 📋 `{source}` 的 owner review_spec（框架组装，无需你去找）",
        f"来源：`{spec_path}`" + ("（已截断）" if truncated else ""),
        "",
        body[:_REVIEW_SPEC_MAX_CHARS],
        "",
        "按这份判据审。critique 的 `rubric_source.owner_spec_loaded` 记 true、"
        "`owner_spec_path` 记上面的来源路径。",
    ])


_REVIEW_SPEC_MAX_CHARS = 24_000


def _build_user_prompt(harness: NodeHarness, state: State, node_inputs: dict) -> str:
    sections: list[str] = []
    _manifest: list[str] = []
    if state is not None:
        state.hook_state["_ctx_manifest_user"] = _manifest
    _mark = _manifest.append

    spec_section = _owner_review_spec_section(harness, node_inputs, state)
    if spec_section:
        sections.append(spec_section)
        _mark("owner_review_spec")

    if node_inputs:
        sections.append("## 节点输入")
        for k, v in node_inputs.items():
            sections.append(f"- **{k}**：{v}")
        _mark("node_inputs")

    artifacts = state.list_artifacts()
    if artifacts:
        sections.append("## 可用的上游 artifact（用 `read_artifact` 查看）")
        for a in artifacts[:20]:
            sections.append(f"- `{a['id']}`（type={a['type']}）：{a['name']}")
        _mark("upstream_artifacts")

    # 段 3.5：相关 KB 内容（**总是注入** —— 除非显式 kb_query='_disable'）
    # KB 不是 opt-in：项目级知识是所有节点的共享前提。
    if harness.kb_query != "_disable":
        effective_query = (
            harness.kb_query.strip()
            if harness.kb_query
            else _derive_kb_query(
                harness,
                node_inputs,
            )
        )
        kb_hits = _search_kb_for_context(state, effective_query)
        kb_totals = _kb_totals(state)
        any_kb_data = any(kb_totals.values())
        any_hit = any(kb_hits.values())

        if any_kb_data:
            _mark("kb_context")
            # ── Phase D（v0.3.2+）启发式注入头：让 LLM 知道 KB 是参考不是 ground truth ──
            sections.append("## 项目 KB 状态（💡 历史记录参考，**不是 ground truth**）")
            # v3：4 个 entity；claim_type 内含 hypothesis/synthesis/question/etc 子类
            claim_type_breakdown = kb_totals.get("by_claim_type", {})
            ct_str = (
                "; ".join(f"{k}:{v}" for k, v in claim_type_breakdown.items())
                if claim_type_breakdown
                else "(no breakdown)"
            )
            sections.append(
                f"KB 内已有（已过滤 invalid status）："
                f"{kb_totals.get('concepts', 0)} concept / "
                f"{kb_totals.get('claims', 0)} claim ({ct_str}) / "
                f"{kb_totals.get('experiments', 0)} experiment / "
                f"{kb_totals.get('chunks', 0)} chunk。"
            )
            # 启发式使用原则（用户决策：不要 challenge_kb_claim 硬工具，启发式引导）
            sections.append(
                "**使用原则**（用 KB 但不被 KB 锁死）：\n"
                "- 写新 claim/concept 前先 `search_kb` 找已有 —— 找到的 id 必须复用\n"
                "- 看每条 claim 的 **scope_dimensions** —— 你当前任务跟它同 scope 吗？\n"
                "  - scope 不同 → 你的判断**优先**于 KB，不必受约束"
                "（dead_end 也一样：仅在其 scope 内成立）\n"
                "  - scope 同但你认为 claim 过时 / 错 → 在你的 artifact 里**显式写**：\n"
                "    `I disagree with claim_<id> because <reason>` —— "
                "curator 会扫到并 propose review\n"
                "  - **不要**默认 trust KB；**也不要**静默 ignore\n"
                "- 看到 ⚠️ disputed 标的 claim → 这是 active controversy，不是定论\n"
                "- 看到 superseded 标的 claim → 跳到 `superseded_by` 指的新版"
            )
            if any_hit:
                sections.append(f"### 跟你任务相关的 KB 条目（query={effective_query!r}）：")
                for entity, records in kb_hits.items():
                    if not records:
                        continue
                    sections.append(f"**{entity}**（命中 {len(records)} 条）：")
                    for r in records:
                        sections.append(_format_kb_entry_rich(entity, r))
            else:
                sections.append(
                    f"（用 query={effective_query!r} 没命中，但 KB 里有上面提到的"
                    f"那些条目。你可以用别的 query 再查。）"
                )

    if harness.required_outputs:
        sections.append(
            f"## 提醒：必须产出的 artifact 类型 = {harness.required_outputs}。"
            f"在结束前用这些 `artifact_type` 调用 `save_artifact`。"
        )
        _mark("required_outputs_reminder")

    if not sections:
        sections.append("（没有上游上下文，按 system prompt 直接开始。）")

    return "\n\n".join(sections)


# ── KB context helpers ──────────────────────────────────────────────────────


def _derive_kb_query(harness: NodeHarness, node_inputs: dict) -> str:
    """harness.kb_query 为空时，从 system_prompt 第一句 + node_inputs 值派生。

    目的：让 KB 注入永远有 query —— agent 不能靠"我没填 kb_query"绕开 KB。
    """
    # 1. node_inputs 的 string 值拼一段
    inp_text = " ".join(str(v) for v in (node_inputs or {}).values() if isinstance(v, str) and v)[
        :300
    ]
    if inp_text:
        return inp_text
    # 2. system_prompt 第一段（非空行）
    for line in (harness.system_prompt or "").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            return s[:300]
    # 3. fallback：node_type 本身
    return harness.node_type


# 强制注入的 entity 类型 + per-entity cap。
# v3 schema：4 entity (concepts/claims/experiments/chunks)。
# experiments + chunks 不强制注入（量大 / 相关性弱 / 按需 search_kb 即可）。
# 旧 syntheses/hypotheses/questions/opportunities/decisions/failures 全合 claim_type
# 不再独立 entity。
_FORCED_INJECT_ENTITIES: dict[str, int] = {
    "concepts": 5,
    "claims": 8,  # 多一点，因为它现在涵盖所有 claim_type
}

# 每个 entity 默认过滤掉的 invalid status
_INVALID_STATUSES_INJECT: dict[str, set[str]] = {
    "claims": {"refuted", "superseded", "abandoned"},
    "concepts": {"deprecated", "merged_into"},
}

# claim_type 分层 —— 注入 ranking 用。高分 = 即使 cosine 一般也该浮上来。
#
# 键必须是**现行 5 类**（2026-08-21 收敛 10→5 时这里曾漏改：theoretical/causal/
# assumption 归一成 empirical 后从 90/80/75 静默掉到 55 —— 表没报错，只是那批
# claim 从此沉底。查找表的键挂在一套会演化的词表上，词表改了表就得跟着改）。
#
# 原表还承担"跨项目复用的类型优先"—— 那个问题现在由架构本身回答：
# 跨项目复用 = 晋升进 org，由开题注入（org_orientation）机械送达，不走这里的
# cosine 排序。这张表只管**项目内**的浮沉：
#   dead_end   最高 —— 本项目已知不通的路，撞上的代价最大
#   methodological / synthesis —— 决策与高阶结论，改变后续怎么做
#   hypothesis —— 承诺在案，相关时要看得见
#   empirical  —— 具体观察按 cosine 选，量大，不该挤掉上面几类
_CLAIM_TYPE_PRIORITY: dict[str, int] = {
    "dead_end": 92,
    "methodological": 88,
    "synthesis": 85,
    "hypothesis": 70,
    "empirical": 55,
}


def _search_kb_for_context(state: State, query: str) -> dict[str, list[dict]]:
    """跨语义 entity 检索，per-entity cap 见 _FORCED_INJECT_ENTITIES。

    Phase D (v0.3.2+)：优先用 semantic 检索（embedding cosine）；不可用时退回
    substring 兜底。invalid status 自动过滤。返按 claim_type 分层 + cosine 排序。

    env HARNESS_DISABLE_SEMANTIC_DEDUP=1 → 跳过 semantic，纯 substring。
    """
    import os as _os

    use_semantic = _os.getenv("HARNESS_DISABLE_SEMANTIC_DEDUP") != "1"
    out: dict[str, list[dict]] = {}
    q = (query or "").lower().strip()

    SEMANTIC_ENTITIES = {"claims", "concepts"}

    semantic_hits: dict[str, list[tuple[str, float]]] = {}
    if use_semantic and q:
        try:
            from core.embeddings import get_default_embedding_client
            from core.kb_vector_index import (
                query_across_scopes,
                should_rebuild,
            )

            client = get_default_embedding_client()
            # 索引未初始化 → 这次跳过 semantic
            org_ready = not should_rebuild("org", client=client)
            proj_ready = not state.project_id or not should_rebuild(
                "project",
                state.project_id,
                client=client,
            )
            if org_ready and proj_ready:
                # query 前缀跟 embed text template 一致（passage:）；e5 model 要求
                qvec = client.embed([q])[0]
                for ent in SEMANTIC_ENTITIES:
                    cap = _FORCED_INJECT_ENTITIES.get(ent, 5)
                    hits = query_across_scopes(
                        ent,
                        qvec,
                        top_k=cap * 2,
                        project_id=state.project_id,
                        min_cosine=0.30,  # 宽召回 → 后续靠 claim_type 排序
                    )
                    semantic_hits[ent] = [(h[0], h[1]) for h in hits]
        except Exception:
            semantic_hits = {}

    for entity, cap in _FORCED_INJECT_ENTITIES.items():
        records = state.list_kb(entity)
        invalid = _INVALID_STATUSES_INJECT.get(entity, set())
        if invalid:
            records = [r for r in records if r.get("status") not in invalid]
        if not records:
            out[entity] = []
            continue

        # semantic 命中（如果有）+ substring fallback
        sem_ids_to_cos: dict[str, float] = dict(semantic_hits.get(entity, []))
        if sem_ids_to_cos:
            # Only actual vector-search hits are eligible.  The previous code
            # sorted *every* record and then sliced ``[:cap]``; when fewer than
            # cap records cleared the cosine threshold, unrelated org claims
            # with cosine=0 were still injected (and high-priority dead_end /
            # methodological types received an extra rank boost).  That turns
            # cross-project scientific capital into topic contamination.
            records = [r for r in records if r.get("id") in sem_ids_to_cos]

            # 按 cosine 排，但同 cosine 时按 claim_type 优先级（principle 类排前）
            def rank_key(r):
                cid = r.get("id", "")
                cos = sem_ids_to_cos.get(cid, 0.0)
                ct_boost = _CLAIM_TYPE_PRIORITY.get(
                    r.get("claim_type", ""),
                    50,
                )
                # 主：cosine；副：claim_type priority
                return -(cos + (ct_boost - 50) / 200.0)

            ranked = sorted(records, key=rank_key)
            picked = ranked[:cap]
            out[entity] = picked
        else:
            # substring fallback
            if q:
                records = [r for r in records if q in str(r).lower()]

            # 仍按 claim_type 分层
            def fallback_key(r):
                ct_boost = _CLAIM_TYPE_PRIORITY.get(
                    r.get("claim_type", ""),
                    50,
                )
                return -ct_boost

            records = sorted(records, key=fallback_key)
            out[entity] = records[:cap]
    return out


def _kb_totals(state: State) -> dict[str, int | dict]:
    """v3 4 entity 的总数 + claims 按 claim_type 分布。"""
    totals: dict[str, int | dict] = {}
    for entity in ("concepts", "claims", "experiments", "chunks"):
        recs = state.list_kb(entity)
        invalid = _INVALID_STATUSES_INJECT.get(entity, set())
        if invalid:
            recs = [r for r in recs if r.get("status") not in invalid]
        totals[entity] = len(recs)
        if entity == "claims":
            from collections import Counter

            ct_counts = Counter(r.get("claim_type") or "?" for r in recs)
            totals["by_claim_type"] = dict(ct_counts.most_common())
    return totals


def _format_kb_entry_rich(entity: str, r: dict) -> str:
    """Phase D 富注入：显示 scope_dimensions + reason + provenance + caveat。

    v3：只有 concepts / claims 两个 entity 走这里（experiments/chunks 不强制注入）。
    """
    rid = r.get("id", "?")
    if entity == "concepts":
        aliases = r.get("aliases") or []
        alias_str = f" [aliases: {', '.join(aliases[:3])}]" if aliases else ""
        return (
            f"- `{rid}` **{r.get('canonical_name', '?')}** "
            f"({r.get('concept_type', '?')}){alias_str}: "
            f"{(r.get('description') or '')[:120]}"
        )

    if entity == "claims":
        ct = r.get("claim_type") or "?"
        status = r.get("status", "provisional")
        indep = r.get("independent_source_count") or len(r.get("sources") or [])
        repl = r.get("replication_count", 0)
        conf = r.get("confidence", 0.5)
        labels = [
            f"status={status}",
            f"indep_sources={indep}",
        ]
        if repl > 0:
            labels.append(f"replications={repl}")
        labels.append(f"conf={conf}")
        # 争议 / 过期标记
        rh = r.get("review_history") or []
        flips = sum(1 for e in rh if e.get("from_status") != e.get("to_status"))
        if flips >= 3:
            labels.append("⚠️ disputed")
        if r.get("superseded_by"):
            labels.append(f"⬆ superseded_by={r['superseded_by']}")

        lines = [
            f"- `{rid}` [{ct}, {', '.join(labels)}]",
            f"  text: {(r.get('claim_text') or '')[:160]}",
        ]
        scope_dims = r.get("scope_dimensions") or {}
        if scope_dims:
            scope_str = " ".join(f"{k}={v}" for k, v in scope_dims.items() if v)
            if scope_str:
                lines.append(f"  scope: {scope_str}")

        # claim_type 特殊提示
        if ct == "dead_end" and r.get("dont_repeat_reason"):
            lines.append(f"  why_dont_repeat: {r['dont_repeat_reason'][:200]}")
            lines.append("  💡 仅在上述 scope 成立；scope 不同需重新评估，不要默认避开")
        elif ct == "hypothesis":
            fc_struct = r.get("falsification_criteria_structured") or {}
            if fc_struct:
                m = fc_struct.get("metric", "?")
                op = fc_struct.get("comparison") or fc_struct.get("op", "?")
                th = fc_struct.get("threshold", "?")
                lines.append(f"  falsification: {m} {op} {th}")
            pred = r.get("predicted_outcome")
            if pred:
                lines.append(f"  predicted: {pred[:150]}")
        return "\n".join(lines)

    # experiments / chunks 兜底（少量场景才进 inject）
    return f"- `{rid}` {str(r)[:160]}"


# ─── v2.0 injection helpers (#379)：cap + owner filter ─────────────────────

_TASK_INJECTION_BYTE_CAP = 5 * 1024  # ~5 KB
_TASK_PER_OWNER_PENDING_LIMIT = 10
_TASK_GLOBAL_PENDING_PREVIEW = 5


def _truncate_bytes(text: str, cap: int, suffix: str = "\n\n…（注入截断）") -> str:
    b = text.encode("utf-8")
    if len(b) <= cap:
        return text
    cut = b[:cap].decode("utf-8", errors="ignore")
    return cut + suffix


#: 历史待办最多列几条。比自己的那节短 —— 它是背景，不是任务清单。
_TASK_INHERITED_PENDING_LIMIT = 3


def _task_age(task) -> str:
    """待办多老了。年龄要看得见：挂了两天的和刚写下的，读起来不该一样。"""
    from datetime import datetime, timezone

    raw = str(getattr(task, "created_at", "") or "")
    if not raw:
        return "创建时间不详"
    try:
        born = datetime.fromisoformat(raw)
    except ValueError:
        return "创建时间不详"
    if born.tzinfo is None:
        born = born.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - born).days
    if days <= 0:
        return "今天写下的"
    return f"{days} 天前写下的"


def _render_task_injection(state, node_type: str) -> str:
    """Owner-filtered task list 注入。

    - in_progress：当前节点的 + 其它节点的 id-only 计数
    - pending：当前节点 owns 的前 N + 全局 pending 预览 M 个
    - blocked：全部（需要关注）
    """
    from core.tasks import TaskList

    tl = TaskList(state.project_root / "tasks")
    all_tasks = tl.list_all()
    if not all_tasks:
        return ""

    by_status: dict[str, list] = {"in_progress": [], "pending": [], "blocked": []}
    for t in all_tasks:
        if t.status in by_status:
            by_status[t.status].append(t)

    out: list[str] = []

    # in_progress
    my_inprog = [t for t in by_status["in_progress"] if t.owner_node == node_type]
    other_inprog = [t for t in by_status["in_progress"] if t.owner_node != node_type]
    if my_inprog:
        out.append("### 🔄 你正在做（in_progress / hard limit 1 per owner_node）")
        for t in my_inprog:
            out.append(f"- `{t.id}` **{t.title}** — {(t.description or '').strip()[:200]}")
        out.append("⚠️ 完成 / block 之前**不能 start 新 task**。")
    if other_inprog:
        ids = ", ".join(f"`{t.id}`({t.owner_node})" for t in other_inprog[:8])
        more = f" +{len(other_inprog) - 8} 更多" if len(other_inprog) > 8 else ""
        out.append(f"### 🔄 其它节点 in_progress（不影响你）\n{ids}{more}")

    # blocked (全部，关键信号)
    blocked = by_status["blocked"]
    if blocked:
        out.append("### 🚫 Blocked tasks（待 unblock）")
        for t in blocked[:8]:
            reason = (t.blocked_reason or "")[:120]
            out.append(f"- `{t.id}` ({t.owner_node}) **{t.title}** — blocked: {reason}")
        if len(blocked) > 8:
            out.append(f"…（+{len(blocked) - 8} 更多）")

    # pending: 当前节点 own 的优先
    #
    # ── 本会话的 vs 项目历史的（#952）──────────────────────────────────
    #
    # 任务清单是**项目级**的，而会话是人交代一件事的边界。此前两者压在同一节
    # "你 own 的 pending" 里：一次全新会话的 experiment run，开局就看到两天前、
    # 昨天另外两个会话留下的 "Finalize external job"，于是顺手把别人的作业收尾了。
    # 节点的处理是诚实的（一个关成失败、一个关成受阻，没有伪造成功）——
    # **它不是记错了账，是被要求去做这件事**。
    #
    # 这里不改框架行为（不删、不拦），只把两件事在文本上分开，并写明"项目历史
    # 的不是你这一轮的活"。判据是出生会话，不是标题或年龄猜测；老记录没有
    # session id → 按来源不明处理，同样不冒充本会话的。
    session_id = str(getattr(state, "session_id", "") or "")
    my_pending_all = [t for t in by_status["pending"] if t.owner_node == node_type]
    other_pending = [t for t in by_status["pending"] if t.owner_node != node_type]
    if session_id:
        my_pending = [t for t in my_pending_all
                      if getattr(t, "created_in_session_id", "") == session_id]
        inherited = [t for t in my_pending_all
                     if getattr(t, "created_in_session_id", "") != session_id]
    else:
        # 没有会话身份（CLI / 教学版）时不分 —— 分不出来就别假装分得出。
        my_pending, inherited = my_pending_all, []
    if my_pending:
        out.append(f"### 📥 你 own 的 pending（{len(my_pending)}）")
        for t in my_pending[:_TASK_PER_OWNER_PENDING_LIMIT]:
            out.append(f"- `{t.id}` **{t.title}** — {(t.description or '').strip()[:140]}")
        if len(my_pending) > _TASK_PER_OWNER_PENDING_LIMIT:
            out.append(
                f"…（+{len(my_pending) - _TASK_PER_OWNER_PENDING_LIMIT} 更多；"
                "用 task(action='list', filter='pending')）"
            )
    if inherited:
        out.append(
            f"### 🗂 项目里的历史待办（{len(inherited)}，**不是这一轮的活**）")
        for t in inherited[:_TASK_INHERITED_PENDING_LIMIT]:
            out.append(
                f"- `{t.id}` **{t.title}**（{_task_age(t)}，来自本项目**另一个会话**）"
            )
        if len(inherited) > _TASK_INHERITED_PENDING_LIMIT:
            out.append(f"…（+{len(inherited) - _TASK_INHERITED_PENDING_LIMIT} 更多）")
        out.append(
            "这些是本项目**别的会话**留下的。除非用户这一轮要你处理它们，"
            "否则**不要顺手去做** —— 替别的 run 写终态事实不在你这次的授权里。"
            "真要做，先在回复里说清你打算替谁收尾、为什么。"
        )
    if other_pending:
        ids = ", ".join(
            f"`{t.id}`({t.owner_node})" for t in other_pending[:_TASK_GLOBAL_PENDING_PREVIEW]
        )
        more = (
            f" +{len(other_pending) - _TASK_GLOBAL_PENDING_PREVIEW}"
            if len(other_pending) > _TASK_GLOBAL_PENDING_PREVIEW
            else ""
        )
        out.append(f"### 📥 其它节点 pending（参考）\n{ids}{more}")

    if not out:
        return ""

    text = "\n".join(out)
    return _truncate_bytes(text, _TASK_INJECTION_BYTE_CAP)


# `_render_directives_for_node` 与 `_DIRECTIVES_INJECTION_BYTE_CAP` 已删
# （2026-08-21 记忆重建）：directives.md 这条通道整体退场 —— 它在平台模式下
# **从不被注入**却回报 success（写 memory/directives.md，而注入分支只读
# MEMORY.md）。语义归宪法（用户所立）与叙事（调度器所写）两节，
# 各有明确的写者与送达路径。见 docs/RFC_MEMORY_REBUILD_20260821.md §3.1/§3.2。
