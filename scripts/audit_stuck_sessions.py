#!/usr/bin/env python3
"""卡死普查探针：找出"非终态但没有活进程"的 session。

既是诊断工具，也是修复方案的验收断言（台账 #8）：
    修好之后这个脚本的 orphan 计数必须恒为 0。

判据刻意做成**外部可执行**、不依赖平台内部任何机制 —— 因为要验的恰恰是
"平台自己看不见自己卡住了"。谁来验都不该问被验的那一方。

用法：
    .venv/bin/python scripts/audit_stuck_sessions.py            # 人读
    .venv/bin/python scripts/audit_stuck_sessions.py --assert   # CI：有孤儿则退出码 1
"""
from __future__ import annotations

import json
import subprocess
import sys

#: 隐形卡死：这些状态宣称"有东西在跑/在等"，但没有活进程就是尸体。
#: UI 上它们看起来一切正常 —— 这是最贵的一类，必须为 0。
INVISIBLE = ("running", "waiting_human", "waiting_permission", "retrying", "queued")
#: 已标记：平台看见了它、也标了可恢复，只是还没人去恢复。
#: 这是**可接受**状态 —— 判据是"平台知不知道"，不是"有没有人处理"。
MARKED = ("stale_unknown",)
NON_TERMINAL = INVISIBLE + MARKED

#: 活性标记：每个活着的会话进程持有 `<...>/orchestrator__*/.chat.lock`。
#: 这是**平台自己**的标记，不是我另造的判据 —— 量具应当读被测系统已有的
#: 事实，别自己发明一套（自己发明的那套刚刚假阳性了一次）。
LOCK_MARKER = ".chat.lock"
SQL = f"""
select session_id, id, status,
       to_char(updated_at,'MM-DD HH24:MI'),
       round(extract(epoch from (now()-updated_at))/60)::int
from runs where status in {NON_TERMINAL}
order by updated_at
"""


def _psql(sql: str) -> list[list[str]]:
    out = subprocess.run(
        ["docker", "exec", "p2-postgres", "psql", "-U", "postgres",
         "-d", "research_platform", "-tAF", "|", "-c", sql],
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        raise SystemExit(f"psql 失败: {out.stderr.strip()[:300]}")
    return [line.split("|") for line in out.stdout.splitlines() if line.strip()]


def _live_sessions() -> set[str]:
    """当前真的有 harness 进程在服务的 session。

    判据用**平台自己的活性标记**：每个活着的会话进程持有
    `<worktree>/<session>/.research/cache/runtime/runs/orchestrator__*/.chat.lock`。

    先前按"进程命令行里有没有 session id"和"进程 cwd 在不在 worktree 里"认，
    两个都不成立 —— serve 模式的命令行是 `-m platform_runtime --serve`，
    session id 根本不在参数里；cwd 也不在 worktree。结果是把一个活得好好的
    session 判成隐形卡死（实测 pid 38408 明明持着锁）。
    **量具本身错了，判据就会撒谎** —— 这个探针是整套修复的验收标准，
    它的假阳性比被测系统的 bug 更贵。
    """
    # 全局列一遍打开的文件、再按锁文件名筛 —— `lsof +D <深目录>` 在这个层级
    # 上返回空（实测），按名字筛一抓就中。
    out = subprocess.run(
        ["bash", "-lc", "lsof -Fn 2>/dev/null | grep '\\.chat\\.lock' || true"],
        capture_output=True, text=True, timeout=120,
    )
    live: set[str] = set()
    for line in out.stdout.splitlines():
        for token in line.replace("/", " ").split():
            if len(token) == 36 and token.count("-") == 4:
                live.add(token)
    return live


def main() -> None:
    strict = "--assert" in sys.argv
    rows = _psql(SQL)
    live = _live_sessions()
    orphans, alive = [], []
    for sid, rid, status, seen, mins in rows:
        (alive if sid in live else orphans).append(
            {"session": sid, "run": rid, "status": status,
             "last_seen": seen, "stale_min": int(mins)})

    invisible = [o for o in orphans if o["status"] in INVISIBLE]
    marked = [o for o in orphans if o["status"] in MARKED]
    print(f"非终态 run：{len(rows)}　|　有活进程：{len(alive)}　|　"
          f"**隐形卡死：{len(invisible)}**　|　已标记待恢复：{len(marked)}")
    if orphans:
        print(f"\n{'session':14s} {'status':18s} {'最后活动':12s} {'停滞(分钟)':>10s}")
        print("-" * 60)
        for o in sorted(orphans, key=lambda x: -x["stale_min"]):
            print(f"{o['session'][:12]:14s} {o['status']:18s} "
                  f"{o['last_seen']:12s} {o['stale_min']:10d}")
        worst = max(o["stale_min"] for o in orphans)
        print(f"\n最久停滞：{worst} 分钟（{worst/60:.1f} 小时）")
    if strict:
        # ── 判据修正（2026-08-10）──────────────────────────────────────
        #
        # 原判据只卡"隐形卡死"，把已标 stale_unknown 的算作"知道且可恢复，
        # 等人处理不算故障"。于是实测出现这一幕：断言**通过**，同时列着 19 个
        # session 停在 stale_unknown，最久 73.6 小时。
        #
        # 这是判据在给自己判及格。`stale_unknown` 的语义是**中转站**——"运行时
        # 丢了，需要恢复"。而全仓 `recover_stale_session` 只有一个调用方，是个
        # API 端点：没人来调就永远停在中转站。一个 73 小时没人管的中转站，
        # 对用户而言和卡死没有任何区别。
        #
        # 新判据：**非终态 + 无活进程 = 故障**，不管平台有没有给它贴过标签。
        # 贴标签是过程，收敛才是结果。判据必须写在结果上。
        #
        # 唯一的豁免是"可寻址的人工决策"——人确实需要做决定、且这个决定在
        # 某个人看得见的队列里。那不是本脚本能判断的，得由 blocker 登记来证明；
        # 在 blocker 自动登记接上之前，这里不留豁免口子（宁可吵）。
        GRACE_MIN = 15  # 进程刚退出、平台还没来得及收敛的正常窗口
        stuck = [o for o in orphans if o["stale_min"] > GRACE_MIN]
        if stuck:
            worst = max(o["stale_min"] for o in stuck)
            n_invisible = len([o for o in stuck if o["status"] in INVISIBLE])
            n_marked = len([o for o in stuck if o["status"] in MARKED])
            print(f"\n✗ 断言失败：{len(stuck)} 个 session 非终态且无活进程，"
                  f"超过 {GRACE_MIN} 分钟没有收敛"
                  f"（隐形 {n_invisible} / 已标记但没人恢复 {n_marked}）。"
                  f"\n  最久 {worst/60:.1f} 小时。贴标签不是收敛 —— "
                  f"没有常驻组件持有『每个非终态 session 都必须有活着的驱动者』这条不变量。")
            sys.exit(1)
        print(f"\n✓ 断言通过：所有非终态 session 要么有活进程，"
              f"要么在 {GRACE_MIN} 分钟收敛窗口内。")


if __name__ == "__main__":
    main()
