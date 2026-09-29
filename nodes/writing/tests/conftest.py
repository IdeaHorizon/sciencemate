"""writing 节点本地测试配置。

根 tests/conftest.py 管不到这个目录，这里自带同一件事：HARNESS_FRAMEWORK_HOME /
ORG_HOME 隔离到 tmp。

缺它的代价实测过（2026-09-23）：编译失败而日志里没有 TeX 报错时，latex.py 会实测一次
PDF 工具链并把结果写进数据根（shared/lib/pdf_toolchain）。这里的编译测试一跑，开发者
本机真数据根里就多了一条「这台机器出不了 PDF」—— 本机桌面版的系统提示随即照着它说。
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_harness_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "harness-home"))
    monkeypatch.setenv("HARNESS_FRAMEWORK_ORG_HOME", str(tmp_path / "harness-home" / "org"))
    yield


@pytest.fixture(autouse=True)
def _pdf_toolchain_already_measured(monkeypatch):
    """write_brief 开工时会实测一次 PDF 工具链（真编一份样本，几十秒）。写作流程的测试
    不测这件事，给它一份「实测可用」的记录；测它的用例自己换掉（test_brief_says_whether_pdf_works）。"""
    from shared.lib import pdf_toolchain

    async def measured(state=None, *, timeout=900):
        return {"works": True, "status": "works", "reason": "",
                "identity": {"compiler": "latexmk", "biber": "/usr/bin/biber"}}

    monkeypatch.setattr(pdf_toolchain, "ensure_measured", measured)
