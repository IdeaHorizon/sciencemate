#!/usr/bin/env bash
# Forgejo pre-receive hook —— **纯 shell 版**（容器 Alpine 镜像里没 Python）。
#
# 部署：拷到 bare repo 的 hooks/pre-receive.d/scope-guard，chmod +x。
# Forgejo 容器内 bash + awk + git 都有，零外部依赖。
#
# 行为：跟 forgejo_pre_receive_hook.py 等价
#   - main 分支放行
#   - 其它分支：根据 newrev 中的 .scope_map.yaml 校验改动 path
#   - 找不到 map → 放行（fail-open）
#   - GITEA_PUSHER_NAME 拿不到 → 当 'unknown'，按 _anyone 限制
#
# YAML 解析靠 awk —— 严格依赖文件格式：
#   <key>:                 # 顶级 author
#     - "<glob>"           # 缩进 2 空格 + dash 列表项
#   _anyone:               # 特殊键，所有 author 都获得
#     - "..."

set -e

ZERO="0000000000000000000000000000000000000000"
AUTHOR="${GITEA_PUSHER_NAME:-unknown}"

# 提取某个 author 的所有 glob —— awk 解析 yaml
extract_globs() {
    local yaml="$1"
    local key="$2"
    printf '%s\n' "$yaml" | awk -v key="$key" '
        # 顶级 key 行（不以空格起头，含冒号）
        /^[a-zA-Z_][a-zA-Z0-9_]*:[[:space:]]*$/ {
            cur = $0
            sub(/:.*/, "", cur)
            in_key = (cur == key)
            next
        }
        # 非顶级缩进列表项
        in_key && /^[[:space:]]+-[[:space:]]+/ {
            val = $0
            sub(/^[[:space:]]+-[[:space:]]+/, "", val)
            # 去引号
            gsub(/^"|"$/, "", val)
            gsub(/^'\''|'\''$/, "", val)
            if (val != "") print val
        }
        # 内联空列表 []：跳过（无 glob）
    '
}

# 一个 path 是否匹配任一 glob？
# shell case 的 * 已经匹配 / —— 所以 nodes/literature/** 跟 nodes/literature/* 等价
matches_any() {
    local path="$1"
    shift
    local pat
    for pat in "$@"; do
        case "$path" in
            $pat) return 0 ;;
        esac
    done
    return 1
}

# 主循环
PASSED=0
while read -r oldrev newrev refname; do
    [ -z "$refname" ] && continue
    [ "$refname" = "refs/heads/main" ] && continue
    [ "$newrev" = "$ZERO" ] && continue  # 删分支
    # Forgejo 内部 sync 把 head ref 拷到 refs/pull/<N>/head（PUSHER 是 system /
    # 空）。这不是用户 push，跳过 scope 校验，否则 hook 拒后 PR head 没更新、CI
    # 不触发（symptom：force-push 到 head branch 成功但 PR 状态卡老 SHA）。
    case "$refname" in
        refs/pull/*) continue ;;
    esac

    # scope 基准恒为 main（见下方 CHANGED 说明）。main 不存在 → fail-open。
    MAIN_REF=$(git rev-parse refs/heads/main 2>/dev/null || echo "$ZERO")
    if [ "$MAIN_REF" = "$ZERO" ]; then
        echo "WARN: main 不存在，跳过 $refname 的 scope 校验" >&2
        continue
    fi

    # 拉 scope map：**main 优先**（trusted，经 PR review）；main 没有才 fallback
    # newrev（bootstrap 场景）。⚠️ 顺序是安全边界：newrev 优先会让 author 在
    # 自己分支改 map 给自己加 '*' 实现 self-lift（同 .py 版 2026-07 修复）。
    SCOPE_YAML=$(git show "refs/heads/main:.scope_map.yaml" 2>/dev/null || \
                 git show "$newrev:.scope_map.yaml" 2>/dev/null || \
                 echo "")
    if [ -z "$SCOPE_YAML" ]; then
        echo "WARN: .scope_map.yaml 在 $newrev 和 main 都没找到，跳过校验" >&2
        continue
    fi

    # 拼 allowed globs（author + _anyone）
    AUTHOR_GLOBS=$(extract_globs "$SCOPE_YAML" "$AUTHOR")
    ANYONE_GLOBS=$(extract_globs "$SCOPE_YAML" "_anyone")

    # 用换行连接，按行读进数组
    ALL_GLOBS=$(printf "%s\n%s" "$AUTHOR_GLOBS" "$ANYONE_GLOBS" | sed '/^$/d')

    # framework owner（'*'）
    if printf '%s\n' "$ALL_GLOBS" | grep -qx '\*'; then
        continue  # 通过
    fi

    # 拿改动文件 —— 基准恒为 **main**，用三点 diff（merge-base(main,newrev)→newrev）。
    #
    # ⚠️ 2026-07 修复（nidy 报的真 bug）：旧代码用 $oldrev（**分支在服务器上的旧
    # 位置**）作 base。当 owner 把 main merge 进自己长期分支再 push 时，旧分支位置
    # 远落后于 main，`oldrev...newrev` 会把 merge 进来的**整个 main**（几百个别人
    # 的文件）算成"本次改动"→ 全判越权。以 main 为 base：merge 进来的内容与 main
    # 相同 → 不出现在 diff；只剩 author 自己 commit 的净改动。三点 diff 同时正确
    # 处理"分支落后 main 未 merge"（落后的文件在 merge-base 侧，不算 author 改动）
    # 和 evil-merge（tree diff 不看 commit 结构，夹带的改动照样现形）。
    CHANGED=$(git diff --name-only "refs/heads/main...$newrev" 2>/dev/null || true)
    [ -z "$CHANGED" ] && continue

    # 检查每条
    VIOLATIONS=""
    while IFS= read -r FILE; do
        [ -z "$FILE" ] && continue
        # 权限元文件自改保护：非 '*' owner（上面已 continue 掉）一律拒，
        # 不管 glob 白名单怎么配（同 .py 版 PROTECTED_META_FILES）。
        case "$FILE" in
            .scope_map.yaml|framework_exemptions.yaml)
                VIOLATIONS="$VIOLATIONS$FILE (权限元文件，仅 framework owner 可改)
"
                continue ;;
        esac
        # 把 ALL_GLOBS 喂给 matches_any
        MATCHED=0
        while IFS= read -r PAT; do
            [ -z "$PAT" ] && continue
            case "$FILE" in
                $PAT) MATCHED=1; break ;;
            esac
        done <<EOF
$ALL_GLOBS
EOF
        if [ "$MATCHED" = "0" ]; then
            VIOLATIONS="$VIOLATIONS$FILE
"
        fi
    done <<EOF
$CHANGED
EOF

    if [ -n "$VIOLATIONS" ]; then
        PASSED=1
        echo "" >&2
        echo "╔════════ ❌ Scope guard 拒绝 push by '$AUTHOR' ════════" >&2
        echo "║  ref: $refname" >&2
        echo "║  越权改了以下文件：" >&2
        printf '%s' "$VIOLATIONS" | while IFS= read -r V; do
            [ -n "$V" ] && echo "║     $V" >&2
        done
        echo "║" >&2
        if [ -n "$ALL_GLOBS" ]; then
            echo "║  你被允许改的 path glob：" >&2
            printf '%s\n' "$ALL_GLOBS" | while IFS= read -r G; do
                [ -n "$G" ] && echo "║     $G" >&2
            done
        else
            echo "║  你（'$AUTHOR'）不在 .scope_map.yaml 里，没任何允许范围。" >&2
            echo "║  联系 framework owner 把你加进 map。" >&2
        fi
        echo "║" >&2
        echo "║  本地自查：python3 scripts/check_pr_scope.py $AUTHOR main HEAD" >&2
        echo "╚═════════════════════════════════════════════════════════" >&2
        echo "" >&2
    fi
done

exit $PASSED
