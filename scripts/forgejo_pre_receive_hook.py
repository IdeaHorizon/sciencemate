#!/usr/bin/env python3
"""Forgejo pre-receive hook 实现 —— **不要直接放在文件系统**，复制粘贴用。

部署位置：Forgejo Web UI → 仓库 → Settings → Git Hooks → pre-receive
  把本文件 from `#!/usr/bin/env python3` 起的整段内容粘进去，保存。

执行环境：
  - 在 Forgejo server 上跑（bare repo 工作目录）
  - 读 stdin：每行 `<oldrev> <newrev> <refname>`
  - 环境变量 `GITEA_PUSHER_NAME` = 推送者 username
  - 需要 server 上有 python3 + PyYAML（pip install pyyaml）

行为：
  - 对 main 分支：放行（PR merge 走 force_merge API 不应被本 hook 拦）
  - 对其它分支：根据 **main 上的** `.scope_map.yaml` 校验改动 path
  - main 上没有 .scope_map.yaml → fallback 到 newrev 里的版本（bootstrap 场景）
  - 都没有 / PyYAML 没装 → 警告但放行（fail-open，避免锁死）
  - merge commit 不再整体豁免：对每个 merge commit 校验 combined diff
    （相对**所有 parent 都不同**的文件），堵 "evil merge" 夹带
  - `.scope_map.yaml` / `framework_exemptions.yaml` 本身：非 '*' owner 一律拒改

设计原则：
  - **信任 main 上的 map**（经过 PR review 的版本），而不是 newrev 里的 ——
    否则 author 在自己分支上改 map 给自己加 '*' 就能自我提权（self-lift）。
    v1 曾从 newrev 读（想让"自己 push 的新版本去 gate 自己"），audit 发现
    这个方向只防自我收紧、不防自我放权，2026-07 翻转为 main 优先。
  - fail-open 而不是 fail-closed：宁愿放过偶发问题也不想锁死自己 push
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import List

try:
    import fnmatch
    import yaml
except ImportError as e:
    print(
        f"WARN: scope guard 依赖缺失 ({e})；放行（fail-open）。"
        f"在 Forgejo server 上 `pip install pyyaml` 启用检查。",
        file=sys.stderr,
    )
    sys.exit(0)

ZERO = "0" * 40
AUTHOR = os.environ.get("GITEA_PUSHER_NAME", "").strip() or "unknown"

# 权限元文件：只有 scope 含 '*' 的 owner 能改（glob 白名单管不到它们——
# 即便 _anyone 配了宽 glob，也不能借道改掉权限配置本身）。
PROTECTED_META_FILES = {".scope_map.yaml", "framework_exemptions.yaml"}


def _run(args: List[str]) -> str:
    """跑 git 命令；失败抛 CalledProcessError。"""
    return subprocess.check_output(
        args, stderr=subprocess.DEVNULL,
    ).decode(errors="replace")


def _load_scope_map(newrev: str) -> dict | None:
    """读 `.scope_map.yaml`：**main 优先**（trusted，经 PR review），main 没有
    才回退 newrev（bootstrap 新仓库场景）；都没 → None。

    ⚠️ 顺序是安全边界：从 newrev 优先读会让 author 在自己分支改 map 给自己
    加 '*' 实现 self-lift。别翻回去。"""
    for ref in ("refs/heads/main", newrev):
        try:
            raw = _run(["git", "show", f"{ref}:.scope_map.yaml"])
        except subprocess.CalledProcessError:
            continue
        try:
            return yaml.safe_load(raw) or {}
        except yaml.YAMLError as e:
            print(f"WARN: .scope_map.yaml 解析失败 in {ref}: {e}", file=sys.stderr)
            return None
    return None


def _resolve_base(oldrev: str) -> str | None:
    """新分支（oldrev 全 0）→ 用 main 作为 base；main 都没 → None 跳过校验。"""
    if oldrev != ZERO:
        return oldrev
    try:
        return _run(["git", "rev-parse", "refs/heads/main"]).strip()
    except subprocess.CalledProcessError:
        return None


# ── git 吐出来的路径不是逐字的（#954）──────────────────────────────────────
#
# `git log --name-only` 默认按 `core.quotepath=true` 走：非 ASCII 字节被转义成
# 八进制、整串再套上双引号 ——
#
#     "nodes/literature/\344\272\214\347\272\247\345\255\246\347\247\221.tsv"
#
# 这个串匹配不上 `nodes/literature/**`，于是 owner 自己目录里的中文名文件被判成
# **越界**：push 成功，PR 一建就被自动关掉。现场表现像"权限没生效"，极难自诊。
#
# （这段与 `scripts/check_pr_scope.py` 里那份逐字相同：pre-receive hook 部署在
# 服务端、对着 bare 仓库跑，import 不到仓库里的任何模块，只能各带一份。）
#
# 两道一起上，缺一不可：
#   1. `-c core.quotepath=false` —— 让 git 直接吐 UTF-8 原文；
#   2. 收到仍然带引号的行就**吵出来并拒判**。quotepath=false 不是万能的：名字里
#      真有双引号或换行时 git 照样会引。那时静默按字面匹配 = 再次误判成越界，
#      正是这个 bug 的形状。宁可停下来让人看见，也不要继续悄悄给错答案。


def _unquoted_or_die(path: str, where: str) -> str:
    """路径必须是逐字的；还带着 git 的引号就说明上面那道参数没生效。"""
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        print(
            f"ERROR: {where} 返回的路径仍是 git 的转义形式：{path}\n"
            "       它匹配不上任何 scope glob，会被误判成越界。"
            "       不做判定直接停下 —— 见 #954。",
            file=sys.stderr,
        )
        sys.exit(2)
    return path


def _merge_introduced_files(base: str, newrev: str) -> list[str]:
    """列出 `base..newrev`（排除 main 已有 commit）里每个 merge commit 的
    **combined diff** 文件 —— 即 merge 结果相对**所有 parent 都不同**的文件
    （`git diff-tree -c` 语义）。

    为什么这样堵 "evil merge"：诚实 merge 里每个文件要么原样取自某个 parent
    （不出现在 -c 输出），要么是双方都改过的冲突解决（出现，但必须仍在
    author scope 内）。借 merge commit 夹带任一 parent 都没有的改动
    （比如 `git merge main --no-commit` 之后顺手改 core/）会在这里现形 ——
    老实现用 `--no-merges` 把 merge commit 整体跳过，正好漏掉这条路。

    fail-open：git 命令失败只 WARN 返 []，跟其余路径一致。
    """
    try:
        merges = _run([
            "git", "rev-list", "--merges", f"{base}..{newrev}",
            "^refs/heads/main",
        ]).split()
    except subprocess.CalledProcessError as e:
        print(f"WARN: git rev-list --merges 失败：{e}；跳过 merge 校验", file=sys.stderr)
        return []
    seen: set[str] = set()
    out: list[str] = []
    for m in merges:
        try:
            lines = _run([
                "git", "-c", "core.quotepath=false",
                "diff-tree", "--no-commit-id", "-c", "--name-only", "-r", m,
            ]).splitlines()
        except subprocess.CalledProcessError as e:
            print(f"WARN: git diff-tree -c {m[:12]} 失败：{e}；跳过该 merge", file=sys.stderr)
            continue
        for f in lines:
            f = f.strip()
            if f:
                f = _unquoted_or_die(f, "git diff-tree -c --name-only")
            if f and f not in seen:
                seen.add(f)
                out.append(f)
    return out


def _check_one_ref(oldrev: str, newrev: str, refname: str) -> bool:
    """校验单条 ref 更新。返 True=通过；False=拒。"""
    if refname == "refs/heads/main":
        return True  # main 放行（PR merge API 走这条）
    if newrev == ZERO:
        return True  # 删分支
    if refname.startswith("refs/pull/"):
        # Forgejo 内部 sync 把 head ref 拷到 refs/pull/<N>/head（PUSHER 空）。
        # 不是用户 push，跳过；否则 hook 拒后 PR head 没更新、CI 不触发。
        return True

    base = _resolve_base(oldrev)
    if base is None:
        print(
            f"WARN: 新分支 {refname} 但 main 不存在，跳过 scope 校验",
            file=sys.stderr,
        )
        return True

    scope_map = _load_scope_map(newrev)
    if scope_map is None:
        print(
            f"WARN: 在 {newrev} 和 main 都没找到 .scope_map.yaml，"
            "跳过 scope 校验（fail-open）",
            file=sys.stderr,
        )
        return True

    allowed: list[str] = []
    allowed.extend(scope_map.get(AUTHOR) or [])
    allowed.extend(scope_map.get("_anyone") or [])

    if "*" in allowed:
        return True  # framework owner

    try:
        # 关键：用 `^main` 排除"已经在 main 上的 commit"——已经过 main PR
        # 评审的内容不算 author push 进来的越权改动。
        #
        # 之前的 `git diff base...newrev` 三点写法在这个场景下等价于两点
        # （base 是 newrev 的 ancestor），导致 `git merge main` 拉进的新文件
        # 也被算成"author 改的"。同事 nidy 2026-06-18 撞到：他 merge main
        # 拿了 PR #72 的 core/executor.py，hook 误报他越权改了 core/。
        #
        # 范围 `base..newrev ^refs/heads/main` = 在 newrev 中但既不在 base
        # 也不在 main 上的 commit；merge 拉进来的 main commit 全被 `^main`
        # 排除。`--no-merges` 再额外把 author 自己 merge 的整合 commit 排掉，
        # 只看真正写代码的 commit。
        log_out = _run([
            "git", "-c", "core.quotepath=false",
            "log", "--no-merges", "--name-only", "--pretty=format:",
            f"{base}..{newrev}", "^refs/heads/main",
        ])
        changed = log_out.splitlines()
    except subprocess.CalledProcessError as e:
        print(f"WARN: git log 失败：{e}；放行", file=sys.stderr)
        return True

    # 去重 + 去空行
    seen: set[str] = set()
    deduped: list[str] = []
    for c in changed:
        c = c.strip()
        if c and c not in seen:
            seen.add(c)
            deduped.append(c)
    changed = deduped

    # ── merge commit 校验（evil-merge 堵漏）────────────────────────────────
    # `--no-merges` 让上面的 log 只看普通 commit；merge commit 自己的
    # combined diff（夹带内容会藏在这）单独拉出来一并过 glob 校验。
    merge_changed = _merge_introduced_files(base, newrev)
    via_merge = set(merge_changed) - set(changed)
    all_changed = changed + [f for f in merge_changed if f in via_merge]
    if not all_changed:
        return True

    # ── 权限元文件自改保护（self-lift 堵漏）────────────────────────────────
    # 到这里 author 一定不是 '*' owner（上面已 early-return），改权限配置
    # 本身一律拒 —— 不管 glob 白名单怎么配。
    protected_hits = [f for f in all_changed if f in PROTECTED_META_FILES]
    if protected_hits:
        print("", file=sys.stderr)
        print(f"╔════════ ❌ Scope guard 拒绝 push by '{AUTHOR}' ════════", file=sys.stderr)
        print(f"║", file=sys.stderr)
        print(f"║  ref: {refname}", file=sys.stderr)
        print(f"║  改了权限元文件（只有 scope 含 '*' 的 framework owner 能改）：", file=sys.stderr)
        for v in protected_hits:
            suffix = "（经 merge commit 夹带）" if v in via_merge else ""
            print(f"║     {v}{suffix}", file=sys.stderr)
        print(f"║", file=sys.stderr)
        print(f"║  需要调整自己的 scope？联系 framework owner 提 PR 改 main 上的 map。", file=sys.stderr)
        print(f"╚═════════════════════════════════════════════════════════", file=sys.stderr)
        print("", file=sys.stderr)
        return False

    violations = [
        f for f in all_changed
        if not any(fnmatch.fnmatch(f, pat) for pat in allowed)
    ]
    if not violations:
        return True

    print("", file=sys.stderr)
    print(f"╔════════ ❌ Scope guard 拒绝 push by '{AUTHOR}' ════════", file=sys.stderr)
    print(f"║", file=sys.stderr)
    print(f"║  ref: {refname}", file=sys.stderr)
    print(f"║  越权改了以下文件：", file=sys.stderr)
    for v in violations:
        suffix = "（经 merge commit 夹带 —— evil merge?）" if v in via_merge else ""
        print(f"║     {v}{suffix}", file=sys.stderr)
    print(f"║", file=sys.stderr)
    if allowed:
        print(f"║  你被允许改的 path glob：", file=sys.stderr)
        for p in allowed:
            print(f"║     {p}", file=sys.stderr)
    else:
        print(
            f"║  你（'{AUTHOR}'）不在 .scope_map.yaml 里，没任何允许范围。",
            file=sys.stderr,
        )
        print(
            f"║  联系 framework owner 把你加进 map。",
            file=sys.stderr,
        )
    print(f"║", file=sys.stderr)
    print(
        f"║  本地自查命令：python3 scripts/check_pr_scope.py {AUTHOR} main HEAD",
        file=sys.stderr,
    )
    print(f"╚═════════════════════════════════════════════════════════", file=sys.stderr)
    print("", file=sys.stderr)
    return False


def main() -> int:
    passed = True
    for line in sys.stdin:
        parts = line.strip().split()
        if len(parts) != 3:
            continue
        oldrev, newrev, refname = parts
        if not _check_one_ref(oldrev, newrev, refname):
            passed = False
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
