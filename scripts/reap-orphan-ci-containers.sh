#!/usr/bin/env bash
#
# 收掉 CI 的幽灵容器 —— **在 runner 宿主机上跑**（它需要 docker socket，
# 所以不能放进 job 里：给 job 开 docker socket 等于给任何 PR 里的代码宿主机 root）。
#
# 为什么需要它：这套 act_runner（v6.4.0）取消一个 run 时只在服务端标记取消，
# **不杀容器**。被取消的 job 容器留在那儿把整套测试跑完 —— 实测抓到过
# `Up 28 minutes / CPU=100.31%` 的一个，属于早就"已取消"的 run。
#
# 主要防线是 workflow 里关掉 `cancel-in-progress`（见那两个文件里的说明）。
# 这个脚本是兜底：手工 `docker rm -f` 过、runner 崩过、或者哪天又开了取消，
# 都会留下幽灵，而幽灵的症状是"CI 偶发红且复现不出来"，没人会往这想。
#
# 判据是**年龄**，不是名单：超过 THRESHOLD 分钟还活着的 CI 容器一律收掉。
# 正常一轮 CI 47 秒，10 分钟是 12 倍余量。
set -euo pipefail

THRESHOLD_MIN="${1:-10}"
now=$(date +%s)
killed=0

for name in $(docker ps --format '{{.Names}}' | grep '^FORGEJO-ACTIONS-TASK-' || true); do
    started=$(docker inspect -f '{{.State.StartedAt}}' "$name" 2>/dev/null) || continue
    started_ts=$(date -d "$started" +%s 2>/dev/null) || continue
    age_min=$(( (now - started_ts) / 60 ))
    if [ "$age_min" -ge "$THRESHOLD_MIN" ]; then
        cpu=$(docker stats --no-stream --format '{{.CPUPerc}}' "$name" 2>/dev/null || echo "?")
        echo "收掉 ${name}（已运行 ${age_min} 分钟，CPU=${cpu}）"
        docker rm -f "$name" >/dev/null 2>&1 && killed=$((killed + 1))
    fi
done

echo "共收掉 $killed 个（阈值 ${THRESHOLD_MIN} 分钟）"
