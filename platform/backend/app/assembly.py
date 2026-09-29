"""档位装配：同一套代码，personal 与 org 两种起法。

## 规矩（RFC_RESEARCH_BUDDY §3 R2）

**档位只在装配处生效，不在业务代码里。** 业务代码永远不写
`if settings.profile == ...` —— 它只看自己被装配成了什么样：鉴权依赖是谁、
哪些后台任务在跑、数据落在哪个根。谁都能在自己那一层加一句 `if profile`，
而每加一句，两种档位就多分叉一处，且没有任何一层能发现它分叉了。

允许读 `settings.profile` 的只有两个文件：`config.py`（算默认值）和这里
（接线）。`tests/test_profile_is_read_only_in_assembly.py` 机械地守着这条。

## 两种档位的差别，一次说完

============  ==========================  ============================
              personal                    org
============  ==========================  ============================
用户          隐式本机用户，无登录        JWT + 成员关系
数据根        `~/.harness-framework/`     必须显式给绝对路径
数据库        该根下的 db.sqlite          Postgres（显式给）
后台采集      不起（用户点开才采）        起
============  ==========================  ============================
"""
from __future__ import annotations

import importlib.util
import logging
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings
from app.edition import read_edition

logger = logging.getLogger(__name__)

#: 这台 App Server 提供哪些能力。前端按它渲染（WP-11 起）；个人档里组织相关的
#: 一概不出现 —— 那正是「不教育用户」这条要求在机器上的样子。
_ORG_ONLY_CAPABILITIES = frozenset({
    "auth",            # 登录、登出、改密码
    "members",         # 项目成员与角色
    "governance",      # 机构 / 组织治理
    # 曾经还有一条 "publish_review"（发布前的审阅与批准）：声明了一年，全仓零消费者 ——
    # 一个没人画、没人问的能力，只会让读它的人以为机器上有一道并不存在的门。删。
    # 真要做，按 RFC_RESEARCH_BUDDY X1：merge 前的义务检查，不是一项能力。
    "feed_collection", # 服务器侧资讯采集
})

#: `config.Settings.secret_key` 的出厂值。判据是「还是出厂值吗」，所以它要有
#: 名字，不能在两个文件里各写一份同样的字面量。
_DEV_SECRET_KEY = "dev-secret-key-change-in-production"

_BASE_CAPABILITIES = frozenset({
    "projects", "sessions", "artifacts", "knowledge", "memory",
    "compute", "settings", "feed",
})

#: 「连接组织服务器」是**专业版桌面端**的入口（EXEC_PLAN_TWO_EDITIONS §1）。
#: 09-05 时它是个人档唯一允许出现的那一句组织概念；09-16 拍板两种发行之后，
#: 个人版连这一句也不画，入口归专业版。组织服务器自己也不画 —— 它是被连接的那一端。
_PRO_DESKTOP_CAPABILITIES = frozenset({"connections"})


def profile_name() -> str:
    """档位名字 —— **只用来显示**（状态栏里说一句"本机"还是"组织服务器"）。

    界面上任何"要不要画"的判断都必须落在 `capabilities()` 上：名字会变（产品
    还没定名），能力不会。这个函数存在的唯一理由是让别处不必自己去读
    `settings.profile`（那道扫盘闸会拦下来，而它拦得对）。
    """
    return settings.profile


def edition_name() -> str:
    """这份安装是哪种发行（personal / pro）—— 只用来回显与在这里接线。

    发行不是档位：档位说这台服务器装配成了什么形态，发行说这份安装是哪一种。
    业务代码两个都不许读；要按发行分叉，在这里改能力集合。
    """
    return read_edition().name


def update_source() -> str:
    """自更新从哪拿：显式配置 > 包里烧的 > 出厂值。

    专业版的更新在私有仓库里，地址由打包器烧进 `edition.json`；个人版什么都不烧，
    空串交给 `self_update` 落到 `DEFAULT_UPDATE_SOURCE`（公开仓库）。
    """
    return settings.update_source or read_edition().update_source


def capabilities() -> frozenset[str]:
    """本档位 × 本发行提供的能力集合。"""
    features = set(_BASE_CAPABILITIES)
    if settings.profile == "org":
        features |= _ORG_ONLY_CAPABILITIES
    if settings.profile == "personal" and edition_name() == "pro":
        features |= _PRO_DESKTOP_CAPABILITIES
    return frozenset(features)


def prepare_the_data_root() -> None:
    """个人档：把数据根建出来。

    2026-09-05 真机点验抓到的：一台全新机器上 `~/.harness-framework/` 根本不
    存在，于是 SQLite 报 `unable to open database file`，服务起不来 —— 而这条
    路正是"下载、打开、开跑"的第一步。默认值算得出路径不等于那个路径存在。

    只建目录，不写任何内容：一个刚打开还没做任何事的软件不该在磁盘上留下
    状态。删掉这个目录 = 彻底卸载，这条也因此成立。
    """
    import os

    from app.config import data_root

    # 这台机器上只有主人该读得到这些东西：库里有模型凭据，worktree 里有还没
    # 发表的研究。**改 umask 而不是逐个文件 chmod** —— 后者是一份名单，漏掉
    # 的那个默认就是世界可读；而 umask 管住这个进程与它起的每一个子进程
    # （harness worker、沙箱里的模型代码）之后创建的**所有**东西。
    #
    # 2026-09-06 实测：修之前 `~/.harness-framework` 是 drwxr-xr-x、
    # `db.sqlite` 是 -rw-r--r-- —— 同机器上任何别的用户、任何有磁盘访问权的
    # 程序、任何把 home 纳入范围的备份/同步/MDM，拿到的都是可解密的凭据。
    os.umask(0o077)

    root = Path(settings.platform_data_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    for kind in ("repositories", "worktrees", "state", "sockets"):
        data_root(kind).mkdir(parents=True, exist_ok=True)
    # 存量：umask 只管新建的。已经装过的机器上那些目录/文件是宽的，收回来。
    _tighten_what_is_already_there(root)


def _tighten_what_is_already_there(root: Path) -> None:
    """把已经存在的数据根收成私有。

    只收**这一层的目录**与库文件本身：递归遍历一个装满研究数据的目录树，代价
    随项目数增长，而放宽的风险集中在根与库上（子目录是我们自己按 umask 建的）。
    """
    import os

    for path in (root, *(p for p in root.iterdir() if p.is_dir())) if root.is_dir() else ():
        try:
            os.chmod(path, 0o700)
        except OSError:  # 不是我们的东西就不动它
            continue
    for name in ("db.sqlite", "credential.key"):
        target = root / name
        if target.is_file():
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass


def install(app) -> None:
    """按档位接线。启动最早期调用一次，在任何闸之前。

    个人档把 `get_current_user` 这个依赖整个换掉 —— 用 FastAPI 自己的
    `dependency_overrides`，因为那是**这个 app 实例**的属性。用模块级开关的
    版本在测试里立刻现形：一条测试跑过启动路径，同一个 xdist worker 里后面
    每条测试的"当前用户"都变成了本机用户，项目列表于是空了（2026-09-05 实测
    10 条）。
    """
    logger.info("assembling the %s profile", settings.profile)
    edition = read_edition()
    if edition.problem:
        # 文件在但读不懂：按个人版跑（fail-closed），但要留下痕迹 —— 一份专业版
        # 悄悄变回个人版，用户看到的只是"那个入口不见了"，指不到病因。
        logger.warning("edition.json 读不懂，按个人版处理：%s", edition.problem)
    logger.info("edition: %s%s", edition.name,
                f"（更新源 {edition.update_source}）" if edition.update_source else "")
    if settings.profile == "org" and not settings.local_demo_mode \
            and settings.secret_key == _DEV_SECRET_KEY:
        # 组织档才签发 token，所以只有那里的密钥是"必须换掉"的东西。
        # 个人档一个 token 都不签 —— 在那里要求配一个密钥，等于让用户为一件
        # 不存在的事做准备，正是「教育用户」的样子。
        raise RuntimeError(
            "SECRET_KEY must be configured for the org profile "
            "(set SECRET_KEY, or run with LOCAL_DEMO_MODE=true)"
        )
    if settings.profile == "personal":
        from app.auth import get_current_user, implicit_local_user

        prepare_the_data_root()
        app.dependency_overrides[get_current_user] = implicit_local_user
        logger.info(
            "personal profile: implicit local user, data root %s",
            settings.platform_data_root or "(unset)",
        )
    else:
        logger.info("org profile: authentication and memberships are in force")


def uninstall(app) -> None:
    """把 install 装上的东西拆干净。

    对称不是洁癖：`lifespan` 在测试里会被反复进出（`async with lifespan(app)`），
    装上不拆，后面每一条测试的"当前用户"都还是本机用户 —— 2026-09-05 实测，
    这样漏掉的测试有两条，而它们报的错是"项目列表是空的"，指不到病因。
    """
    from app.auth import get_current_user

    app.dependency_overrides.pop(get_current_user, None)


def the_credential_key_is_operator_managed() -> bool:
    """加密凭据的主密钥，是由**运维配置**的，还是这份安装自己的。

    组织档：由 `SECRET_KEY` 派生 —— 那把密钥由操作者管理，且多个进程/多台机器
    必须算出同一把（否则 A 机器存的凭据 B 机器解不开）。这正是"运维管理的密钥"
    该有的样子，而组织档也确实要求它必须换掉出厂值。

    个人档：这台机器自己的一把随机密钥（钥匙串优先，否则 0600 文件）。没有别的
    进程需要跟它算出同一把，也没有运维来配 —— 让用户为一件不存在的事配一个密钥
    就是在教育用户，而用出厂常量等于不加密。

    档位判断留在装配层（R2）：凭据模块只需要知道"这台机器的密钥归谁管"，
    不需要知道自己跑在哪个档位上。
    """
    return settings.profile != "personal"


def weak_resource_walls_are_acceptable() -> bool:
    """跑飞的时候没有自动刹车 —— 这台机器上能不能接受。

    个人档能：那是用户自己的电脑，自己看着，代价只落在自己身上。组织的共享
    执行器不能：那里跑飞会踩到别人。**这不是"个人档标准低"**，不可恢复的三条
    （写边界 / .git / 断网）在两边都不让步（见 `services/unattended.py`）。
    """
    return settings.profile == "personal"


def the_backend_log_is_shared() -> bool:
    """这台后端的日志里有没有别人的事。

    组织服务器上有：很多人的会话走同一台后端，诊断包只能收点名了那个会话或项目的
    记录，别的日志文件一概不收（`services/session_diagnostics.py`）。个人档没有：
    整台机器只有一个人，时间窗内的日志全是他自己的。
    """
    return settings.profile == "org"


def background_collection_enabled() -> bool:
    """资讯采集这类后台任务起不起。

    个人档不起：一个刚装好的软件不该在用户还没说要什么之前就开始爬网。
    功能本身没删 —— 用户点开资讯流时按需采（RFC X8）。
    """
    return settings.profile == "org"


# ── 发行接线 ──────────────────────────────────────────────────────────────


@dataclass
class EditionHooks:
    """发行挂进 lifespan 的钩子。核心在固定的两个时刻调它们：启动闸都过了之后、
    以及起长活任务的时候。个人版两张表都是空的。"""

    after_startup: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    #: 每个返回一个协程（起）或 None（这台机器不起）。
    long_running: list[Callable[[], Coroutine | None]] = field(default_factory=list)


EDITION_HOOKS = EditionHooks()


def wire_the_edition(app) -> None:
    """按发行接线 —— **核心找专业版的唯一一处**。

    专业版住在 `app/pro/`（后端）等几个目录里，公开仓库是删掉它们之后的快照。
    所以核心不能 import 它，只能在这里按名字找：找得到就让它把自己挂上来，找不到
    就是个人版 —— 两种情况下核心一个字都不改。`tests/test_the_core_never_imports_the_pro_edition.py`
    守着「只有这一处」。

    在 app 构造时调一次，且要在别的 `add_middleware` **之前**：专业版的转交中间件
    必须排在最里面（Starlette 按注册倒序包裹）。
    """
    if importlib.util.find_spec("app.pro") is None:
        logger.info("edition wiring: no app.pro package here, core only")
        return
    from app import pro

    pro.wire(app, EDITION_HOOKS, api_prefix=settings.api_v1_prefix)
