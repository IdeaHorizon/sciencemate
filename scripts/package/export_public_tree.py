"""导出公开树 —— 个人版开源仓库的内容，从内部仓库机械地生成。

## 为什么是一个脚本，不是一份手工维护的仓库

源码只有一份（内部仓库）。专业版住在几个固定目录里，公开树 = 内部仓库删掉这些目录、
再删掉内部件（CI、部署脚本、过程文档、有版权的数据、公司自己的研究材料）之后的快照，
外加一个替换：前端接缝 `platform/frontend/src/edition.ts` 换成空的 `wire`。每次发版重跑，
真相源仍然只有内部 main —— 两棵树各自演化的那一天，就是分叉不报错的那一天。

## 只拷 git 跟踪的（`git ls-files`）

按盘扫会把本机的 `.venv`、`node_modules`、别人的项目仓库、测试库一起扫进来
（`scripts/package/git_tracked.py` 记着那次教训）。

## 出门检查

导出之后扫一遍：`from app.pro…`、`from app import pro`（装配处除外）、`from "@/pro/…"`
这样的 import 都拒绝 —— 那是核心还指着专业版，或者排除表漏了一处。CI 的导出闸接着在导出树里跑后端
测试、前端 typecheck/test/build（`.gitea/workflows/platform.yaml` 的 `public-tree` job）。

用法：
    python3 scripts/package/export_public_tree.py <输出目录> [--check-only]
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: 专业版：整个目录 / 文件不进公开树。（边界清单 §2.1、§3.1、§6）
PROFESSIONAL = (
    "platform/backend/app/pro/",
    "platform/backend/tests/pro/",
    "platform/frontend/src/pro/",
    "platform/frontend/src/app/login/",
    "platform/frontend/src/app/register/",
    "platform/frontend/src/app/(workspace)/organisation/",
    "deploy/org/",
    "scripts/package/build_server_bundle.py",
)

#: 内部件：既不是专业版，也不公开（CI、内网部署、过程文档、运维脚本）。
INTERNAL = (
    ".gitea/",
    "deploy/platform/",
    "deploy/imagegen-service/",
    "docs/",
    "docs-html/",
    "scripts/forgejo_merge_pr.sh",
    "dogfood_log.md",
    "DELIVERY.md",
    ".scope_map.yaml",
    "run-occam-local.sh",
    "run-preview-backend.sh",
    "equil_R4_rep1.restart",
    "platform/backend/docs/local_runtime.md",
    "platform/backend/scripts/seed_local_demo.py",
    "platform/backend/tests/test_seed_local_demo.py",
    # 只对内网部署 / 内部分工有意义的测试（它们读的正是上面排除的东西）。
    "tests/test_deploy_hands_the_service_to_the_unit.py",
    "tests/test_deploy_survives_a_free_port.py",
    "tests/test_the_debt_and_the_write_permission_must_match.py",
)

#: 被排除的目录里仍然要带上的：不是过程文档，是代码和闸读的登记表。
KEPT_INSIDE_EXCLUDED = (
    "docs/verdict_demolition/",
    "docs/compression-fate-table.md",
    # 文献采集在运行时读的期刊映射（app/services/literature_harvester.py）。
    "docs/xiaohongshu_journal_map.tsv",
)

#: 数据与材料：有版权的分区表、公司自己的研究夹具。
DATA = (
    "nodes/literature/data/reference/",
    "nodes/literature/file/",
    "nodes/writing/tests/fixtures/rebuild/",
)

EXCLUDED = PROFESSIONAL + INTERNAL + DATA

#: 唯一的接缝：内部树里它转发专业版的 wire，公开树里换成空桩。
SEAM = "platform/frontend/src/edition.ts"
SEAM_STUB = '/** 公开树：没有专业版，接线是空的（内部树里这个文件转发 `@/pro/wire`）。 */\nexport function wire(): void {}\n'

#: 出门检查：公开树里不许有**指向专业版的 import**（注释里提一句不算，那不会让树起不来）。
#: Python：`from app.pro…` / `import app.pro…` / `from app import pro`；TS：`from "@/pro/…"`。
#: 允许的例外：装配处按名字找它（`app/assembly.py`），和被换成空桩的接缝。
import re

FORBIDDEN_IMPORTS = (
    re.compile(r"^\s*(?:from\s+app\.pro\b|import\s+app\.pro\b|from\s+app\s+import\s+pro\b)", re.M),
    re.compile(r"""^\s*(?:import|export)\b[^\n]*\bfrom\s+["']@/pro/""", re.M),
    re.compile(r"""^\s*(?:import|export)\b[^\n]*\bfrom\s+["']\.{1,2}/(?:[\w.-]+/)*pro/""", re.M),
)
ALLOWED_TO_LOOK_FOR_IT = ("platform/backend/app/assembly.py", SEAM)


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, check=True, capture_output=True).stdout
    return [p.decode("utf-8") for p in out.split(b"\0") if p]


def _matches(path: str, rule: str) -> bool:
    return path == rule or (rule.endswith("/") and path.startswith(rule))


def is_excluded(path: str) -> bool:
    if any(_matches(path, rule) for rule in KEPT_INSIDE_EXCLUDED):
        return False
    return any(_matches(path, rule) for rule in EXCLUDED)


def public_files() -> list[str]:
    return [p for p in tracked_files() if not is_excluded(p)]


def export(into: Path) -> list[str]:
    if into.exists() and any(into.iterdir()):
        raise SystemExit(f"输出目录不是空的：{into}")
    into.mkdir(parents=True, exist_ok=True)
    files = public_files()
    for rel in files:
        src = REPO / rel
        dst = into / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            dst.symlink_to(src.readlink())
        else:
            shutil.copy2(src, dst)
    (into / SEAM).write_text(SEAM_STUB, encoding="utf-8")
    prune_path_keyed_registries(into)
    return files


#: 按文件路径登记东西的表：登记了公开树里没有的文件，那些表的闸会说"名单烂了"。
#: 导出时把指向被排除文件的条目删掉 —— 表的规则一个字不改，只是少了几行。
PATH_KEYED_REGISTRIES = ("framework_exemptions.yaml",)


def prune_path_keyed_registries(tree: Path) -> None:
    """按文本删条目（不经 yaml 重排：那会丢掉表里 70 行解释规则的注释）。
    一个条目 = 从 `  - file: X` 起到下一个 `  - file:` / 出块为止的那几行。"""
    for rel in PATH_KEYED_REGISTRIES:
        path = tree / rel
        if not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        out: list[str] = []
        dropping = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("- file:"):
                named = stripped[len("- file:"):].strip().strip("'\"")
                dropping = is_excluded(named)
            elif dropping and (not line.startswith((" ", "\t")) or stripped.startswith("- ")) and not stripped.startswith(("sites:", "argv_source:", "reason:")):
                dropping = False
            if not dropping:
                out.append(line)
        if out != lines:
            path.write_text("".join(out), encoding="utf-8")


def make_it_a_repository(tree: Path) -> str:
    """把导出树 `git init` 并提交一次：公开树就是一个 git 仓库，扫盘按 `git ls-files` 的闸
    （`tests/test_the_scan_corpus_is_what_git_says.py` 那一类）在里面才有话可说。"""
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=tree, check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("-c", "user.name=export", "-c", "user.email=export@localhost", "commit", "-q", "-m", "public tree export")
    return git("rev-parse", "--short", "HEAD")


def what_still_imports_the_pro_edition(tree: Path, files: list[str]) -> list[str]:
    """公开树里还 import 专业版的文件（核心指着它，或者排除表漏了一处）。"""
    hits: list[str] = []
    for rel in files:
        if rel in ALLOWED_TO_LOOK_FOR_IT or not rel.endswith((".py", ".ts", ".tsx")):
            continue
        path = tree / rel
        if path.is_symlink():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in FORBIDDEN_IMPORTS:
            found = pattern.search(text)
            if found:
                hits.append(f"{rel}: {found.group(0).strip()}")
                break
    return hits


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("into", nargs="?", help="输出目录（必须为空或不存在）")
    parser.add_argument("--check-only", action="store_true", help="不导出，只在内部树上做出门检查（按排除表算公开文件）")
    parser.add_argument("--git-init", action="store_true", help="导出后 git init 并提交一次（CI 的导出闸要在一个真的仓库里跑）")
    args = parser.parse_args(argv)
    if args.check_only:
        files = public_files()
        hits = what_still_imports_the_pro_edition(REPO, files)
        print(f"公开文件 {len(files)} 个（跟踪 {len(tracked_files())} 个，排除 {len(tracked_files()) - len(files)} 个）")
        if hits:
            print("❌ 这些公开文件还 import 专业版：\n  " + "\n  ".join(hits))
            return 1
        print("✓ 公开文件里没有一处 import 专业版")
        return 0
    if not args.into:
        parser.error("要么给输出目录，要么 --check-only")
    into = Path(args.into).resolve()
    files = export(into)
    hits = what_still_imports_the_pro_edition(into, files)
    print(f"导出 {len(files)} 个文件到 {into}（接缝 {SEAM} 已换成空桩）")
    if hits:
        print("❌ 导出树里还 import 专业版：\n  " + "\n  ".join(hits))
        return 1
    print("✓ 导出树里没有一处 import 专业版")
    if args.git_init:
        print(f"✓ git init + commit：{make_it_a_repository(into)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
