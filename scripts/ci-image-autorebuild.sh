#!/usr/bin/env bash
#
# 在 runner 那台机器上按 cron 跑：main 的依赖指纹和镜像里烘的对不上，就重建一次镜像。
#
# 镜像是缓存不是真相源（见 .gitea/Dockerfile.ci 顶部）—— 过期不会错，只会让每个
# job 从头 apt + pip。2026-08-19 建的那份就这样悄悄过期了两个月：没人看 job 日志
# 里那一行「依赖已烘进镜像」有没有出现，直到清华镜像把 runner 的出口 IP 拉黑
# （2026-10-28，四个 job 秒红）。缓存会不会过期不该靠人记得，所以交给 cron。
#
# 用法（runner 主机，fcbay 的 crontab）：
#     17 * * * *  cd /mnt/c/Users/fcbay/build/harness-framework \
#                  && git fetch -q origin +refs/heads/main:refs/remotes/origin/main \
#                  && git show origin/main:scripts/ci-image-autorebuild.sh | bash -s -- "$PWD" >> ~/ci-image-autorebuild.log 2>&1
#
# 用 `git show origin/main:…` 取脚本本身：那个 checkout 的工作树停在旧提交上，
# 不靠它有没有 checkout 过 main。
#
# 只读 `origin/main` 的对象，不碰那个 checkout 的工作树；构建 context 是临时目录里
# 的六份清单（和 .dockerignore 白名单同一份）。镜像站只被单流拉一遍。
set -euo pipefail

REPO="${1:?用法: $0 <harness-framework checkout> [镜像名]}"
IMAGE="${2:-harness-ci:latest}"
REF=origin/main
LOCK=/tmp/ci-image-autorebuild.lock

exec 9>"$LOCK"
if ! flock -n 9; then
    echo "$(date +%FT%T) 上一轮还在建，跳过"
    exit 0
fi

cd "$REPO"
# 写明 refspec：`git fetch origin main` 在老克隆上只更新 FETCH_HEAD，不动
# refs/remotes/origin/main（2026-10-29 runner 上那份 origin/main 还停在 PR#73），
# 指纹就会按几个月前的文件算、再把镜像建成古董。
git fetch -q origin "+refs/heads/main:refs/remotes/origin/main"

# 与 .gitea/ci-deps.sh::_fingerprint / Dockerfile.ci 最后一步同一种算法：
# ci-deps.sh 与各 profile 的清单拼起来取 sha256 前 16 位。
fingerprint() {
    { git show "$REF:.gitea/ci-deps.sh"; git show "$REF:$1"; } | sha256sum | cut -c1-16
}
baked() {
    docker run --rm "$IMAGE" cat "/etc/harness-ci-deps-$1" 2>/dev/null || echo none
}

want_framework="$(fingerprint pyproject.toml)"
want_backend="$(fingerprint platform/backend/uv.lock)"
want_frontend="$(fingerprint platform/frontend/package-lock.json)"
if [ "$want_framework" = "$(baked framework)" ] \
    && [ "$want_backend" = "$(baked backend)" ] \
    && [ "$want_frontend" = "$(baked frontend)" ]; then
    echo "$(date +%FT%T) 指纹一致（${want_framework} / ${want_backend} / ${want_frontend}），不建"
    exit 0
fi

SRC="$(mktemp -d /tmp/ci-image-src.XXXXXX)"
trap 'rm -rf "$SRC"' EXIT
git archive "$REF" \
    .dockerignore .gitea/Dockerfile.ci .gitea/ci-deps.sh pyproject.toml \
    platform/backend/pyproject.toml platform/backend/uv.lock \
    platform/frontend/package.json platform/frontend/package-lock.json \
    scripts/build-ci-image.sh \
    | tar -x -C "$SRC"
echo "$(date +%FT%T) 指纹变了，从 $(git rev-parse --short=9 "$REF") 重建 ${IMAGE}"
bash "$SRC/scripts/build-ci-image.sh" "$IMAGE"
echo "$(date +%FT%T) 建完"
