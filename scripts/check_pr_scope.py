#!/usr/bin/env python3
"""Scope guard：验证一次 git change 只动了 author 允许的路径。

配置文件：仓库根目录的 `.scope_map.yaml`（author → 允许的 path glob 列表）。

使用方式：

  # 本地自查（在 PR 推之前看看 scope 对不对）
  python3 scripts/check_pr_scope.py <author> <base_ref> <head_ref>

  # 自查跟 main 的差
  python3 scripts/check_pr_scope.py jicq main HEAD

  # CI / hook 包装调用同样姿势

退出码：
  0 = 通过
  1 = 有违规
  2 = 调用错误（脚本本身问题）

Pre-receive hook 在 Forgejo server 上跑 `scripts/forgejo_pre_receive_hook.py`
（独立文件，逻辑等价；server 上不能依赖 working tree）。
"""
from __future__ import annotations

import fnmatch
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: 需要 PyYAML（pip install pyyaml）", file=sys.stderr)
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCOPE_MAP_PATH = REPO_ROOT / ".scope_map.yaml"


def load_scope_map() -> dict:
    if not SCOPE_MAP_PATH.exists():
        print(f"ERROR: 找不到 {SCOPE_MAP_PATH}", file=sys.stderr)
        sys.exit(2)
    return yaml.safe_load(SCOPE_MAP_PATH.read_text(encoding="utf-8")) or {}


# ── git 吐出来的路径不是逐字的（#954）──────────────────────────────────────
#
# `git log --name-only` 默认按 `core.quotepath=true` 走：非 ASCII 字节被转义成
# 八进制、整串再套上双引号 ——
#
#     "nodes/literature/\344\272\214\347\272\247\345\255\246\347\247\221.tsv"
#
# 这个串匹配不上 `nodes/literature/**`，于是 owner 自己目录里的中文名文件被判成
# **越界**：push 成功，PR 一建就被自动关掉。现场表现像"权限没生效"，极难自诊。
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


def get_changed_files(base: str, head: str) -> list[str]:
    """只取 base..head 范围里 author 真正写的文件。

    用 `^main` 排除已经在 main 上的 commit（已经过 PR 评审）；
    用 `--no-merges` 排除 author 自己 merge main 的整合 commit。
    """
    try:
        out = subprocess.check_output(
            [
                "git", "-c", "core.quotepath=false",
                "log", "--no-merges", "--name-only", "--pretty=format:",
                f"{base}..{head}", "^refs/heads/main",
            ],
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as e:
        print(
            f"ERROR: git log 失败：{e.stderr.decode().strip()}",
            file=sys.stderr,
        )
        sys.exit(2)
    seen: set[str] = set()
    deduped: list[str] = []
    for line in out.decode().splitlines():
        line = line.strip()
        if line:
            line = _unquoted_or_die(line, "git log --name-only")
        if line and line not in seen:
            seen.add(line)
            deduped.append(line)
    return deduped


def main() -> int:
    if len(sys.argv) != 4:
        print(
            "Usage: check_pr_scope.py <author> <base_ref> <head_ref>",
            file=sys.stderr,
        )
        return 2

    author, base, head = sys.argv[1], sys.argv[2], sys.argv[3]
    scope_map = load_scope_map()

    allowed: list[str] = []
    allowed.extend(scope_map.get(author, []) or [])
    allowed.extend(scope_map.get("_anyone", []) or [])

    if "*" in allowed:
        print(f"✓ '{author}' 是 framework owner（* allow-all）")
        return 0

    if not allowed:
        print(
            f"⚠️  author '{author}' 不在 .scope_map.yaml 且 _anyone 为空。"
            f" 任何改动都会被拒。",
            file=sys.stderr,
        )

    files = get_changed_files(base, head)
    if not files:
        print(f"✓ '{author}' 没改任何文件（{base}..{head}）")
        return 0

    violations: list[str] = [
        f for f in files
        if not any(fnmatch.fnmatch(f, pat) for pat in allowed)
    ]

    if violations:
        print("", file=sys.stderr)
        print(f"❌ scope guard 拒绝 '{author}' 的改动：", file=sys.stderr)
        for v in violations:
            print(f"     {v}", file=sys.stderr)
        print("", file=sys.stderr)
        print(f"  你被允许改的 path glob：", file=sys.stderr)
        for p in allowed:
            print(f"     {p}", file=sys.stderr)
        print("", file=sys.stderr)
        print(
            "  如需跨 scope 改动（例如修了 framework bug），找 framework owner 代为 push。",
            file=sys.stderr,
        )
        return 1

    print(f"✓ '{author}' 改的 {len(files)} 个文件全部在 scope 内")
    return 0


if __name__ == "__main__":
    sys.exit(main())
