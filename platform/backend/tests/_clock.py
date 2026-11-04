"""「刚刚发表」的测试数据要往回让一点。

推荐流的时间窗都写成 ``published_at <= now``（未来日期的条目不进池），而 ``now`` 是
被测代码**自己**再取一次的。测试里把条目戳成 ``datetime.now(UTC)``，就是在赌两次取
墙钟之间钟不会往回走。

CI runner 在家里那台 PC 的 WSL2 里，墙钟曾每 ~32 秒往回跳 ~1 秒（2026-10-30 实测：
100 秒内 −995 / −1159 / −928 ms 三次；``dmesg`` 里 ``systemd-journald: Time jumped
backwards`` 一天上千条）。根因是 WSL 里两个校时源拉锯（Hyper-V 跟 Windows 主机、
timesyncd 跟 NTP，两者差 0.7 秒），11-02 已在主机上停掉 timesyncd；但测试不该押注于
「这台机器的钟今天是好的」，所以这里的余量照留。往回跳正好落在
两次取钟之间，四条「刚刚发表」的条目就全成了未来条目 —— ``test_feed_asset_queue``
在 PR#1276 上那次 ``assert set() == {...}`` 就是这么来的，重跑即绿。

所以造「刚刚」用 ``just_now()``：往回让一分钟，远大于一次跳变，又远小于任何一个窗口
（最短 31 天），测的意思一点不变。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

#: 比一次墙钟回跳（~1 s）大得多、比最短的时间窗（31 天）小得多。
CLOCK_STEP_MARGIN = timedelta(minutes=1)


def just_now() -> datetime:
    """「刚刚」—— 对被测代码随后取的 ``now`` 来说，一定不在未来。"""
    return datetime.now(UTC) - CLOCK_STEP_MARGIN
