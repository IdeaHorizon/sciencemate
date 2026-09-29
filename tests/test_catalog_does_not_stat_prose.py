"""`/catalog` 不许把一段散文当路径拿去 stat（#972 回归）。

现场：Experiment 冻结 operation 三件套后，`GET /…/catalog` 返回 500。堆栈一路到
`closing_manifest._companions_of → _relative_to_workspace → Path.exists()`，
抛 `OSError: [Errno 36] File name too long`。原因是 `_path_candidates` 把
**整个字符串**无条件当候选，而那个字段是一整段中文业务 summary。
后果是「这个项目产出了什么」的唯一读模型完全不可用，单个坏字段拖垮所有产出展示。

⚠️ **判据为什么不断言「会不会抛」**：那个 errno 只在 Linux 后端上炸。本机
macOS 的 `Path.exists()` 把它吞掉返回 False —— 同一份代码在开发机上永远绿。
所以判据落在**这个字符串有没有被交出去 stat**：用探针记下每一次 stat 的对象，
断言那段散文不在里面。这条在两个平台上都成立，也就都能转红。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core import closing_manifest


#: #972 现场那个字段的形状：以中文说明开头、足够长、里面还带标点和数字。
_PROSE = (
    "负向校验作业物理层如实记录为 failed（exit 7），"
    "并在 receipt 中保留 stderr 尾部与 rc；" + "复核记录" * 60
)


def test_a_long_prose_field_is_never_handed_to_stat(tmp_path, monkeypatch):
    """那段散文不得出现在任何一次 stat 的对象里。"""
    statted: list[str] = []
    real_exists = Path.exists

    def _spy(self, *a, **kw):
        statted.append(str(self))
        return real_exists(self, *a, **kw)

    monkeypatch.setattr(Path, "exists", _spy)

    closing_manifest._companions_of(
        tmp_path / "experiment" / "record.json",
        {"summary": _PROSE},
        tmp_path,
    )

    offenders = [p for p in statted if "复核记录复核记录" in p]
    assert not offenders, (
        "一整段散文被拼成路径交给 stat 了 —— Linux 上这就是 ENAMETOOLONG，"
        f"整个 /catalog 变 500。命中：{offenders[:1]}"
    )


def test_stat_that_blows_up_does_not_take_the_catalog_with_it(tmp_path, monkeypatch):
    """就算真有一个候选让 stat 抛错，也只能当成没命中，不许往上抛。

    这是第二道：长度闸挡住了已知形状，这条挡住「下一个我们没想到的 errno」。
    """
    def _always_explodes(self, *a, **kw):
        raise OSError(36, "File name too long")

    monkeypatch.setattr(Path, "exists", _always_explodes)

    # 不抛就是通过；顺带确认它老老实实返回了空清单而不是半截结果。
    assert closing_manifest._companions_of(
        tmp_path / "experiment" / "record.json",
        {"figure": "runtime/out/convergence.png"},
        tmp_path,
    ) == []


def test_a_real_relative_path_in_prose_is_still_found(tmp_path):
    """收窄不能收过头：散文里**真的**指到一个存在的文件，仍然要捞得出来。

    没有这一条，上面两条可以靠「什么都不认」作弊通过。
    """
    node_dir = tmp_path / "experiment"
    (node_dir / "runtime" / "out").mkdir(parents=True)
    (node_dir / "runtime" / "out" / "convergence.png").write_bytes(b"\x89PNG")

    found = closing_manifest._companions_of(
        node_dir / "record.json",
        {"note": "见 runtime/out/convergence.png (log-log, 9 点 chi2 CI 误差线)"},
        tmp_path,
    )

    assert "experiment/runtime/out/convergence.png" in found
