"""收尾时，交付物清单由**框架**递到调度器手上。

## 缺口（wangd 2026-08-23）

「你哪怕出了一篇论文、搞定了之后，调度器到最后是不是得给用户一个明确的答复
啊？说你的要求已经做完了、做了什么、怎么做的、输出有哪些，然后把这个 PDF、
这个图片摆出来。现在是没有这一套。」

查下来确实没有。机械的只有**否定门**（`core/closure.py`：没闭环不许说"完成"，
并把 run 降级 incomplete，issue #221）。做完之后要不要汇报、汇报什么、把产物
摆不摆出来 —— 全在 prompt 的自觉里，没有义务项也没有校验。

实测三种失败各不相同：v27 自己写了一份不错的（但产物是纯文本路径，点不开）、
v28 报了 completed 却根本没冻结（"做完"≠"交付了"）、v29 卡死一个字都没有。

## 分工

**机械可判的归框架**：这个项目有哪些冻结交付物、它们在盘上哪个路径、收尾正文
里有没有把它们摆成用户点得开的形状 —— 这三件事框架全都数得出来，不该让模型
去回忆或猜（[[框架在制造症状]]）。

**语义判断归模型**：汇报怎么写、哪些值得强调、结论如何措辞 —— 框架不碰。

所以本模块只做一件事：把清单**递到手上**，和 PR#634「审查意见机械送达决策方」
同一条路子 —— 送达不设门，决定权仍在模型。

## 为什么路径必须是工作区相对的

对话里的 `![](路径)` / `[](路径)` 只对**工作区相对路径**放行
（`platform/frontend/.../workspace-target.ts`）：绝对路径、外部 URL、协议相对
URL 一律退回成纯文本。那不是格式讲究，是零点击外泄的防线。

而真产物里恰恰有绝对路径 —— 真样本：frozen manuscript 的
`metadata.pdf_path` 是
`/Users/…/project_worktrees/<pid>/<sid>/writing/latex_build/…/main.pdf`。
直接抄进正文，渲染层会拒，用户看到的还是一串点不开的字符。所以这里**一律
相对化**，落在工作区外的直接丢弃。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


#: 一次最多列多少件。清单是给人看的收尾说明，不是账本导出。
MAX_LISTED = 12

#: 每件产物最多带几个伴随文件（PDF / 图 / bundle）。
MAX_COMPANIONS = 6


#: 收集阶段的硬上限（防 metadata 异常膨胀）。排序截断用的是 MAX_COMPANIONS。
_COLLECT_CEILING = 64

#: 伴随文件按「对读者的可呈现性」排序，**不是**按遍历顺序。
#:
#: 2026-08-23 真产物实测（RW_FPT manuscript v4）：按遍历顺序取前 6，选出来的是
#: main.pdf / main_clean.pdf / bundle.zip / main.tex / frontmatter.tex /
#: abstract.tex —— 而两张真正该摆给用户看的图
#: （fig1_tail_loglog.png / fig2_mean_truncation.png）被 tex 碎片挤掉了。
#: 拿 dict 迭代顺序当重要性，和当初 orientation 拿字母序当重要性是同一个错。
#:
#: 排序而不是**排除**：`.tex` 不该硬拦（有人就是想看源码），只是排在后面。
#: 名单式排除会漏新格式，排序不会 —— 未知扩展名落在最后一档，仍然可达。
_PRESENTABILITY = (
    (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"),   # 能直接画进对话
    (".pdf",),                                            # 点开就能读
    (".zip", ".tar", ".gz", ".csv", ".xlsx"),             # 拿得走的成品
)


def _presentability_rank(path: str) -> tuple[int, str]:
    suffix = Path(path).suffix.lower()
    for rank, group in enumerate(_PRESENTABILITY):
        if suffix in group:
            return (rank, path)
    return (len(_PRESENTABILITY), path)


@dataclass
class Deliverable:
    """一件冻结交付物 + 它自己 metadata 里指到的、能渲染的伴随文件。"""
    artifact_id: str
    version: int
    path: str                     # 工作区相对
    node: str
    frozen_at: str
    companions: list[str] = field(default_factory=list)   # 工作区相对

    @property
    def all_paths(self) -> list[str]:
        return [self.path, *self.companions]


def _relative_to_workspace(raw: str, workspace: Path,
                          bases: tuple[Path, ...] = ()) -> str | None:
    """把任意路径字符串归一成工作区相对路径；落在工作区外 → None。

    ## 三种真实形状都要能收（2026-08-23 实测，不是推演）

        绝对         metadata.pdf_path = "/Users/…/writing/latex_build/…/main.pdf"
        工作区相对   账本里的 path = "paper/manuscript__x.tex"
        **节点相对** clean_results 的 figure = "runtime/clean/fig1_tail_loglog.png"

    第三种是最容易漏的一种：产物记的路径相对于**它自己所在的节点目录**
    （这里是 `experiments/`）。只按工作区根解析，那两张真正该摆给用户看的图
    就永远找不到 —— 而"把图片摆出来"正好是这件事的一半。

    `bases` 是除工作区根之外还要试的基准目录，按顺序取第一个命中的。
    """
    value = (raw or "").strip()
    if not value or "\n" in value:
        return None
    candidate = Path(value)
    root = workspace.resolve()
    if candidate.is_absolute():
        tries = [candidate]
    else:
        tries = [workspace / candidate] + [base / candidate for base in bases]
    for attempt in tries:
        try:
            rel = attempt.resolve().relative_to(root)
        except (ValueError, OSError):
            continue
        text = rel.as_posix()
        # 平台记账目录不是给人看的交付物
        if not text or text.startswith(".git/") or text.startswith(".research/"):
            return None
        # `exists()` 不是无害的：它是一次真的 `stat()`，可以按 errno 抛出来
        # （#972 的现场是 ENAMETOOLONG）。上面那个 try 只包住了 resolve，
        # 这一句露在外面 —— 一个坏候选就能把整个项目的产出页打成 500。
        # 探到底是什么错不重要，重要的是**探不动就当没命中**，别往上抛。
        try:
            hit = attempt.exists()
        except OSError:
            continue
        if hit or not bases:
            # 有 bases 时要求"真的存在"才算命中，否则第一个基准会无条件吞掉
            # 后面的候选（工作区根下同名文件不存在，却仍返回一个死路径）。
            return text
    return None


#: 一个字符串里可能藏着路径的片段。
#:
#: 2026-08-23 Buffon 课题实测：图**确实**记在冻结产物的 metadata 里，但记成了
#: 带说明的句子，整串不是路径 ——
#:
#:     "runtime/out/convergence.png (log-log, 9 点 chi2 CI 误差线, OLS 线, …)"
#:     "Figure 1: convergence.png (300dpi, Chinese labels)"
#:
#: 只把**整个字符串**当路径试，这个课题的唯一一张图就一件都找不到。而"把图片
#: 摆出来"正好是用户要的一半。两个真项目两种写法，说明 metadata 里的路径本来
#: 就没有统一格式 —— 判据得能从散文里把路径捞出来。
#:
#: 误报不用怕：捞出来之后还要过"**这个文件在工作区里真的存在**"这一关。
#: 捞错的字符串命中不了真文件；碰巧命中的，那就是一个真文件。
_PATH_IN_PROSE = __import__("re").compile(r"[\w][\w./\-]*\.[A-Za-z0-9]{1,6}")

#: 一个字符串里最多试几个候选（防病态长文本把扫描拖垮）。
_MAX_CANDIDATES_PER_STRING = 8

#: 一段路径分量的字节上限。POSIX `NAME_MAX` 在 Linux/macOS 上都是 255；
#: 超过这个长度的字符串**不可能**是盘上任何一个文件名，连试都不该试。
#:
#: 这不是性能优化，是 #972：`_relative_to_workspace` 会把候选拼到工作区上
#: 调 `stat()`，而一整段中文业务 summary 当候选时，Linux 上直接
#: `OSError: [Errno 36] File name too long` —— 于是一个坏字段把整个项目的
#: `/catalog` 打成 500。**注意这条闸在 macOS 上看不出效果**：本机 Python 的
#: `Path.exists()` 把这个 errno 吞掉返回 False，现场是 Linux 后端才炸。
#: 所以判据不能落在"会不会抛"上，要落在"这个字符串有没有被交出去 stat"上。
_NAME_MAX_BYTES = 255


def _is_statable(candidate: str) -> bool:
    """这个字符串有没有资格被拿去 stat —— 任何一段超过 NAME_MAX 就没有。

    比长度用**字节**不用字符：NAME_MAX 是字节数，一个中文字 UTF-8 占 3 字节，
    按字符比会把 85 个汉字以上的串放过去（那正是 #972 的现场）。
    """
    for part in candidate.replace("\\", "/").split("/"):
        if len(part.encode("utf-8", "surrogatepass")) > _NAME_MAX_BYTES:
            return False
    return True


def _path_candidates(value: str) -> list[str]:
    """从一个字符串里取出所有可能是路径的写法，整串优先。

    **仍然是扫值，不是查字段名单**（见 `_companions_of` 的说明）：写死
    `("pdf_path", "figure_paths", …)` 就是名单式护栏，新加的键默认漏过。
    这里只加了一道机械闸 —— 不可能是文件名的字符串不交出去 stat。
    """
    text = (value or "").strip()
    if not text:
        return []
    out = [text] if _is_statable(text) else []
    if len(text) < 4096:
        for match in _PATH_IN_PROSE.finditer(text):
            token = match.group(0)
            if token != text and token not in out and _is_statable(token):
                out.append(token)
                if len(out) > _MAX_CANDIDATES_PER_STRING:
                    break
    return out


def _companions_of(record_path: Path, metadata: dict | None, workspace: Path) -> list[str]:
    """产物 metadata 里指到的、**盘上真的存在**的文件。

    判据是"扫值"，不是"查一份键名单"：写死 `("pdf_path", "figure_paths", …)`
    就是名单式护栏，新加的键默认漏过而且没人会发现
    （[[护栏要扫盘，不要写名单]]）。这里只问一件事 ——
    **这个字符串是不是一个落在工作区里、盘上存在的文件**。
    """
    found: list[str] = []
    seen: set[str] = set()
    # 产物记的相对路径可能以**它自己的节点目录**为基准（真实形状，见
    # `_relative_to_workspace` 的说明）。记录正文就在节点目录下，所以节点目录
    # 是它的父目录。
    node_dir = Path(record_path).parent

    def walk(node: object, depth: int = 0) -> None:
        # 这里**不能**按 MAX_COMPANIONS 提前收手：先收齐再排序再截断。
        # 按遍历顺序截断 = 谁先被 dict 迭代到谁入选，与"对读者有多大用"无关。
        if len(found) >= _COLLECT_CEILING or depth > 6:
            return
        if isinstance(node, str):
            for candidate in _path_candidates(node):
                rel = _relative_to_workspace(candidate, workspace, bases=(node_dir,))
                if rel and rel not in seen and (workspace / rel).is_file():
                    seen.add(rel)
                    found.append(rel)
        elif isinstance(node, dict):
            for value in node.values():
                walk(value, depth + 1)
        elif isinstance(node, (list, tuple)):
            for value in node:
                walk(value, depth + 1)

    walk(metadata if isinstance(metadata, dict) else {})
    found.sort(key=_presentability_rank)
    return found[:MAX_COMPANIONS]


def frozen_deliverables(workspace: Path | None) -> list[Deliverable]:
    """账本里全部冻结过的身份，每个取**最近一次**冻结的那一版。

    修订过的（冻结版之后又存了草稿）仍然列：可点入口是盘上那个文件（head），
    版本号报冻结的那一版 —— 交付物是冻结版，草稿只是它旁边的工作。
    """
    if workspace is None or not workspace.is_dir():
        return []
    from core.ledger import workspace_store

    store = workspace_store(workspace)
    out: list[Deliverable] = []
    for head in store.heads().values():
        if not head.frozen_version:
            continue
        rel = _relative_to_workspace(head.path, workspace)
        if not rel:
            continue
        target = workspace / rel
        if not target.is_file():
            # 账本里有、盘上没有：这不是交付物，说出来只会让模型去引一个 404
            continue
        item = Deliverable(
            artifact_id=head.artifact_id,
            version=head.frozen_version,
            path=rel,
            node=head.produced_by_node_type,
            frozen_at=head.frozen_at,
        )
        item.companions = [p for p in _companions_of(target, head.metadata, workspace) if p != rel]
        out.append(item)
    out.sort(key=lambda d: (d.frozen_at, d.artifact_id))
    return out


def _markdown_targets(text: str) -> set[str]:
    """正文里 `[..](target)` / `![..](target)` 的全部 target。

    只认这一种形状是**故意**的：这正是渲染层会当成文件处理的那一种。
    路径被当普通文字提了一嘴（v27 的收尾汇报就是这样）不算"摆出来了"——
    用户点不开它（[[断言文案还在≈什么都没断言]]）。
    """
    return {m.group(1).strip() for m in re.finditer(r"!?\[[^\]]*\]\(([^)\s]+)\)", text or "")}


#: 用户点不开的前缀：run 的运行时目录（草稿、scratch、transcript）。
#: 判据只认这一个前缀 —— `.research/` 下别的东西（账本、会话记账）也不是给
#: 用户看的，但模型不会把它们摆进对话；真出现过的是 scratch 里的图。
_UNPRESENTABLE_PREFIXES = (".research/",)


def unpresentable_targets(text: str) -> list[str]:
    """正文里摆成可点形状、但落在运行时目录里的路径 —— 用户点开是 404 或废纸。

    调度器的草稿落 run 目录 `scratch/`（它没有私人抽屉），要给用户看的东西必须
    先入档：写进 `project/notes/…`、或 `save_artifact`。呈现即入档不是文案，
    是这里的判据：这些路径出现在交出去的正文里，收尾闸就把话递回去。
    """
    return sorted(
        target for target in _markdown_targets(text)
        if target.startswith(_UNPRESENTABLE_PREFIXES)
    )


def render_unpresentable(targets: list[str]) -> str:
    lines = "\n".join(f"  · {target}" for target in targets)
    return (
        "📎 呈现即入档：你刚才摆进正文的这些路径在 run 的运行时目录里，用户点不开，"
        "而且随这个 run 一起消失：\n\n"
        + lines
        + "\n\n先把它们放进研究记录再摆出来：\n"
        "  · 文件（图 / PDF / 表）：`write_file` 或 `run_bash cp` 到 "
        "`project/notes/<文件名>`，正文里写 `notes/<文件名>`\n"
        "  · 文字结论：`save_artifact`，或写 `project/notes/<名字>.md`\n"
        "草稿留在 scratch 里没问题 —— 只有给用户看的那几件要入档。"
    )


def unpresented(deliverables: list[Deliverable], text: str) -> list[Deliverable]:
    """哪些交付物在这段正文里**没有**被摆成可点的形状。

    一件产物只要它自己或它的任一伴随文件被摆出来了，就算交代过了 —— 论文的
    可点入口是 PDF，不是那份 json；要求两个都摆是无意义的形式主义。
    """
    targets = _markdown_targets(text)
    return [d for d in deliverables if not (targets & set(d.all_paths))]


def render(missing: list[Deliverable], *, total: int) -> str:
    """递给模型的清单。全部来自扫盘，一个字不是模型自报的。"""
    lines: list[str] = []
    for item in missing[:MAX_LISTED]:
        head = f"  · {item.artifact_id} (v{item.version}"
        if item.node:
            head += f"，{item.node} 签的"
        lines.append(head + ")")
        for path in [item.path, *item.companions]:
            lines.append(f"      {path}")
    more = len(missing) - MAX_LISTED
    if more > 0:
        lines.append(f"  · …另有 {more} 件未列出（清单有上限，不是它们不存在）")
    return (
        f"📦 收尾对账：本项目有 {total} 件**已冻结**的交付物，其中 "
        f"{len(missing)} 件你刚才那段收尾汇报里没有摆出来。\n\n"
        + "\n".join(lines)
        + "\n\n"
        "路径是框架扫盘现算的，可以直接用。把该给用户看的那几件摆进正文：\n"
        "  · 图片：`![说明](路径)` —— 直接画在对话里\n"
        "  · PDF / 其它文件：`[说明](路径)` —— 用户点开进右栏\n"
        "只收**工作区相对路径**（上面这些就是）；绝对路径和外部 URL 会被渲染层\n"
        "拒掉，写了等于没写。\n\n"
        "顺带把这次交付说清楚：用户当初要的是什么、你做了什么、怎么做的、\n"
        "结论是什么、哪些**没**做到。\n\n"
        "⚠️ 这不是门禁：要不要摆、摆哪几件、怎么写，你自己定 —— 有的产物\n"
        "（中间态的 prereg、给审稿看的草稿）本来就不该往用户面前推。框架只负责\n"
        "让你**看得见有这些东西**，不让「忘了」和「不打算给」长成一个样子。"
    )
