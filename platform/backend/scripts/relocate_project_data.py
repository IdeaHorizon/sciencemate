"""把 Project 仓库与 Session worktree 搬到一个新的数据根，并让库跟着改。

这是**换根的唯一合法出口**。启动闸（`main._refuse_to_serve_where_the_data_is_not`）
在配置的根与库里记录的根不一致时会拒绝启动，并把这条命令报给你 —— 因为除此
之外的"换根"都不是换根，是把数据分裂成两处。

它做四件事，缺一件就是把问题往后挪：

1. 搬目录：`<旧根>/<project_id>` → `<新根>/<project_id>`，仓库与 worktree 都搬。
2. `git worktree repair`：Git 的 worktree 是**双向绝对路径**（worktree 里的
   `.git` 指向仓库的 `worktrees/<id>`，后者的 `gitdir` 又指回来）。只 mv 不
   repair，两边互相指着不存在的路径，`git status` 当场就废。
3. 改写 `sessions.git_worktree_path`：它是"数据当时放在哪"的证据，也是启动闸
   的判据。不改写 = 下次启动照样被拦。
4. 复核：搬完后按新根逐条现算 `session_path()`，全部落在实处才算成功。

默认 dry-run，`--apply` 才真动。搬之前请先停掉 App Server 与 worker：这个脚本
不会去猜"有没有人正在写"。
"""

import argparse
import asyncio
import shutil
import subprocess
from collections import Counter
from pathlib import Path

from sqlalchemy import text

from app.database import get_session_factory

LAYOUT = (("project-repositories", "repositories"), ("project-worktrees", "worktrees"))


def _repository_root_beside(worktree_root: Path) -> Path | None:
    """找与这个 worktree 根配对的仓库根。

    两种拼法都得认：`project-repositories`（shared 布局）与
    `project_repositories`（相对默认值那一版在 release 里建出来的）。只认一种
    的后果是搬走 worktree、把仓库留在原地 —— Git 的双向指针当场断掉，而
    `mv` 本身不会报任何错。
    """
    for name in ("project-repositories", "project_repositories"):
        candidate = worktree_root.parent / name
        if candidate.is_dir():
            return candidate
    return None


def _run(*command: str) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True)


async def _recorded_paths() -> list[tuple[str, str, str]]:
    factory = get_session_factory()
    async with factory() as db:
        rows = await db.execute(
            text(
                "SELECT session_id, project_id, git_worktree_path FROM sessions "
                "WHERE archived_at IS NULL AND git_worktree_path IS NOT NULL"
            )
        )
        return [(str(a), str(b), str(c)) for a, b, c in rows.all()]


async def _rewrite(mapping: dict[str, str]) -> int:
    factory = get_session_factory()
    async with factory() as db:
        changed = 0
        for session_id, new_path in mapping.items():
            await db.execute(
                text("UPDATE sessions SET git_worktree_path = :p WHERE session_id = :s"),
                {"p": new_path, "s": session_id},
            )
            changed += 1
        await db.commit()
        return changed


def _move_project(kind_dir: str, project_id: str, source_root: Path, target_root: Path,
                  apply: bool) -> str | None:
    source = source_root / project_id
    target = target_root / project_id
    if not source.exists() and not source.is_symlink():
        return None
    if target.exists() and not target.is_symlink() and source.resolve() == target.resolve():
        return f"    已在位 {kind_dir}/{project_id}"
    if not apply:
        return f"    将搬 {source} → {target}"
    target_root.mkdir(parents=True, exist_ok=True)

    # 软链要按它指向的**真身**处理，别把链本身搬来搬去。两个方向都会遇到：
    #   source 是链：上一轮"临时桥接"在旧根里留下的指路牌；
    #   target 是链：同一次桥接在**新根**里留下的、正指着 source 的那一条 ——
    #     它让 `target.exists()` 为真，看起来像"目标已存在"，实则是同一份数据。
    #     不认这一种，脚本会在自己造的指路牌前止步（8-21 实测差点如此）。
    real = source.resolve()
    if target.is_symlink():
        if target.resolve() == real:
            target.unlink()
        else:
            return f"    ⛔ 目标是指向别处的软链，人工处置：{target} → {target.resolve()}"
    if source.is_symlink():
        source.unlink()
        if real == target:
            return f"    已解桥 {source}（数据本来就在 {target}）"
        shutil.move(str(real), str(target))
    else:
        if target.exists():
            return f"    ⛔ 目标已存在且不同源，人工处置：{target}"
        shutil.move(str(source), str(target))
    return f"    已搬 {source} → {target}"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", required=True, help="新的 PLATFORM_DATA_ROOT（绝对路径）")
    parser.add_argument("--apply", action="store_true", help="真的动手（默认只打印计划）")
    args = parser.parse_args()

    target_root = Path(args.to).expanduser()
    if not target_root.is_absolute():
        raise SystemExit(f"--to 必须是绝对路径，收到 {args.to!r}")

    recorded = await _recorded_paths()
    if not recorded:
        print("库里没有任何活跃 Session 的 worktree 记录 —— 无需搬迁。")
        return

    roots = Counter(str(Path(p).parent.parent) for _, _, p in recorded)
    print(f"目标数据根: {target_root}")
    print("库里记录的 worktree 根：")
    for root, count in roots.most_common():
        mark = "（已是目标）" if root == str(target_root / "project-worktrees") else ""
        print(f"  {count:>4} 个会话  {root} {mark}")

    projects = sorted({project_id for _, project_id, _ in recorded})
    mode = "执行中" if args.apply else "（dry-run，加 --apply 才动手）"
    print(f"\n涉及 {len(projects)} 个 Project。{mode}")

    for project_id in projects:
        source_roots = {
            str(Path(p).parent.parent) for _, pid, p in recorded if pid == project_id
        }
        print(f"  {project_id}")
        for source_worktree_root in sorted(source_roots):
            worktree_root = Path(source_worktree_root)
            repository_root = _repository_root_beside(worktree_root)
            if repository_root is None:
                print(f"    ⛔ 找不到与 {worktree_root} 配对的仓库根，人工处置")
                continue
            for name, kind in LAYOUT:
                src = repository_root if kind == "repositories" else worktree_root
                line = _move_project(name, project_id, src, target_root / name, args.apply)
                if line:
                    print(line)

        if args.apply:
            repository = target_root / "project-repositories" / project_id
            if repository.is_dir():
                for _, pid, path in recorded:
                    if pid != project_id:
                        continue
                    worktree = target_root / "project-worktrees" / project_id / Path(path).name
                    if not worktree.is_dir():
                        continue
                    result = _run("git", "-C", str(repository), "worktree", "repair", str(worktree))
                    status = "ok" if result.returncode == 0 else f"FAILED: {result.stderr.strip()}"
                    print(f"    worktree repair {worktree.name}: {status}")

    if not args.apply:
        print("\n（dry-run 结束，什么都没动。）")
        return

    mapping = {
        session_id: str(target_root / "project-worktrees" / project_id / Path(path).name)
        for session_id, project_id, path in recorded
    }
    changed = await _rewrite(mapping)
    print(f"\n已改写 {changed} 行 sessions.git_worktree_path")

    missing = [p for p in mapping.values() if not Path(p).is_dir()]
    print(f"复核：{len(mapping) - len(missing)}/{len(mapping)} 个会话的 worktree 落在实处")
    if missing:
        print("⛔ 以下会话搬完仍然查无此处 —— 别启动服务，先查：")
        for path in missing[:20]:
            print(f"    {path}")
        raise SystemExit(1)
    print(f"\n完成。把部署的 PLATFORM_DATA_ROOT 设成 {target_root} 再启动。")


if __name__ == "__main__":
    asyncio.run(main())
