"""``_recon_fingerprint`` 的源码路径必须加引号（S07 回归）。

``source_path`` 由 ``_infer_source_path`` 从模型命令（cd / -C / -S）与声明的源码角色
推导，是**模型可影响**的值；而 ``repro_snapshot._run`` 是 ``shell=True``。不加引号
插进 shell 字符串，路径里的元字符就是任意命令执行。

2026-09-08 实测：``source_path`` 取 ``"/x; touch /tmp/PWNED; echo"`` 时该文件被创建。
``repro_snapshot`` 自己的 15 处路径插值一律走 ``_q()``；这两行是唯一漏掉的。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from nodes.experiment.tools import safe_bash  # noqa: E402


def test_metacharacters_in_source_path_do_not_execute(tmp_path, monkeypatch):
    marker = tmp_path / "PWNED"
    seen: list[str] = []

    def _capture(cmd: str, timeout: int = 10) -> str:
        # 用真实 shell 跑，才能证明"引号确实挡住了"而不是"我们没跑"。
        seen.append(cmd)
        import subprocess
        subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
        return ""

    monkeypatch.setattr("nodes.experiment.tools.repro_snapshot._run", _capture)
    safe_bash._recon_fingerprint(f"/nonexistent; touch {marker}; echo")

    assert not marker.exists(), (
        f"shell metacharacters in source_path executed; commands were: {seen}")
    assert any("git -C " in c for c in seen), "the probe should still have run"
    assert any("'" in c or '"' in c for c in seen), (
        "the source path must reach the shell quoted")


def test_ordinary_path_still_produces_a_working_probe(tmp_path, monkeypatch):
    """加引号不得把正常路径也弄坏 —— 指纹拿不到就等于每次都重扫。"""
    seen: list[str] = []
    monkeypatch.setattr("nodes.experiment.tools.repro_snapshot._run",
                        lambda cmd, timeout=10: seen.append(cmd) or "abc123")

    safe_bash._recon_fingerprint(str(tmp_path))

    assert any(str(tmp_path) in c and "rev-parse HEAD" in c for c in seen)
