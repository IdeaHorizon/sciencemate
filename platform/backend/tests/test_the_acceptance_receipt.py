"""验收收据：它只在跑通之后写，而且认的是**字节**。

发布那头（`scripts/package/publish_release.py::refuse_a_windows_build_nobody_ran`）
拿这张收据决定 Windows 安装器发不发。所以收据必须满足两件事，否则那道闸拦的是
一个影子：

* **只在成功之后写。** 失败时写一张，等于用一张纸说"跑通了"。
* **记安装器的 sha256，不是版本号。** 版本号对得上的包可以有很多个；发出去的
  只有一份字节。

两次事故（0.4.0 `[WinError 5]` #908、0.5.x `[WinError 6]` #1122）都是同一个形状：
改了 spawn，Windows 上一次都没真发过消息就发版。
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SMOKE = REPO / "scripts" / "acceptance" / "personal_smoke.py"
_spec = importlib.util.spec_from_file_location("afs_personal_smoke", SMOKE)
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)


def test_the_receipt_carries_the_bytes_it_verified(tmp_path) -> None:
    installer = tmp_path / "ScienceMate-Setup.exe"
    installer.write_bytes(b"pretend this is 369 MB")
    receipt = tmp_path / "windows-acceptance.json"

    smoke.write_the_receipt(receipt, "http://127.0.0.1:61971", installer, 5.2)

    body = json.loads(receipt.read_text(encoding="utf-8"))
    assert body["sha256"] == hashlib.sha256(installer.read_bytes()).hexdigest()
    assert body["installer"] == "ScienceMate-Setup.exe"
    assert body["against"] == "http://127.0.0.1:61971"
    assert body["result"] == "SMOKE OK"


def test_a_receipt_without_an_installer_still_says_what_it_verified(tmp_path) -> None:
    """没给安装器也写 —— 但没有 sha256，发布那头会拒。收据不替人做判断，只如实记。"""
    receipt = tmp_path / "r.json"

    smoke.write_the_receipt(receipt, "http://127.0.0.1:1", None, 1.0)

    body = json.loads(receipt.read_text(encoding="utf-8"))
    assert "sha256" not in body and body["against"] == "http://127.0.0.1:1"


def test_the_receipt_is_written_only_after_the_reply_arrived() -> None:
    """扫盘：`run()` 里写收据那一句，只许出现在"收到回复"之后的成功段里。

    判据不看文案看**位置**：`write_the_receipt` 的调用必须在 `return 0` 之前、
    且与它同在 `try` 的正常出口，不能在 `except` / `finally` 里
    —— 失败也写一张的收据比没有收据更糟。
    """
    tree = ast.parse(SMOKE.read_text(encoding="utf-8"))
    run = next(node for node in ast.walk(tree)
               if isinstance(node, ast.FunctionDef) and node.name == "run")
    tries = [node for node in ast.walk(run) if isinstance(node, ast.Try)]
    assert tries, "run() 的形状变了（没有 try）—— 这条扫盘得跟着改"

    def calls_in(nodes) -> int:
        return sum(1 for branch in nodes for node in ast.walk(branch)
                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id == "write_the_receipt")

    happy = sum(calls_in(t.body) for t in tries)
    sad = sum(calls_in(t.handlers) + calls_in(t.finalbody) for t in tries)
    assert happy == 1, f"成功段里写收据的地方有 {happy} 处，应恰好 1 处"
    assert sad == 0, "失败/收尾段里也在写收据 —— 那张纸会说跑通了"


def test_asking_for_an_installer_without_a_receipt_is_a_usage_error() -> None:
    """算了哈希却没人收 = 用的人以为自己留下了凭据，其实没有。"""
    with pytest.raises(SystemExit):
        smoke.main(["--for-installer", "x.exe"])


#: 真机上写出来的那一张（9800x3d 的 Windows 侧，2026-09-22，源码树那次跑）。
#: 手捏的样本只证明我对自己的理解自洽 —— 回放一张真的，字段名、时间格式、
#: `against` 为 null 的形状才都是真的。
CAPTURED = {
    "result": "SMOKE OK",
    "against": None,
    "platform": "win32",
    "host": "DESKTOP-9EL2944",
    "ran_at": "2026-09-22T02:32:36+00:00",
    "seconds": 5.4,
    "installer": "Shell_fix.exe",
    "sha256": "0da7adb4c4afe862d9882027e86e363b69d1c9e79ab6a0f787197317add8a28d",
}


def test_the_publish_gate_reads_a_real_receipt(tmp_path) -> None:
    """回放真机那张：源码树跑出来的收据（`against` 为 null）必须被拒。

    源码树里现起的后端在 Windows 上一直是通的 —— 它证明不了装出来的那个包。
    """
    spec = importlib.util.spec_from_file_location(
        "afs_publish_release_for_receipt", REPO / "scripts" / "package" / "publish_release.py")
    publish_release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(publish_release)

    release = tmp_path / "release"
    release.mkdir()
    (release / "SHA256SUMS").write_text(
        f"{CAPTURED['sha256']}  ScienceMate-Setup.exe\n", encoding="utf-8")
    (release / publish_release.WINDOWS_ACCEPTANCE).write_text(
        json.dumps(CAPTURED), encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        publish_release.refuse_a_windows_build_nobody_ran(release)
    assert "against" in str(exc.value)

    # 同一张纸，只把「验的是哪个跑着的实例」补上 —— 哈希本来就对得上，于是放行。
    (release / publish_release.WINDOWS_ACCEPTANCE).write_text(
        json.dumps({**CAPTURED, "against": "http://127.0.0.1:61971"}), encoding="utf-8")
    publish_release.refuse_a_windows_build_nobody_ran(release)
