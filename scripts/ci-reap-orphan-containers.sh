#!/usr/bin/env bash
#
# 收掉 CI 的幽灵容器 —— 在 runner 那台机器上按 cron 跑（它要 docker socket，所以不能放进
# job 里：给 job 开 docker socket 等于给任何 PR 里的代码宿主机 root）。
#
# 为什么需要它：act_runner（v6.4.0）取消一个 run 时只在服务端标记取消，**不杀容器**。被取消
# 的 job 容器留在那儿把整套测试跑完，和真 job 抢 CPU —— 症状是「CI 偶发红且复现不出来」。
#
# ## 判据：比任何一个 job 的超时还老
#
# 活着的 job 不可能比它自己的 `timeout-minutes` 更老 —— runner 到点就杀。所以「比所有
# workflow 里最大的 `timeout-minutes` 还老」的 CI 容器一定是幽灵，而比它年轻的一个都不碰。
#
# 这个脚本从前在 runner 主机上手写，阈值写死 10 分钟，而 job 的超时是 15–30 分钟：
# 2026-08-20 到 10-31 它 `docker rm -f` 了 25 个**正在跑**的 job（日志里没有任何取消记录，
# 十来个被收时 CPU 90%+），每一个在 CI 上都是一次莫名其妙的红；10-31 那次 public-tree
# 就是被它在第 10 分钟收掉的。阈值从 workflow 里现读，以后谁调 `timeout-minutes`，
# 这里自动跟上；没声明 `timeout-minutes` 的 job 会让这里拒绝干活（见下）。
#
# 用法（runner 主机，fcbay 的 crontab），与 ci-image-autorebuild.sh 同一种取法：
#     */10 * * * *  cd /mnt/c/Users/fcbay/build/harness-framework \
#                    && git fetch -q origin +refs/heads/main:refs/remotes/origin/main \
#                    && git show origin/main:scripts/ci-reap-orphan-containers.sh | bash -s -- "$PWD" >> ~/reap-ci.log 2>&1
#
# `--print-threshold`：只打印算出来的阈值（分钟），不碰 docker —— 测试用它核对判据。
set -euo pipefail

PRINT_ONLY=0
if [ "${1:-}" = "--print-threshold" ]; then
    PRINT_ONLY=1
    shift
fi
REPO="${1:?用法: $0 [--print-threshold] <harness-framework checkout>}"
REF="${CI_REAP_REF:-origin/main}"
MARGIN_MIN=10      # 排队、拉镜像、收尾步骤的余量；宁可晚收一个幽灵，不能误杀一个活 job

cd "$REPO"

# 每个 job 的 `timeout-minutes`。一个 job 都没声明、或有 job 没声明，就不知道「最老的活 job
# 有多老」（runner 的默认是几个小时）—— 那时宁可不收，也不能按猜的阈值去杀。
max_timeout=0
for file in $(git ls-tree --name-only "$REF" .gitea/workflows/); do
    case "$file" in *.yml|*.yaml) ;; *) continue ;; esac
    body="$(git show "$REF:$file")"
    jobs="$(printf '%s\n' "$body" | awk '/^jobs:/{in_jobs=1; next} in_jobs && /^[^ #]/{in_jobs=0} in_jobs && /^  [A-Za-z0-9_-]+:[[:space:]]*$/{n++} END{print n+0}')"
    timeouts="$(printf '%s\n' "$body" | grep -E '^    timeout-minutes:[[:space:]]*[0-9]+' | grep -oE '[0-9]+' || true)"
    declared="$(printf '%s\n' "$timeouts" | grep -c . || true)"
    if [ "$jobs" -ne "$declared" ]; then
        echo "$(date +%FT%T) $file 有 $jobs 个 job，只有 $declared 个声明了 timeout-minutes —— 不知道活 job 能活多久，这一轮不收" >&2
        exit 1
    fi
    for t in $timeouts; do
        [ "$t" -gt "$max_timeout" ] && max_timeout="$t"
    done
done
if [ "$max_timeout" -eq 0 ]; then
    echo "$(date +%FT%T) 在 $REF 的 .gitea/workflows/ 里没读到任何 timeout-minutes，这一轮不收" >&2
    exit 1
fi
THRESHOLD_MIN=$((max_timeout + MARGIN_MIN))

if [ "$PRINT_ONLY" -eq 1 ]; then
    echo "$THRESHOLD_MIN"
    exit 0
fi

now=$(date +%s)
killed=0
for name in $(docker ps --format '{{.Names}}' | grep '^FORGEJO-ACTIONS-TASK-' || true); do
    started=$(docker inspect -f '{{.State.StartedAt}}' "$name" 2>/dev/null) || continue
    started_ts=$(date -d "$started" +%s 2>/dev/null) || continue
    age_min=$(( (now - started_ts) / 60 ))
    if [ "$age_min" -ge "$THRESHOLD_MIN" ]; then
        cpu=$(docker stats --no-stream --format '{{.CPUPerc}}' "$name" 2>/dev/null || echo "?")
        echo "$(date +%FT%T) 收掉 ${name}（已运行 ${age_min} 分钟，CPU=${cpu}）"
        docker rm -f "$name" >/dev/null 2>&1 && killed=$((killed + 1))
    fi
done

echo "$(date +%FT%T) 共收掉 $killed 个（阈值 ${THRESHOLD_MIN} 分钟 = 最长 job 超时 ${max_timeout} + 余量 ${MARGIN_MIN}）"
