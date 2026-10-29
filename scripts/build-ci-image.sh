#!/usr/bin/env bash
#
# 在 runner 那台机器上重建 CI 基础镜像。
#
# 什么时候要跑：改了 `.gitea/ci-deps.sh`、`pyproject.toml`、后端 `uv.lock`
# 或前端 `package-lock.json` 之后。不跑也不会错，只会慢（job 自己把差的补
# 下来）—— 见 Dockerfile.ci 顶部那段。
set -euo pipefail

cd "$(dirname "$0")/.."
IMAGE="${1:-harness-ci:latest}"

echo "构建 $IMAGE ..."
docker build -f .gitea/Dockerfile.ci -t "$IMAGE" .

echo
for profile in framework backend frontend; do
    baked="$(docker run --rm "$IMAGE" cat "/etc/harness-ci-deps-$profile")"
    case "$profile" in
        framework) now="$(cat .gitea/ci-deps.sh pyproject.toml | sha256sum | cut -c1-16)" ;;
        backend)   now="$(cat .gitea/ci-deps.sh platform/backend/uv.lock | sha256sum | cut -c1-16)" ;;
        frontend)  now="$(cat .gitea/ci-deps.sh platform/frontend/package-lock.json | sha256sum | cut -c1-16)" ;;
    esac
    [ "$baked" = "$now" ] && verdict="一致" || verdict="不一致（job 会自己补装）"
    echo "指纹 ${profile}：镜像 ${baked} / 工作区 ${now} → ${verdict}"
done
echo
echo "让 runner 用它：把 ~/services/forgejo-runner/data/.runner 里的"
echo "  \"ubuntu-latest:docker://node:20-bookworm\""
echo "改成"
echo "  \"ubuntu-latest:docker://$IMAGE\""
echo "然后 docker restart forgejo-runner"
