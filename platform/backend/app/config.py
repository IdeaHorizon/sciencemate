"""Application configuration via environment variables."""

import os
import re
import sys
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings


def _default_data_root() -> Path:
    """见 `app.data_root_default.default_data_root` —— 规则只写在那一处。

    这里留一个同名薄壳：config 自己的 validator 和钉住「与 harness 逐字一致」的
    那条测试都叫这个名字。launcher 在导入 config 之前需要同一个答案，所以本体
    搬去了一个零依赖的模块。
    """
    from app.data_root_default import default_data_root

    return default_data_root()


#: 组织档的库。个人档在下面的 validator 里换成数据根里的 SQLite —— 判据是
#: 「这个值还是默认值吗」，所以它必须有名字，不能写成两处一样的字面量。
_SERVER_DATABASE_URL_DEFAULT = "postgresql+asyncpg://postgres:postgres@localhost:5432/research_platform"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    #: 这台 App Server 的起法。**只有 config.py 与 assembly.py 读它**
    #: （RFC_RESEARCH_BUDDY R2：档位只在装配处生效）。
    #:
    #: personal —— 个人电脑上的一份安装：隐式单用户、SQLite、数据落
    #:   `~/.harness-framework/`。下载、打开、填模型、开跑，什么都不用先配。
    #: org —— 一台常驻服务器：鉴权、成员、Postgres，四个根必须显式给绝对路径
    #:   （08-21 丢过 43 个会话，见 `data_root()`）。
    profile: Literal["personal", "org"] = Field(
        default="personal",
        # 环境变量名字叫 `PLATFORM_PROFILE` —— 文档、RFC、派工单里写的都是它，
        # 而 2026-09-05 验收脚本实测：代码这边只认裸 `PROFILE`，于是照文档配
        # `PLATFORM_PROFILE=org` 起一台组织服务器，起来的是**个人档**：隐式本
        # 机用户、零鉴权、谁连上都是"本人"。配错的代价往开放的方向倒，而且
        # 一声不吭。
        #
        # 裸 `PROFILE` 一并留着：现网可能有人正用它，而这条路上「名字换了」
        # 的失败长得跟上面那个一模一样。
        validation_alias=AliasChoices("PLATFORM_PROFILE", "PROFILE"),
    )

    # Application
    app_name: str = "Research Platform"
    debug: bool = False
    local_demo_mode: bool = False

    #: 静态导出的 UI 目录（`platform/frontend/out`）。给了就由 App Server 自己
    #: serve —— 个人档运行期没有 Node，也没有 nginx。留空 = 不挂载（组织档由
    #: nginx 同源提供 UI，那条路一字不动）。
    static_ui_root: str = ""
    api_v1_prefix: str = "/api/v1"
    # 浏览器能从哪些 origin 调这个 API。逗号分隔，生产部署必须显式给。
    # 留空 + local_demo_mode=true 时放行**任意 loopback 端口**（见 main.py）：
    # 自部署文档明写"按需改端口"，而写死名单意味着改了端口就 CORS 400，
    # 前端只能显示一句没有信息量的 "Failed to fetch"。
    cors_allow_origins: str = ""
    #: 组织服务器上谁能注册。`invite` = 要邀请码；`open` = 谁都能建号。
    #:
    #: **组织档的默认是 `invite`**。此前没有这个开关，`/register` 一律开放：任何能
    #: 连到这台服务器的人都能建号，而没有任何路径能改一个人的角色 —— 门开着，管门
    #: 的人不存在。个人档没有注册这回事，这个值在那里没有意义。
    registration_mode: Literal["invite", "open"] = "invite"
    #: 这台服务器允许哪几种登录方式（逗号分隔，见 services/sign_in.py）。
    #:
    #: 今天门后只有 `password` 一种。留这个配置是为了让"接单位已有的账号系统"
    #: （LDAP / 企业微信 / 学校 OAuth）成为**加一种实现 + 改一行配置**，而不是
    #: 动登录端点 —— 院所真部署时第一句话就是这个要求。
    #:
    #: 认不出来的名字忽略；一种都不剩时退回 password（锁死所有人不是更安全，
    #: 是更糟）。
    sign_in_methods: str = "password"
    runtime_tenant_id: str = "local-tenant"
    harness_bridge_enabled: bool = False
    harness_root: str = ""
    harness_python: str = ""
    harness_state_root: str = ""
    #: 一台部署上**所有持久科研数据**的根。Project 的 Git 仓库、Session
    #: worktree、harness 运行态、worker 命令面 socket 全部由它派生（见文件末尾
    #: `data_root()`）。必须绝对路径，且必须显式给 —— 没有默认值。
    platform_data_root: str = ""
    # Deadline for short Harness control-plane RPCs (initialization and
    # termination). Scientific turns deliberately have no wall-clock timeout;
    # they are durable Runs stopped only by an explicit terminal condition.
    harness_timeout_seconds: int = 900
    # Git-native Project repositories.  Project content is authoritative here;
    # SQL tables are query/control-plane projections.
    # worker 命令面（RFC 异步运行时 P0-2）。走 socket 时后端可以重启/崩溃而
    # 不带走正在跑的研究；关掉则退回 stdio 管道（老行为，worker 随后端死）。
    # 默认开：一个默认关着的开关就是"机制存在但没接到路径"。
    harness_control_socket: bool = True
    #: 自更新从哪拉。空 = 内置默认（Forgejo 仓库的最新 release）。可以是一个
    #: 直接指向 manifest.json 的 URL，或一个 Forgejo/Gitea 仓库网址。
    update_source: str = ""
    #: 启动与每天一次去看有没有新版本。关掉 = 只在用户手动点「检查更新」时看。
    update_check_enabled: bool = True
    #: 更新源是私有 Forgejo 仓库时的凭据（`user:token` 或裸 token，只读即可）。
    #: 空 = 匿名。release 资产与仓库同一个可见性 —— 私有仓库不带它就是 404。
    update_source_token: str = ""
    #: socket 放哪。AF_UNIX 路径有硬上限，所以要短根 —— 太深时 worker 侧
    #: 会自己回退到临时目录，真实地址以注册表行的 control_socket 为准。
    #
    # ⚠️ 下面四个根**没有默认值**，这是刻意的。见本文件末尾 `data_root()`。
    harness_socket_root: str = ""
    project_repository_root: str = ""
    project_worktree_root: str = ""
    project_git_executable: str = "git"
    project_git_max_blob_bytes: int = 10_000_000
    #: 一次 checkpoint 里**一个目录**能进 Git 的字节上限。单文件上限看不见"几千个
    #: 各自不大的文件"这种形态（解开的数据集），而它一旦进了会话分支，每一次
    #: "这个会话改了什么"都要跟它成正比。超了整目录排除并上报，数据走材料池。
    project_git_max_tree_bytes: int = 100_000_000
    #: 单份**用户交来的文件**的上限。它跟 `project_git_max_blob_bytes` 是两个
    #: 问题：那个管"多大的 blob 能进 Git"，这个管"用户能交多大的文件"。用户
    #: 文件的字节从不进 Git（内容寻址池 + `.ref` 指针，见 `core/materials`），
    #: 所以它不受 Git 的 blob 规矩约束，只受这台部署的磁盘约束。
    #: 老实现在这里有三个互不相干的数（附件 64 MiB / 材料 10 MB / checkpoint
    #: 静默剔除），于是一个 800MB 的日志包三条路都进不去、且只有一条会报错。
    material_max_bytes: int = 2_147_483_648
    #: 一次预览最多把多大的文件送进浏览器。刻意大于 `project_git_max_blob_bytes`
    #: （那个管的是"能不能进 checkpoint"）：未跟踪的中间产物同样要能打开看，
    #: 而 latex 出的带图论文 PDF 十几 MB 很常见。
    project_file_preview_max_bytes: int = 25_000_000
    #: Deployment-owned capability registry for external dataset/storage mounts.
    #: Project records may name one of these keys through ``workspace_binding``;
    #: they can never supply or widen a host path themselves. Environment form:
    #: SANDBOX_MOUNT_BINDINGS='{"md17":{"path":"/srv/datasets/md17","mode":"ro"}}'
    sandbox_mount_bindings: dict[str, dict[str, str]] = {}
    #: 科研资讯流的采集调度。默认开 —— 一个默认关着的开关就是"机制存在但
    #: 没接到路径"（同 `harness_control_socket` 的理由）。关掉它，资讯流仍
    #: 可读，只是不再有新内容进来。
    feed_collector_enabled: bool = True
    #: 出网抓取。**离线/内网部署把它关掉**：外部源一个都不拉，资讯流退化成
    #: 组织内部动态 + 已有内容，而不是每轮拉取都超时然后把源全熔断掉。
    feed_external_fetch_enabled: bool = True
    literature_harvester_enabled: bool = True
    # 后台仅刷新用户实际订阅的学科，不再遍历完整二级学科目录。
    literature_harvester_offline_enabled: bool = True
    # 文献增量采集与缺失资产补齐每七天执行一轮。
    literature_harvester_interval_seconds: int = 604800
    # 调度器多快检查一次“有没有订阅期刊满7天”。检查不等于发远程请求；
    # 小间隔避免用户在两次整周 tick 之间新订阅后要等到第8—14天。
    literature_harvester_check_interval_seconds: int = 3600
    literature_harvester_max_queries_per_round: int = 100
    literature_harvester_batch_pause_seconds: int = 60
    literature_harvester_max_per_source: int = 20
    literature_asset_completion_max_per_round: int = 20
    literature_asset_completion_budget_seconds: int = 600
    # 小红书后台只接收最近 N 天的期刊文章；E2E literature 不受此配置影响。
    literature_harvester_recent_days: int = 7
    literature_harvester_seed_queries: str = ""
    # Database
    database_url: str = _SERVER_DATABASE_URL_DEFAULT
    database_echo: bool = False

    # LLM providers
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    deepseek_api_key: str = ""
    kimi_api_key: str = ""
    glm_api_key: str = ""
    glm_base_url: str = "http://maas.icompify.com:32788/v1"
    default_llm_provider: str = "glm"
    default_llm_model: str = "glm-5.1"

    # Per-harness model routing — maps node_type to preferred model.
    # Falls back to default_llm_model if node_type not listed.
    # Override via env: HARNESS_MODEL_MAP='{"survey":"deepseek-v4-flash",...}'
    harness_model_map: dict[str, str] = {
        "exploration": "deepseek-v4-flash",
        "survey": "deepseek-v4-flash",
        "planning": "deepseek-v4-pro",
        "experiment": "deepseek-v4-pro",
        "data_process": "deepseek-v4-flash",
        "analysis": "deepseek-v4-pro",
        "writing": "deepseek-v4-pro",
        "review": "deepseek-v4-pro",
        "project_chat": "deepseek-v4-flash",
        "global_chat": "deepseek-v4-flash",
        "checkpoint": "deepseek-v4-pro",
        "intake": "deepseek-v4-flash",
    }
    # Per-harness provider routing — maps node_type to provider name.
    # Falls back to default_llm_provider if not listed.
    harness_provider_map: dict[str, str] = {}

    # Auth
    secret_key: str = "dev-secret-key-change-in-production"
    access_token_expire_minutes: int = 1440  # 24 hours
    #: 凭据被**保管**起来时活多久（天）。
    #:
    #: 24 小时是"一个浏览器标签页"的尺度。桌面握着的那份不是标签页：它加密躺在
    #: 这台机器的钥匙串里，代表的是"我在这个组织里有个账号"这件长期事实。按 24
    #: 小时算，用户每天开机都要重新登录一次 —— 而他从没说过要退出。
    #:
    #: 撤销不靠过期：登出会把那一张记进撤销表，改密码会让**每一张**旧的当场作废
    #: （`auth.credential_fingerprint`）。所以放长的代价是"被偷走的那张多活一阵"，
    #: 不是"收不回来"。
    held_credential_expire_days: int = 180

    # Memory system (Hermes-inspired capacity limits)
    memory_project_soft_limit: int = 500
    memory_user_soft_limit: int = 100
    memory_consolidation_threshold: float = 0.8  # trigger at 80%

    # Budget defaults
    default_project_llm_token_budget: int = 10_000_000

    # 新注册用户归属的机构/组。模型后端、指令文档等按 scope 可见 ——
    # 用户不属于任何机构就看不到**任何**后端、连 session 都开不了
    # （node20 多用户实测：新账号 model-backends 返回 []，创建 session
    # 报 "No ready model backend is available"）。默认与部署的 seed 数据
    # 对齐；多租户部署按需覆盖。
    default_institution_id: str = "ieit"

    # `populate_by_name`：上面 `profile` 有了 validation_alias 之后，
    # `Settings(profile="org")` 这种按字段名构造的写法就不再默认成立（测试里有
    # 六处这么写）。别名是给环境变量的，不是给调用方的。
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "populate_by_name": True}

    def get_model_for_harness(self, node_type: str) -> str:
        """Resolve the LLM model for a given node/harness type."""
        return self.harness_model_map.get(node_type, self.default_llm_model)

    def get_provider_for_harness(self, node_type: str) -> str | None:
        """Resolve the LLM provider for a given node/harness type.

        Returns None to use the default provider.
        """
        return self.harness_provider_map.get(node_type) or None



    @model_validator(mode="after")
    def _fill_in_the_personal_defaults(self) -> "Settings":
        """个人档把「数据在哪、库在哪」算出来；组织档一个都不猜。

        为什么这段长在 config.py 而不是 assembly.py：这两个值在**任何**代码跑
        起来之前就得有答案（`data_root()` 在导入期就可能被问到）。装配层来不及。

        为什么组织档仍然必须显式给：08-21 那次 43 个会话丢失，正是因为一个能
        悄悄改变「数据在哪」的默认值 —— 相对路径跟着进程的 cwd 走，每次部署翻
        软链就换一个位置，而每一层拿到的都是一个"合法"的路径。个人档没有这个
        问题：它的根是用户 home 下一个固定目录，不随部署改变。
        """
        if self.profile != "personal":
            return self
        if not self.platform_data_root:
            import os

            self.platform_data_root = os.environ.get(
                "HARNESS_FRAMEWORK_HOME", str(_default_data_root())
            )
        if self.database_url == _SERVER_DATABASE_URL_DEFAULT:
            root = Path(self.platform_data_root).expanduser()
            self.database_url = f"sqlite+aiosqlite:///{root / 'db.sqlite'}"
        return self

settings = Settings()


#: harness 认这个环境变量当它的状态根（`core/paths.py`）。平台与 harness 的
#: 数据根是**同一个目录** —— 这个身份在这里声明一次，由 `publish_the_data_root`
#: 交出去，别的地方一律不再自己算一遍。
HARNESS_HOME_VARIABLE = "HARNESS_FRAMEWORK_HOME"

#: harness 用它单独覆盖 org 层的位置（`core/paths.org_root()`）。
HARNESS_ORG_HOME_VARIABLE = "HARNESS_FRAMEWORK_ORG_HOME"


def the_data_root() -> Path:
    """这台安装的数据根 —— 「数据在哪」只在这里回答。

    `data_root(kind)` 回答的是根**底下**的四类布局；这个函数回答根本身。
    没有它的时候，问「根在哪」的四处代码各自抄了一遍
    ``os.environ.get("HARNESS_FRAMEWORK_HOME", "~/.harness-framework")``：
    launcher 的开机横幅、指令文件、文献投影、以及 config 自己。抄件之间只在
    默认路径下碰巧一致 —— 2026-09-07 实测 `PLATFORM_DATA_ROOT=/tmp/fresh-afs`
    起一个实例，横幅印的是 `~/.harness-framework`，而库真的建在 `/tmp`。

    显示层印错还只是丢人。真正的代价是 harness：它整个状态根都取
    `HARNESS_FRAMEWORK_HOME`，于是后端把项目写进 A、worker 把 KB/记忆/运行态
    写进 B，两边都不报错 —— 那正是 08-21 丢掉 43 个会话的形状。
    """
    if not settings.platform_data_root:
        raise DataRootError(
            "PLATFORM_DATA_ROOT 没有配置。它是这台部署上所有持久科研数据的根，"
            "必须显式给一个绝对路径。"
        )
    root = Path(settings.platform_data_root).expanduser()
    if not root.is_absolute():
        raise DataRootError(
            f"PLATFORM_DATA_ROOT={settings.platform_data_root!r} 是相对路径。"
            "持久科研数据的位置不能取决于进程的工作目录。"
        )
    return root


def publish_the_data_root() -> Path:
    """把同一个答案交给 harness，并且不许两边分叉。

    进程启动时环境里已经有一个不一致的 `HARNESS_FRAMEWORK_HOME`，说明有人分别
    配了两个变量而它们指着不同地方。这时候**起不来**比"挑一个用"好得多：挑一个
    就等于把另一半数据静默地写去别处，而分叉是事后才看得见的。
    """
    root = the_data_root()
    existing = os.environ.get(HARNESS_HOME_VARIABLE, "").strip()
    if existing and Path(existing).expanduser().resolve() != root.resolve():
        raise DataRootError(
            f"{HARNESS_HOME_VARIABLE}={existing!r} 和 PLATFORM_DATA_ROOT={str(root)!r} "
            "指着两个不同的目录。它们是同一个数据根的两个名字：后端会把项目写进"
            "后者，harness worker 会把知识库/记忆/运行态写进前者，两边都不会报错。"
            "把两个变量指到同一个目录，或者只配一个。"
        )
    os.environ[HARNESS_HOME_VARIABLE] = str(root)
    if each_organisation_keeps_its_own_knowledge():
        # 组织档上没有「这台安装的那一个」org 层 —— 每个组织一个（`the_org_home`）。
        # 每个子进程由起它的人按**那一次**是哪个组织显式给；进程环境里不留一个可以
        # 被继承的，否则漏给的那一处会悄悄读写别的组织的知识。
        os.environ.pop(HARNESS_ORG_HOME_VARIABLE, None)
    else:
        os.environ[HARNESS_ORG_HOME_VARIABLE] = str(the_org_home())
    return root


def each_organisation_keeps_its_own_knowledge() -> bool:
    """组织档：一台服务器上好几个组织，各一份 org 层。个人档：整台一份。

    别处要按这个分叉（比如合并旧目录时合到哪），问这里，不自己读档位名字。
    """
    return settings.profile == "org"


#: 组织 id 当目录名用。服务器发的是 `secrets.token_urlsafe`（字母数字 `-_`）；
#: 别的一律不认 —— 它会被拼进路径。
_AN_ORGANISATION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def the_org_home(institution_id: str | None = None) -> Path:
    """org 层在哪 —— **一个组织一个**；个人档那台机器就是一个组织。

    ## 组织档：`<数据根>/organisations/<组织 id>/`（2026-09-24）

    一台组织服务器上住着好几个组织（`User.institution_id` 隔开名录、项目、模型）。
    org 层从前是「这台安装一个」—— 于是 A 组织晋升上去的结论，B 组织的项目开题时
    就读到了；B 的管理员在「组织知识」里看到的是 A 的东西。隔离做到了人和项目，
    知识这一层没有跟上。所以组织档上必须说是**哪个组织**，不给就拒 —— 猜一个等于
    把一个组织的知识写进另一个组织。

    个人档还是 `<数据根>/org`：那台机器只有一个人，也就只有一个组织。

    ## 它为什么必须由这里回答（09-16）

    `core.paths.org_root()` 的默认值是 `<harness home>/org`。而平台给**每个用户
    一个 harness home**（`state/users/<uid>`，指令文件和 worker 状态都住那儿），
    于是默认值算出来的是 `state/users/<uid>/org`：**每个人一个私有的 org 层**。

    2026-09-16 在 node20（唯一跑组织档的真部署）上实测：12 个用户、12 个私有
    `org/` 目录，16 条晋升上来的 claim 散在其中 3 个人的目录里，互相看不见 ——
    「组织级知识」这件事从来没发生过一次。晋升、正典、效果传感器整套机制一直在
    各自的私有目录里空转。

    个人档同样受害，只是不显形：命令行 harness 读 `~/.harness-framework/org`，
    平台 worker 读 `~/.harness-framework/state/users/<uid>/org` —— **同一台机器上
    两个 org 层**，谁也不知道另一个存在。

    所以答案不能是"从 home 推"，必须由这里显式说出来：org 层是**组织**的属性，
    不是某个用户的属性。跨项目是它的本分，跨人是组织档的全部意义。

    ## 它为什么必须由这里回答

    `core.paths.org_root()` 的默认值是 `<harness home>/org`。而平台给**每个用户
    一个 harness home**（`state/users/<uid>`，指令文件和 worker 状态都住那儿），
    于是默认值算出来的是 `state/users/<uid>/org`：**每个人一个私有的 org 层**。

    2026-09-16 在 node20（唯一跑组织档的真部署）上实测：12 个用户、12 个私有
    `org/` 目录，16 条晋升上来的 claim 散在其中 3 个人的目录里，互相看不见 ——
    「组织级知识」这件事从来没发生过一次。晋升、正典、效果传感器整套机制一直在
    各自的私有目录里空转。

    个人档同样受害，只是不显形：命令行 harness 读 `~/.harness-framework/org`，
    平台 worker 读 `~/.harness-framework/state/users/<uid>/org` —— **同一台机器上
    两个 org 层**，谁也不知道另一个存在。

    所以答案不能是"从 home 推"，必须由这里显式说出来：org 层是**这台安装**的
    属性，不是某个用户的属性。跨项目是它的本分，跨人是组织档的全部意义；
    个人档上那台机器只有一个人，共用与否没有区别，但两个 org 层一定是错的。

    ## 那「别人的知识跑进我的 org 层」怎么办

    那正是 org 层本来的样子。它不是谁的私人目录，是这台安装的公共层，而进入它
    **只有晋升一条路**（`scope="org"` 的直写在 `_validate_common` 咽喉处一律拒）。
    晋升是一次改写：去项目化、重述、带 `promoted_from` 出处。所以"共用"共的是
    审过的结论，不是谁的原始记录。
    """
    if not each_organisation_keeps_its_own_knowledge():
        return the_data_root() / "org"
    wanted = str(institution_id or "").strip()
    if not _AN_ORGANISATION_ID.fullmatch(wanted):
        raise DataRootError(
            "组织服务器上每个组织一份知识，得说是哪个组织的"
            f"（{institution_id!r} 不是一个组织 id）。")
    return the_data_root() / "organisations" / wanted


def the_projects_home() -> Path:
    """项目层在哪 —— **一个项目一份，成员共用**：`<数据根>/state/projects/<项目 id>/`。

    平台给每个人一个 harness home（`state/users/<uid>`：身份、画像、指令文件），而 harness 的项目层
    默认住在 home 底下 —— 于是一个项目几个成员就有几份项目知识，谁的会话攒的谁看得见
    （`docs/RFC_PROJECT_HOME_20260924.md`）。项目知道的属于项目，不属于说话人。

    和 `the_org_home` 同一个做法：由这里说，worker 与桥被**告知**（`projects_home`），不从 home 推。
    两档同一个规则 —— 个人档只有一个人，看起来和从前一样，只是换了位置（启动时搬一次，
    `services.one_home_per_project`）。
    """
    return data_root("state") / "projects"


def the_organisations_keyring(institution_id: str) -> Path:
    """一个组织的钥匙放哪 —— 登记机器时装进那台机器的那把 ssh 钥匙。

    **不在 org 层里**：org 层是 agent 读的地方（知识、授权、机器登记表）。钥匙若在那儿，
    任何一个项目的 agent 都读得到它 —— 授权就只剩建议。放在数据根下单独一处，私钥还
    按服务器密钥加密（同组织模型的 API key 一个待遇）。
    """
    wanted = str(institution_id or "").strip()
    if not _AN_ORGANISATION_ID.fullmatch(wanted):
        raise DataRootError(f"{institution_id!r} 不是一个组织 id")
    return the_data_root() / "secrets" / "organisations" / wanted


class DataRootError(RuntimeError):
    """持久数据的根没配、或配了个相对路径。"""


#: 数据根下的固定布局。名字是**数据的**，不是某一版代码的 —— 换 release、换
#: 机器、换启动方式都不该改变它们。
_DATA_ROOT_LAYOUT = {
    "repositories": "project-repositories",
    "worktrees": "project-worktrees",
    "state": "harness-state",
    "sockets": "harness-sockets",
}


def data_root(kind: str) -> Path:
    """解析一类持久数据的根目录。**这是唯一一处计算它们的地方。**

    ## 为什么这几个根不能有默认值

    它们从前是相对路径（`data/project_worktrees` 之类），`Path.resolve()` 相对的
    是**后端进程的 cwd**。node20 上 cwd 是 `current/platform/backend`，`current`
    是指向 `releases/<sha>` 的软链 —— 于是科研数据的落点变成了"这一版代码的子
    目录"，每次部署翻软链就换一个位置。

    2026-08-21 实测后果：43 个会话的 worktree 在旧根下，新根下一个都没有，而
    `session_path()` 是**按当前配置现算**的，于是所有旧会话发一条消息就在 0.1
    秒内 `Session Git worktree is not initialized`。库里 `sessions
    .git_worktree_path` 一直记着正确的绝对路径 —— 没有任何代码读它。
    同样的事 8-19 发生过两次、8-20 被人手工 export 修回来、8-21 再来一次。

    **一个能悄悄改变"数据在哪"的默认值，比没有默认值危险得多。** 相对路径的
    默认值等于把数据位置的决定权交给"这次是谁、用什么 cwd 起的进程"——
    没有任何一层能发现它选错了，因为每一层拿到的都是一个"合法"的路径。
    所以这里的选择是：**要么显式给一个绝对路径，要么起不来。**

    真正兜住这一类的是启动闸（`main._refuse_to_serve_where_the_data_is_not`），
    它拿库里记着的根和这里算出来的根机械比对。本函数只保证一件事：算出来的
    东西不取决于 cwd。

    ## 单根派生 vs 四个独立变量

    四个变量意味着四次配错的机会，且它们只有**同时**指对才有意义（worktree
    在 A、仓库在 B = 一个 Git 仓库找不到自己的 worktree）。所以对外只有一个
    `PLATFORM_DATA_ROOT`，布局由本模块固定。独立变量保留，只为测试隔离
    （conftest 给每条测试发一个 tmp_path）——它们**同样不接受相对路径**。
    """
    try:
        subdirectory = _DATA_ROOT_LAYOUT[kind]
    except KeyError:  # pragma: no cover - 拼错 kind 是程序错误，不是配置错误
        raise ValueError(f"unknown data root kind: {kind!r}") from None

    override = {
        "repositories": settings.project_repository_root,
        "worktrees": settings.project_worktree_root,
        "state": settings.harness_state_root,
        "sockets": settings.harness_socket_root,
    }[kind]
    variable = {
        "repositories": "PROJECT_REPOSITORY_ROOT",
        "worktrees": "PROJECT_WORKTREE_ROOT",
        "state": "HARNESS_STATE_ROOT",
        "sockets": "HARNESS_SOCKET_ROOT",
    }[kind]

    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_absolute():
            raise DataRootError(
                f"{variable}={override!r} 是相对路径。持久科研数据的位置不能取决于"
                f"进程的工作目录 —— 请给绝对路径，或改用 PLATFORM_DATA_ROOT。"
            )
        return candidate

    if not settings.platform_data_root:
        raise DataRootError(
            "PLATFORM_DATA_ROOT 没有配置。它是这台部署上所有持久科研数据"
            f"（Project 仓库 / Session worktree / harness 运行态 / worker socket）"
            f"的根，必须显式给一个绝对路径，例如：\n"
            f"    export PLATFORM_DATA_ROOT=/srv/research-platform/data\n"
            f"（也可以只覆盖单独一类：{variable}=<绝对路径>，测试就是这么隔离的。）"
        )

    root = Path(settings.platform_data_root).expanduser()
    if not root.is_absolute():
        raise DataRootError(
            f"PLATFORM_DATA_ROOT={settings.platform_data_root!r} 是相对路径。"
            "持久科研数据的位置不能取决于进程的工作目录。"
        )
    return root / subdirectory
