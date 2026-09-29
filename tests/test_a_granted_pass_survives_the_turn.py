"""人批准过的高危操作，**换一轮也得还算数** —— 否则它永远批不下来。

## 现场（2026-08-31，本机跑真课题，协作档）

```
04:31 assistant  ⚠️ 检测到高危操作（shell-out），是否批准执行？
04:54 user       批准执行
04:55 assistant  ⚠️ 检测到高危操作（shell-out），是否批准执行？   ← 同一个问题
04:57 user       批准执行
05:06 assistant  ⚠️ 检测到高危操作（shell-out），是否批准执行？   ← 又是它
```
三次批准换回三次一模一样的提问，`pause.abandoned` 每次都报，
而 worker 从头到尾没死过。**这个部署上高危操作永远批不下来。**

## 根因：可持久化的身份被存在了最活不过一轮的地方

`mark_confirmed` 原来只写 `state.hook_state`（**这一个 run 的内存**），
而它只在 pause 还活着时被调。pause 一作废，人的答复就被当成新的一轮，
节点用全新 State 重跑 → 通行证不存在 → 同一道**确定性**的闸再次触发 → 循环。

而 approval_contract 自己写着「通行证绑定在你这次提交的**逐字相同的参数**上」——
身份（工具 + 逐字文本 sha256）本来就可持久化。

判据落在「**新 State 认不认**」上，不落在「内存里写没写」上 ——
内存那份修复前后都在，测它等于什么都没测。
"""
from __future__ import annotations

import time

import pytest

from shared.lib import dangerous_commands as dc

_CMD = "import subprocess; subprocess.run([sys.executable, 'code/run_q2.py'])"


class _FakeState:
    """只带这条路真正读到的字段：hook_state / project_root / session_id。"""

    def __init__(self, project_root, session_id="s-1"):
        self.hook_state: dict = {}
        self.project_root = project_root
        self.session_id = session_id


def test_a_pass_granted_in_one_run_is_honoured_by_the_next(tmp_path):
    """这一条就是现场：批准在 run A，闸在 run B（全新 State）上再撞一次。"""
    granted_on = _FakeState(tmp_path)
    dc.mark_confirmed(granted_on, _CMD)
    assert dc.is_confirmed(granted_on, _CMD)

    # 人的答复被当成新一轮 → 节点用全新 State 重跑（内存里什么都没有）
    fresh = _FakeState(tmp_path)
    assert not fresh.hook_state, "前提：新 State 的内存是空的"
    assert dc.is_confirmed(fresh, _CMD), (
        "换一轮之后通行证就不认了 —— 高危操作永远批不下来（三次批准三次同一个问题）"
    )


def test_the_pass_is_still_one_shot(tmp_path):
    """一次性不能因为落盘就变成长期授权。"""
    st = _FakeState(tmp_path)
    dc.mark_confirmed(st, _CMD)
    dc.consume_confirmation(st, _CMD)
    fresh = _FakeState(tmp_path)
    assert not dc.is_confirmed(fresh, _CMD), "消费掉的通行证还留在盘上"


def test_a_different_command_is_not_covered(tmp_path):
    """绑定的是**逐字**参数：改一个字就得重新问。"""
    st = _FakeState(tmp_path)
    dc.mark_confirmed(st, _CMD)
    fresh = _FakeState(tmp_path)
    assert not dc.is_confirmed(fresh, _CMD + " --force")


def test_another_session_is_not_covered(tmp_path):
    """人是在**这个会话**里批的，别的会话不继承。"""
    dc.mark_confirmed(_FakeState(tmp_path, session_id="s-1"), _CMD)
    assert not dc.is_confirmed(_FakeState(tmp_path, session_id="s-2"), _CMD)


def test_a_pass_expires(tmp_path, monkeypatch):
    """一次性凭证不该永生 —— 人批的是"现在这一次"。"""
    st = _FakeState(tmp_path)
    dc.mark_confirmed(st, _CMD)
    monkeypatch.setattr(dc, "_now", lambda: time.time() + dc.PASS_TTL_SECONDS + 1)
    assert not dc.is_confirmed(_FakeState(tmp_path), _CMD)


def test_run_local_state_without_project_root_still_works(tmp_path):
    """教学版 run-local（没有 project_root）不该炸，退回内存语义。"""
    st = _FakeState(None)
    dc.mark_confirmed(st, _CMD)
    assert dc.is_confirmed(st, _CMD)
    assert not dc.is_confirmed(_FakeState(None), _CMD)
