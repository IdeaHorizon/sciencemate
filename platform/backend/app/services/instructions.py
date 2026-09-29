"""指令层：**文件是权威，读的是当下那一份，冻结交给 git。**

## 从三张表到一列，再到没有那一列（RFC X2 → X3）

X2 把 `instruction_documents` / `instruction_versions` / `instruction_snapshots`
三张表砍成会话行上的一列 `instruction_snapshot`（JSON）。方向是对的 —— 指令
是一段文本，它就该是文件。但那次只走了一半：**表删了，抄件留下了**。

留下来的那一份抄件造出了三个互相独立的故障：

1. **界面写的不是 agent 读的。** 平台把项目层抄自
   `<data_root>/projects/<id>/PROJECT.md`，而 harness 读的是**会话 worktree 里
   的 `PROJECT.md`**（Project v2 的权威，`core/directives_loader.py` 显式覆盖）。
   用户在「项目指令」里改完，agent 一个字都读不到，两边都不报错。
2. **个人层不是个人的。** `<data_root>/user/PROFILE.md` 路径里没有 user id，
   一个机构所有人共用一份；而 worker 的 harness home 本来就是
   `<data_root>/state/users/<uid>`，它自己的加载器读的是那底下的
   `user/PROFILE.md` —— 同一个「个人指令」，写一个地方读另一个地方。
3. **那一列可以是 NULL，而消费方是硬 raise。** 041 建列时没有 backfill，
   `execute_local_turn` 缺了就 `RuntimeError`。node20 实测 99 个会话里 90 个
   命中，041 之前建的会话从此一轮都发不出去，用户只看到一句
   「这一轮没能完成」。（2026-09-14 nidy 卡了四天的就是这个。）

三个都是同一个根：**一个问题有两个真相源，而分叉时两边都不报错。**

## 现在

指令层没有平台侧的第二份。平台读写的路径**就是 harness 读它们的那几个**：

  <data_root>/state/users/<uid>/user/PROFILE.md            个人层（用户写）
  <data_root>/state/users/<uid>/user/RESEARCH_SETTINGS.md  个人层（平台写）
  <project repo>/PROJECT.md                                项目层（git 版本化）

- **冻结 = git。** 会话有自己的 worktree 分支，它读到的 `PROJECT.md` 就是那条
  分支上的那一份。别人改主干不会从它脚下抽走，而这正是原来那一列想买的东西。
- **个人层不冻。** 用户改了偏好就该下一轮生效；冻上三个星期不是特性。
- **组织层删掉了。** 三处路径、一个校验器、一段注入、零写入口 —— 它只可能是
  空文件。要做组织下发时连写入口一起设计，别先留一个永远为空的坑。
- **「当时读到的是哪一份」是证据不是判决**：每层的 sha256 随那一轮上报
  （`core.directives_loader.load_directives_for_node` 的 `digests`）。

## 线协议

`instruction_snapshot` 这个字段从请求里消失了，`platform_runtime
.validate_instruction_snapshot` 连同 `_bind_instruction_snapshot`、
`HARNESS_INSTRUCTION_SNAPSHOT_{DIR,SHA256}` 一起删除 —— worker 读文件，
不再需要平台把文本运过去再落一遍盘。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException

from app.config import data_root

#: 个人层两个文件的文件名 —— 与 `core.directives_loader` 里那两个常量同名同义。
#: 这里不 import harness：后端不该为了两个文件名把整个 core 拉进来；名字对不上
#: 时由 `test_instructions_are_files.py` 直接比对两边的常量当场判红。
PROFILE_FILENAME = "PROFILE.md"
RESEARCH_SETTINGS_FILENAME = "RESEARCH_SETTINGS.md"
PROJECT_FILENAME = "PROJECT.md"

MAX_INSTRUCTION_LAYER_BYTES = 200_000


def harness_home_for(user_id: str) -> Path:
    """这个用户的 harness home —— worker 起来时 `HARNESS_FRAMEWORK_HOME` 就是它。

    只在这里算一次。`harness_sessions._paths()` 也调它：那边算出来的是 worker
    真正跑在哪个 home 下，这边算出来的是平台该把指令文件写到哪 —— 这两个必须
    是同一个目录，否则又是"写一处读另一处"。让它们共用一个函数，而不是各抄
    一行 `state_root / "users" / user.id`。
    """
    return data_root("state").resolve() / "users" / str(user_id)


def personal_instruction_dir(user_id: str) -> Path:
    """`core.directives_loader._user_dir()` 在 worker 里算出来的同一个目录。"""
    return harness_home_for(user_id) / "user"


def personal_instruction_path(user_id: str) -> Path:
    return personal_instruction_dir(user_id) / PROFILE_FILENAME


def research_settings_instruction_path(user_id: str) -> Path:
    """个人层里**机器维护**的那半。

    与 `PROFILE.md` 分文件，是因为写者不同：那份是用户写的，这份是平台按结构化
    研究设置渲染的。合成一个文件，平台每次重写都会把用户自己写的话冲掉。
    """
    return personal_instruction_dir(user_id) / RESEARCH_SETTINGS_FILENAME


def publish_identity(user_id: str, *, display_name: str, email: str) -> Path:
    """worker 眼里「我是谁」（`core.identity.current_user_id`）—— 由平台说。

    没人说的时候，harness 按**这台机器**的 `git config user.email` 自己生成一个。平台上那是
    服务器的 git 身份：一台组织服务器上每个人写下的 KB 记录都署同一个名字，按人写的算力授权
    （grants.yaml 的 `<用户 id>` 那一节）也永远对不上任何人（2026-09-24 读出来的）。
    写在 harness 读的那个位置（`<home>/user/identity.json`），同指令文件一个做法。
    """
    import json

    path = personal_instruction_dir(user_id) / "identity.json"
    record = {"user_id": str(user_id), "display_name": display_name or email, "email": email}
    try:
        if json.loads(path.read_text(encoding="utf-8")) == record:
            return path
    except (OSError, ValueError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(".json.writing")
    scratch.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    scratch.replace(path)
    return path


def read_instruction_file(path: Path) -> str:
    """读一个指令文件。不存在 = 空，不是错误。

    这条语义是这一层能保持全函数的原因：没写过指令的用户、041 之前建的会话、
    刚建好还没写 PROJECT.md 的项目，读出来都是空字符串，没有一个走得到 raise。
    """
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def write_instruction_file(path: Path, content: str) -> None:
    if len(content.encode("utf-8")) > MAX_INSTRUCTION_LAYER_BYTES:
        raise HTTPException(status_code=409, detail="Instruction file exceeds the runtime limit")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def publish_research_settings(user_id: str, rendered: str | None) -> Path | None:
    """把渲染好的研究设置落到用户 harness home 里，供 worker 每轮现读。

    幂等：内容没变就不写（别让每一轮都改一次 mtime）。`rendered` 为空表示这个
    用户没有可下发的设置 —— 删掉文件，而不是留一份过期的在那里被读。
    """
    path = research_settings_instruction_path(user_id)
    text = (rendered or "").strip()
    if not text:
        path.unlink(missing_ok=True)
        return None
    body = text + "\n"
    if read_instruction_file(path) == body:
        return path
    write_instruction_file(path, body)
    return path
