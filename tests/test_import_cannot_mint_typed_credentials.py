"""外来材料不能铸出 typed-only 凭据 —— 豁免只给转发，不给导入。

## 病例（2026-08-22，取证时顺带查实并实测复现）

typed-only 那道闸的转发豁免原先写作 `provenance is not None`，配的理由是：

    Generic LLM-facing ``save_artifact`` does not expose provenance, so it
    cannot use that transfer exception.

**这句话对 `save_artifact` 成立，对「模型的触达面」不成立。** `import_artifact`
同样是模型可调工具（挂在 `_orchestrator` 的工具列表里），而且**无条件**传
`imported` provenance；它的 `artifact_type` 是模型自由填的字符串，没有 enum、
没有白名单。

于是这一句就绕开了整道闸：

    import_artifact(artifact_type="research_state", name=..., source_path=...)

`research_state` 是**科学裁决凭据**（哪条假说被支持/证伪、依据是什么）。它的全部
完整性判据都在 `update_research_state` 里 —— 版本链、假说不得消失、裁决必须挂
证据、冻结 prereg 的假说必须在册。走 import 铸出来的那份，一条都没过。

前提只是 `resources/` 下有一个用户上传的文件 —— 常态。

## 和当天那个冻结伪造是同一个形

模型被一道闸机械拒了，**换个工具再来一次**。区别只在于冻结那次是事后从盘上的
产物取证抓到的，这次是取证时顺路查出来的。判据都一样：一道闸的豁免条件，必须按
**模型够不够得着**来定，不能按「我这个工具没暴露那个参数」来定。
"""
from __future__ import annotations

import pytest

from core import artifact_provenance as prov
from core.artifact_capabilities import (
    TYPED_ONLY_ARTIFACT_OWNERS,
    validate_artifact_write,
)


def _imported():
    """`import_artifact` 实际构造的那种 provenance（artifact_intake，无条件传）。"""
    return prov.imported(source_path="resources/notes.md", sha256="0" * 64,
                         size_bytes=10, by_run_id="r1")


@pytest.mark.parametrize("artifact_type", sorted(TYPED_ONLY_ARTIFACT_OWNERS))
def test_imported_material_cannot_mint_any_typed_credential(artifact_type: str) -> None:
    """扫全表，不挑一个 —— 名单里新加的类型自动被这条罩住。"""
    with pytest.raises(PermissionError) as excinfo:
        validate_artifact_write(node_type="_orchestrator", artifact_type=artifact_type,
                                capability=None, provenance=_imported())
    message = str(excinfo.value)
    assert "typed-only" in message
    assert "不是出路" in message, (
        "报错必须指出「换个工具名把它铸出来不是出路」并给出真正的下一步 —— "
        "这个 bug 的成因就是模型把一次拒绝当成了换条路再试的信号"
    )


@pytest.mark.parametrize("artifact_type", sorted(TYPED_ONLY_ARTIFACT_OWNERS))
def test_forwarding_a_typed_credential_still_works(artifact_type: str) -> None:
    """搬运照旧放行 —— 收紧不能把跨 run 转发一起打死。

    `run_node` 回填子产物、`executor` 转发上游输入走的都是这条；打死它整条
    流水线就断在这，而且全套测试里覆盖得极薄（冻结那次就是这么差点出事的）。
    """
    validate_artifact_write(
        node_type="writing", artifact_type=artifact_type, capability=None,
        provenance=prov.forwarded(prov.produced("postprocess", "r_child"),
                                  via_node_type="writing", via_run_id="r_parent"),
    )


def test_non_typed_types_are_still_freely_importable() -> None:
    """外来材料的正常落点不受影响 —— 那才是 import 该走的路。"""
    for artifact_type in ("figure", "dataset", "survey_report"):
        assert artifact_type not in TYPED_ONLY_ARTIFACT_OWNERS
        validate_artifact_write(node_type="_orchestrator", artifact_type=artifact_type,
                                capability=None, provenance=_imported())


def test_the_owner_check_still_runs_for_ordinary_writes() -> None:
    """原有的属主判据没被顺手改坏。"""
    with pytest.raises(PermissionError, match="not an owner"):
        validate_artifact_write(node_type="literature", artifact_type="research_state",
                                capability=_capability(), provenance=None)


def _capability():
    from core.artifact_capabilities import _WRITE_CAPABILITY

    return _WRITE_CAPABILITY
