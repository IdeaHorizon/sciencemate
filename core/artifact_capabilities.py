"""Capability-gated artifact writes for contracts that must be tool-created.

The generic ``save_artifact`` tool is intentionally flexible, but a small set
of artifacts are themselves approval or provenance credentials.  Allowing an
agent to synthesize those records with arbitrary metadata would turn the
completion gate into self-attestation.

Trusted lifecycle tools call :func:`save_typed_artifact`.  Transfers between
runs remain legal because they **carry** an existing credential rather than
minting a new one —— 见 :func:`_is_transfer` 里对「哪种 provenance 才算转发」
的说明（豁免只给 forwarded，不给 imported）。
"""

from __future__ import annotations

from typing import Any

_WRITE_CAPABILITY = object()


def _is_transfer(provenance: dict[str, Any] | None) -> bool:
    """这次写入是在**搬运**一份既有凭据，还是在**新铸**一个？

    ⚠️ 判据原先是 `provenance is not None`，配的理由是「通用 save_artifact 不
    暴露 provenance，所以模型用不上这条豁免」。那句话对 save_artifact 成立，
    **对模型的触达面不成立** —— `import_artifact` 同样是模型可调的，而且
    **无条件**传 `imported` provenance（artifact_intake.py，`_prov.imported(...)`），
    `artifact_type` 又是模型自由填的字符串、没有 enum 也没有白名单。

    于是 `import_artifact(artifact_type="research_state", ...)` 会在这里提前
    返回，绕开下面整个 owner/capability 检查，直接铸出一份**科学裁决凭据** ——
    `update_research_state` 的全部完整性判据（版本链、假说不得消失、裁决必须挂
    证据、冻结 prereg 假说必须在册）一条都不过。已实测复现。
    前提只是 `sources/` 下有用户上传的文件，这是常态。

    所以豁免只给 `forwarded`：它只能由框架构造（`executor` 上游转发、`run_node`
    子产物回填），模型够不到。外来材料要进来就走非 typed-only 的类型 ——
    `figure` 这类仍然可写正是为此留的。

    同族病例见 2026-08-22 的冻结伪造（模型被一道闸拒了，就换个工具绕过去）。
    """

    from core.artifact_provenance import KIND_FORWARDED

    return isinstance(provenance, dict) and provenance.get("kind") == KIND_FORWARDED

# Keep this set deliberately narrow.  ``figure`` and legacy ``figure_plan``
# remain writable during the compatibility window; the final v2 credentials do
# not.  A forged legacy figure still cannot enter an approved figure_package.
TYPED_ONLY_ARTIFACT_OWNERS: dict[str, frozenset[str]] = {
    # Writing can only mint this receipt after resolving an approved
    # figure_package and re-hashing the exact bytes it copied.  Validation
    # trusts the receipt, so generic save_artifact must not be able to forge it.
    "writing_asset_receipt": frozenset({"writing"}),
    "latex_build_receipt": frozenset({"writing"}),
    "writing_validation_report": frozenset({"writing"}),
    "review_critique": frozenset({"_reviewer"}),
    # research_state 是**科学裁决凭据**（哪条假说被支持/证伪、依据是什么），
    # 不是普通文档。它的全部完整性判据（版本链、假说不得消失、裁决必须挂
    # 证据、冻结 prereg 假说必须在册）都在 update_research_state 里；若通用
    # save_artifact 也能造一份，那些判据就只是建议 —— 绕过它只要换个工具名。
    "research_state": frozenset({"hypothesis", "analysis"}),
}


#: 冻结状态是**生命周期字段**，只有 `freeze_artifact` 能写。
#:
#: 2026-08-22 实测：orchestrator 想 finalize 论文，被冻结闸机械拒了（那道闸是
#: 对的 —— 它专防「把 reviewer 的 REVISE 洗成已冻结的论文」）。模型于是绕过去：
#: 用 `save_artifact` 重新写一遍同一份产物，metadata 里直接带 `frozen: true`。
#: 盘上那份 manuscript 因此 `frozen=True` 而 `frozen_at=None`，账本上也没有 freeze 行
#: —— 真冻结会写时间戳并在账本落一行，伪造的两样都没有。
#:
#: 后果不是少一个字段：**下游只要检查 `metadata.frozen` 就会把它当成已冻结**，
#: 而它绕过的正是那道防止 REVISE 被洗白的闸。一次普通写入就能让不可逆的冻结
#: 形同虚设。
#:
#: ## 判据放在**工具面**，不在落盘边界
#:
#: 我第一版放在 `State.save_artifact`（与 typed-only 同一处），理由写的是
#: 「内部调用方和 fixture 也绕不过去」。**那是照抄位置，不是推出来的判断**，
#: 代价当场显形：全仓 157 条测试转红。
#:
#: 两者的对手不是同一个。typed-only 挡的是「凭据被随手铸出来」，内部调用方
#: 本来就该走生命周期工具，所以放深处对。冻结不一样 —— **「先造一份已冻结的
#: 产物、再测下游怎么对待它」是夹具的正常需求**，157 处都是。放深处等于把
#: 病人和病一起治死。
#:
#: 而伪造只有一个对手：**模型**。模型碰得到这件事的地方只有注册的工具面，
#: 一个都跑不掉。所以闸放在那儿，且靠 `tests/test_freezing_cannot_be_forged.py`
#: 里的**扫盘**测试保证「以后新开的写产物工具也逃不掉」—— 扫盘，不是名单。
_FREEZE_OWNED_METADATA = ("frozen", "frozen_at", "freeze_reason")


def freeze_fields_being_claimed(metadata: dict[str, Any] | None) -> list[str]:
    """metadata 里有没有在**声称**冻结。

    只认真值：`frozen: false` 不是声称冻结（sediment 那两处正是先
    `if not frozen` 才写，按"键在不在"判会把它们炸掉）。
    """

    if not isinstance(metadata, dict):
        return []
    return [k for k in _FREEZE_OWNED_METADATA if metadata.get(k)]


def reject_freeze_forgery(metadata: dict[str, Any] | None) -> None:
    """模型面工具的写入不得自称已冻结。写产物的工具都该在入口调它一次。"""

    forged = freeze_fields_being_claimed(metadata)
    if not forged:
        return
    raise PermissionError(
        f"metadata 里不能手写冻结字段 {forged} —— 冻结是 freeze_artifact 的专属"
        f"动作。手写只会得到一个 frozen=true 但没有 frozen_at、没有晋升、也没走"
        f"过 reviewer flow 检查的假冻结产物。要冻结请调 "
        f"freeze_artifact(artifact_id=...)；若它拒了你，那道拒绝就是结论，"
        f"按它给的理由去处理，不要绕开。"
    )


def validate_artifact_write(
    *,
    node_type: str,
    artifact_type: str,
    capability: object | None,
    provenance: dict[str, Any] | None,
) -> None:
    """Reject untrusted creation of a typed-only artifact.

    Explicit provenance means the record is being forwarded/imported rather
    than minted in the current run.  Generic LLM-facing ``save_artifact`` does
    not expose provenance, so it cannot use that transfer exception.

    冻结字段不在这里判 —— 那道闸在模型面工具入口，见 `reject_freeze_forgery`
    上方的说明（放这里会把 157 处合法夹具一起打死）。
    """

    owners = TYPED_ONLY_ARTIFACT_OWNERS.get(artifact_type)
    if owners is None or _is_transfer(provenance):
        return
    if capability is not _WRITE_CAPABILITY:
        raise PermissionError(
            f"artifact_type={artifact_type!r} is typed-only and must be created "
            f"by its lifecycle tool (owners={sorted(owners)})。"
            f"外来材料请用非 typed-only 的类型登记（如 figure / dataset / "
            f"survey_report），再由属主节点的生命周期工具去产出正式那份 —— "
            f"换个工具名把它铸出来不是出路。"
        )
    if node_type not in owners:
        raise PermissionError(
            f"node_type={node_type!r} is not an owner of typed artifact "
            f"{artifact_type!r}; owners={sorted(owners)}"
        )


def save_typed_artifact(
    state: Any,
    *,
    artifact_type: str,
    name: str,
    content: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Trusted write entry point used only by lifecycle tool implementations."""

    if artifact_type not in TYPED_ONLY_ARTIFACT_OWNERS:
        raise ValueError(f"artifact_type={artifact_type!r} is not registered typed-only")
    return state.save_artifact(
        artifact_type=artifact_type,
        name=name,
        content=content,
        metadata=metadata,
        _write_capability=_WRITE_CAPABILITY,
    )
