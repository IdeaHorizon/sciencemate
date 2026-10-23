"""缺一个能力槽这件事有**两个读者**，所以目录得有两句话。

`absence_note` 是注入给消费方节点的：「审图工具不在你的工具面上……你什么都
不用做，也不要为这件事重跑 —— 平台配没配审图模型不是你能改的事。」对一个
正在跑的节点，这句话完全正确。

但『设置 → 模型』那一页的读者恰恰是**唯一能把这个槽填上的人**。2026-08-31
实测：一位研究员在那一页读到这句话，同时看到下拉是灰的、下面五条连接全是
只读的，于是认为平台不支持配审图模型 —— 而后端从来没拦过他（建个人连接
不查管理权限，他自己注册一条勾上就能用）。一份文案同时服务两个相反的读者，
必然对其中一个撒谎。

于是分成 `absence_note`（给节点）和 `absence_impact`（给人）。这个文件钉住
后者**每个角色都有、且送得到 API 面上** —— 送不到的话，前端读到 undefined，
那一页又变回只有一个空槽。
"""

import re

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.auth import create_access_token, hash_password
from app.database import get_db
from app.main import app
from app.models.user import User
from app.services.model_role_catalog import catalog


def test_每个角色都带一句写给人的缺席影响():
    # 扫盘，不写名单：以后加一个角色，忘了写这句话，这条测试就红。
    for spec in catalog():
        assert spec.absence_impact, f"角色 {spec.id!r} 没有 absence_impact —— 设置页上它会是个哑槽"


def test_给人的那句话不是给节点的那句话的复制():
    for spec in catalog():
        assert spec.absence_impact != spec.absence_note, (
            f"角色 {spec.id!r} 的两句话一模一样 —— 那就是两个读者又合回了一份文案"
        )


def test_缺席影响送得到_API_面上():
    # `GET /settings/model-roles` 直接 `**item.to_public_dict(lang)`。这里断的是
    # 那一层：字段留在 dataclass 里而没进投影，前端拿到的就是 undefined。
    for spec in catalog():
        assert spec.to_public_dict("zh")["absence_impact"] == spec.absence_impact
        assert spec.to_public_dict("en")["absence_impact"] == spec.absence_impact_en


# ── 2026-10-19：英文界面的「模型角色」一节整段是中文 ─────────────────────────
#
# 目录只写了中文，接口原样下发：英文界面上写着「主推理模型」「图像审查模型（VLM）」
# 和三段中文说明。说给人听的三句（title / description / absence_impact）要有英文，
# 而且按看的人的界面语言给；absence_note 是给节点的，不在此列。

HAN = re.compile(r"[\u4e00-\u9fff]")
SAID_TO_A_PERSON = ("title", "description", "absence_impact")


def test_每个角色说给人听的三句都有英文():
    # 扫盘：以后加一个角色只写了中文，英文界面上又是一段中文 —— 这条就红。
    for spec in catalog():
        for field in SAID_TO_A_PERSON:
            english = getattr(spec, f"{field}_en")
            assert english, f"角色 {spec.id!r} 没有 {field}_en —— 英文界面上这里会是中文"
            assert not HAN.search(english), f"角色 {spec.id!r} 的 {field}_en 里写着中文：{english!r}"


@pytest_asyncio.fixture
async def two_readers(db_engine):
    """同一个部署上两个人：一个用中文界面，一个用英文界面。"""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as db:
        tokens = {}
        for email, language in (("zh@atrium.local", "zh"), ("en@atrium.local", "en")):
            user = User(
                email=email,
                hashed_password=hash_password("RoleCatalog2026!"),
                display_name=email,
                role="researcher",
                preferences={"interface": {"language": language}},
            )
            db.add(user)
            await db.flush()
            tokens[language] = create_access_token(user)
        await db.commit()

    async def override():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, tokens
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_设置页拿到的角色说明跟着看的人的界面语言(two_readers):
    """走真接口：目录有了英文还不够，接口得按**这个人**的语言挑。"""
    client, tokens = two_readers
    seen = {}
    for language, token in tokens.items():
        response = await client.get(
            "/api/v1/settings/model-roles", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200, response.text
        seen[language] = {role["id"]: role for role in response.json()["roles"]}
    assert seen["en"], "一个角色都没拿到 —— 这条测试什么都没验"
    for role_id, role in seen["en"].items():
        for field in SAID_TO_A_PERSON:
            assert not HAN.search(role[field]), (
                f"英文界面的设置页上，角色 {role_id!r} 的 {field} 是中文：{role[field]!r}")
    assert any(HAN.search(role["title"]) for role in seen["zh"].values()), (
        "中文界面拿到的也不是中文 —— 投影把语言弄反了")


def test_设置页上的说明不带_markdown_记号():
    # description 在设置页和首次引导里按纯文本显示 —— `**可见缺陷判读**` 原样露出两对星号。
    # absence_note 是注入给节点的，那边 markdown 有用，不在此列。
    for spec in catalog():
        for text in (spec.description, spec.description_en):
            assert "**" not in text, f"角色 {spec.id!r} 的说明里有 ** —— 设置页上会原样显示：{text!r}"
