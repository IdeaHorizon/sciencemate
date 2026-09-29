"""Test fixtures for the research platform."""

from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base, get_db
from app.main import app

@pytest.fixture(scope="session")
def worker_database_url(tmp_path_factory) -> str:
    """落盘的 SQLite（不是 in-memory），**每个 xdist worker 一份**，放在 pytest 的临时目录里。

    每个 test 都在这个库上 create_all / drop_all。串行没问题；并行共用一个文件
    就是：A 建表时 B 也在建（"table sessions already exists"），A 收尾 drop 时
    B 正在用（"no such table: runs"）。实测 -n 12 下 7 failed + 35 errors，全部
    是这两句 —— 一个根因。`tmp_path_factory` 的根在 xdist 下本来就按 worker 分开
    （`popen-gw0/`…），每次运行一个新的。

    **不在 cwd（`platform/backend/`）里**：这里从前是 `sqlite:///./test-gw0.db`，于是
    源码树里一直有别的 worker 正在写的库和它的 `-journal`。PR#1139 上
    `test_an_installed_server_knows_its_own_version` 整棵拷 `platform/backend` 时，
    `test-gw0.db-journal` 在列完目录、还没拷到它的那一刻消失 —— shutil.Error。
    测试的运行态不该出现在被测的源码树里。
    """
    path = tmp_path_factory.mktemp("sqlite") / "test.db"
    return f"sqlite+aiosqlite:///{path.as_posix()}"


@pytest.fixture(autouse=True)
def isolated_project_repositories(tmp_path, monkeypatch, worker_database_url):
    """Every test gets a disposable data root.

    一个根就够：仓库 / worktree / harness 运行态 / socket 全由 `data_root()`
    从它派生（见 `app.config.data_root`）。此前这里只钉了前两个，另外两个靠
    相对路径默认值落到 **cwd** —— 也就是跑测试的那个目录，测试之间互相看得见。
    """
    monkeypatch.setattr(settings, "platform_data_root", str(tmp_path / "data"))
    # harness 读的是 `HARNESS_FRAMEWORK_HOME`。上面那句只钉了平台这一侧的名字，
    # 于是**平台侧隔离、harness 侧仍指着真实 home** —— 走 `app.services.instructions`
    # 这类 harness-facing 路径的测试，读写的是这台机器上真正的 ~/.harness-framework。
    # "一个根就够"要成立，两个名字就得同时钉住（生产里它们本来就是同一个目录，
    # 不一致时 `config.publish_the_data_root()` 直接拒绝启动）。
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "data"))
    # 库也一并钉住 —— 这不是"顺手多钉一个"，而是这条隔离唯一真正的判据：
    # **这个进程有没有能力写到真实的 home。**
    #
    # 个人档给了 `database_url` 一个默认值（数据根里的 db.sqlite）。默认值指向
    # `~/.harness-framework/db.sqlite` 时，任何一条绕过 `runtime_client` 的路径
    # 调到 `get_engine()`，都会**安安静静地建出那个文件**并往里写；从前那里是
    # Postgres，连不上会当场报错，所以这条路一直没显形。2026-09-05 实测：
    # `-n 6 --dist load` 下 10 条测试失败，而真实 home 里多了一个 db.sqlite。
    monkeypatch.setattr(settings, "database_url", worker_database_url)

    # 门禁：测试套件里默认**开着**。
    #
    # 组织档的产品默认是 `invite`（2026-09-16 起）。而这个套件里几十条测试把
    # `/register` 当成"造一个用户"的顺手工具，它们的题目是 token / 登出 / 项目
    # 权限，不是门禁 —— 让门禁默认拦住它们，等于把一次产品默认的改动变成几十处
    # 与题目无关的返工，而且每加一条新测试都要再踩一次。
    #
    # 真正要守的两件事各有自己的判据，不靠这个默认值：
    #   · 产品默认是 invite —— `test_the_shipped_default_is_invite_only` 直接读
    #     `Settings` 的字段默认，这一行覆盖不了它；
    #   · 门禁本身怎么工作 —— `test_who_gets_in_and_who_manages` 自己显式设 invite。
    monkeypatch.setattr(settings, "registration_mode", "open")
    monkeypatch.setattr(settings, "project_repository_root", "")
    monkeypatch.setattr(settings, "project_worktree_root", "")
    monkeypatch.setattr(settings, "harness_state_root", "")
    monkeypatch.setattr(settings, "harness_socket_root", "")
    monkeypatch.setattr(
        settings,
        "harness_root",
        str(Path(__file__).resolve().parents[3]),
    )
    # Backend tests exercise the real dispatch/DB boundary but never execute
    # model payloads. Freeze a syntactically valid immutable image identity;
    # destructive sandbox tests live at repository root and use real Docker.
    # `harness_contract._harness_root` 有 lru_cache(maxsize=1)：一条测试算出的
    # harness 根会留给下一条。socket 地址那条链先过 `worker_addressing()`（依赖
    # `_harness_root()`）再到 `data_root("sockets")` 的相对路径闸——缓存里留着
    # 别人的根，就让这条链在 xdist `--dist load` 分布抖动下时通时断（实测
    # `test_a_relative_config_never_gets_as_far_as_spawning_a_worker` 偶发不
    # raise：worker_addressing 提前 return None，到不了那道闸）。清缓存本就是
    # 隔离的一部分——`harness_domain_registry` 的「缓存要连着清」原则，只是那条
    # fixture 非 autouse、不用它的测试全漏。放这里让每条测试都用当下的 settings。
    from app.services import harness_contract

    harness_contract._harness_root.cache_clear()
    # attempt 冻的「谁来守」来自本机原生后端（darwin / linux），探针一次一个进程。
    # Docker 镜像身份的替身随 PR C 一起没了。
    isolation = harness_contract.isolation_module()
    isolation._reset_for_tests()
    yield
    isolation._reset_for_tests()
    harness_contract._harness_root.cache_clear()


@pytest_asyncio.fixture(autouse=True)
async def _no_execution_outlives_its_test():
    """每条测试结束时收掉它留下的后台执行。

    聊天流是**服务器自有**的：SSE 断开不取消它（那是设计，见
    `shutdown_detached_executions` 的 docstring —— 用户关掉页面不该杀掉研究）。
    代价是任何驱动 `/chat/.../stream` 的测试都会留一个活着的 task，它带着
    自己的 DB Session 继续跑，而下一条测试正在 drop_all 建新库。

    症状是**随机**红：2026-08-13 全量跑两次，红的是两条不同的测试，而它们
    单跑、整文件跑都绿。谁也不会怀疑到"上一条测试还在跑"。

    收在这里而不是逐条测试里 —— 逐条写就是名单式护栏，下一条驱动 stream 的
    测试默认又漏。
    """
    yield
    from app.services.local_execution import shutdown_detached_executions

    await shutdown_detached_executions()


@pytest.fixture
def harness_domain_registry(monkeypatch):
    """把域词表接上 —— 指向本仓库自己的 harness checkout。

    词表按设计只有一处定义（`core/domain_registry.py`），平台经
    `harness_contract` 读它。测试进程默认没有 `HARNESS_ROOT`，于是任何碰域的
    代码都会拿到 `HarnessContractUnavailable`。

    **不 monkeypatch 掉 `domains` 那一层**：替身遮住的正是"平台读得到 harness
    的词表吗"这个接缝，而那是这条契约唯一值得验的东西。这里给的是真路径，
    走的是真加载。

    缓存要连着清：`_harness_root` 有 lru_cache，`domains` 自己也存了模块引用，
    留着上一条测试的值就等于这条测试在读别处的词表。
    """
    from app.services import harness_contract
    from app.services.feed import domains as domain_service

    repo_root = Path(__file__).resolve().parents[3]
    assert (repo_root / "core" / "domain_registry.py").is_file(), repo_root
    monkeypatch.setattr(settings, "harness_root", str(repo_root))
    harness_contract._harness_root.cache_clear()
    domain_service.reset_cache()
    yield domain_service
    harness_contract._harness_root.cache_clear()
    domain_service.reset_cache()


@pytest_asyncio.fixture
async def db_engine(worker_database_url):
    """测试库 —— 同时把**模块级** session factory 也指过来。

    `client` fixture 只覆盖了 FastAPI 的 `get_db` 依赖。可平台里不是只有请求
    处理器在写库：脱离请求的写者（摄取、自动命名、替人点推荐项…）拿的是
    `app.database.get_session_factory()` 这个模块级工厂 —— 它指着**真** DATABASE_URL。

    于是那些写者在测试里根本连不上，或者更糟：连上了开发机上真的那个库。
    它们等于不在测试的视野里 —— 测试看不见的边界，就是没有被测的边界。
    """
    import app.database as database

    engine = create_async_engine(worker_database_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    saved_engine, saved_factory = database._engine, database._session_factory
    database._engine = engine
    database._session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield engine
    finally:
        database._engine, database._session_factory = saved_engine, saved_factory
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def client(db_session):
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
