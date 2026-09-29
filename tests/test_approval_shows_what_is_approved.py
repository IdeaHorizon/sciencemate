"""审批面必须让人看见**被批的到底是什么** —— 看不见就等于没有审批。

这套文件自己的原则原话：「审批一个看不见内容的高危操作没有意义」。
但预览写死 300 字符，且文案叫「**完整**内容（前 300 字符）」——
自相矛盾，而且对一段 Python 根本不够。

2026-08-31 本机跑真课题实测，审批卡上人能看到的是：

    完整内容（前 300 字符）：
    import subprocess, sys, os, hashlib, json
    base = '/Users/.../experiment'
    # run the script to confirm reproducibility
    r = subprocess.run([sys.executable, os.path.join(base, 'code'

—— 恰好切在决定性的那个 token 上：**要跑哪个脚本，审批人看不到**。
连着三次审批都是这一屏，人只能凭"看起来像是它自己的复现脚本"按批准。

判据落在两件事上：(1) 真实长度的片段要能整段看到；
(2) 截断时**必须说自己截了**，不能继续叫「完整内容」。
"""
from __future__ import annotations

from shared.lib.dangerous_commands import APPROVAL_PREVIEW_CHARS, _preview_block

_REAL_SNIPPET = (
    "import subprocess, sys, os, hashlib, json\n\n"
    "base = '/Users/wddddds/afs-local-deploy/shared/project-worktrees/"
    "46da60b0-f321-4187-accf-dbc1ebf5bbf3/a3ddca24-2986-472a-8df6-02ac010aa71e/experiment'\n"
    "# run the script to confirm reproducibility\n"
    "r = subprocess.run([sys.executable, os.path.join(base, 'code', 'run_q2_matched.py'),\n"
    "                    '--seed', '20260831', '--windows', '10,20,50'],\n"
    "                   capture_output=True, text=True, cwd=base)\n"
    "print(r.returncode, r.stdout[-2000:], r.stderr[-2000:])\n"
)


def test_a_real_snippet_is_shown_whole():
    """现场那段脚本必须整段看得到 —— 尤其是它要跑哪个文件。"""
    block = _preview_block(_REAL_SNIPPET)
    assert "run_q2_matched.py" in block, (
        "审批人看不到要跑哪个脚本 —— 300 字符的老上限正好切在这里"
    )
    assert "完整内容" in block


def test_truncation_says_so_instead_of_claiming_completeness():
    """截断了就说截了多少，别继续叫「完整内容」。"""
    long_text = "x" * (APPROVAL_PREVIEW_CHARS + 777)
    block = _preview_block(long_text)
    assert "完整内容" not in block, "截断了还自称完整内容"
    assert str(APPROVAL_PREVIEW_CHARS) in block
    assert "777" in block, "没说清楚被砍掉了多少"
    assert len(block) < len(long_text), "截断没生效"


def test_the_cap_is_big_enough_for_a_real_command():
    """上限得能装下真实的命令片段，不是一个仪式性的数字。"""
    assert APPROVAL_PREVIEW_CHARS >= len(_REAL_SNIPPET)
