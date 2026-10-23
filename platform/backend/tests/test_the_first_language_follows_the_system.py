"""第一次打开时的界面语言跟着系统走（wangd 2026-10-19）。

前端判（`languageForASystem`：系统语言列表里有中文就中文，否则英文），后端只答两件
它才答得出的事：这个人**选过没有**，以及他电脑上的**整张**系统语言列表 —— 桌面版的
WKWebView 只把第一项交给网页（2026-10-19 实测 `AppleLanguages = (en-GB, zh-Hans-CN)`
的 Mac 上 `navigator.languages == ["en-GB"]`），中文排第二的人会被判成英文。
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.auth import create_access_token, hash_password
from app.config import settings
from app.main import app
from app.models.user import User
from app.services import system_language


def test_reads_the_whole_macos_list_in_order():
    # `defaults read -g AppleLanguages` 的原样输出（10-19 这台机器）。
    printed = '(\n    "en-GB",\n    "zh-Hans-CN"\n)\n'
    assert system_language.parse_apple_languages(printed) == ["en-GB", "zh-Hans-CN"]
    assert system_language.parse_apple_languages("(\n    en,\n    de\n)\n") == ["en", "de"]


def test_reads_the_posix_environment_in_order():
    environ = {"LANGUAGE": "de_DE:en_US", "LANG": "zh_CN.UTF-8", "LC_ALL": "C"}
    assert system_language._from_environment(environ) == ["de-DE", "en-US", "zh-CN"]


def test_windows_reads_the_language_list_not_only_the_display_language(monkeypatch):
    """9800x3d 实测：显示语言 en-GB、en-US，语言列表 zh-Hans-CN —— 一个用英文界面的中文用户。"""
    monkeypatch.setattr(system_language, "_windows_display_languages", lambda: ["en-GB", "en-US"])
    monkeypatch.setattr(system_language, "_windows_language_list", lambda: ["zh-Hans-CN", "en-GB"])
    assert system_language._windows() == ["en-GB", "en-US", "zh-Hans-CN"]


def test_a_reader_that_fails_is_an_empty_list_not_an_error(monkeypatch):
    def broken(*_args, **_kwargs):
        raise OSError("no defaults here")

    monkeypatch.setattr(system_language, "_macos", broken)
    monkeypatch.setattr(system_language, "_windows", broken)
    monkeypatch.setattr(system_language, "_from_environment", broken)
    assert system_language.preferred_languages() == []


@pytest_asyncio.fixture
async def newcomer(db_engine):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as db:
        user = User(email="newcomer@atrium.local", hashed_password=hash_password("FirstOpen2026!"),
                    display_name="Newcomer", role="researcher")
        db.add(user)
        await db.commit()
        token = create_access_token(user)

    # 用真的 get_db（db_engine 已把模块级会话工厂指到测试库）：它在请求结束时提交。
    # 换成一个不提交的替身，PUT 存进去的东西下一问就看不见 —— 测的就不是产品了。
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_a_newcomer_has_not_chosen_and_one_save_settles_it(newcomer, monkeypatch):
    client, headers = newcomer
    monkeypatch.setattr(settings, "profile", "personal")
    monkeypatch.setattr(system_language, "preferred_languages", lambda: ["en-GB", "zh-Hans-CN"])

    hint = await client.get("/api/v1/settings/interface/language", headers=headers)
    assert hint.status_code == 200, hint.text
    assert hint.json() == {"chosen": False, "system_languages": ["en-GB", "zh-Hans-CN"], "chosen_at_install": None}

    current = (await client.get("/api/v1/settings/interface", headers=headers)).json()
    saved = await client.put("/api/v1/settings/interface", headers=headers, json={**current, "language": "en"})
    assert saved.status_code == 200, saved.text
    hint = await client.get("/api/v1/settings/interface/language", headers=headers)
    assert hint.json()["chosen"] is True, "存过一次还说没选过 —— 每次打开都会再猜一遍"


@pytest.mark.asyncio
async def test_an_organisation_server_does_not_report_its_own_system_language(newcomer, monkeypatch):
    """组织服务器装的什么语言与看界面的人无关 —— 答空列表，前端用浏览器给的。"""
    client, headers = newcomer
    monkeypatch.setattr(settings, "profile", "org")
    monkeypatch.setattr(system_language, "preferred_languages", lambda: ["zh-CN"])
    hint = await client.get("/api/v1/settings/interface/language", headers=headers)
    assert hint.status_code == 200, hint.text
    assert hint.json()["system_languages"] == []


def test_what_the_person_picked_in_the_windows_installer_reaches_the_app(tmp_path):
    """安装器上点过「中文 / English」才写这一笔；读到别的、读不到都不算。"""
    assert system_language.language_chosen_at_install(tmp_path) is None
    (tmp_path / system_language.INSTALL_CHOICE_FILE).write_text("zh\n", encoding="utf-8")
    assert system_language.language_chosen_at_install(tmp_path) == "zh"
    (tmp_path / system_language.INSTALL_CHOICE_FILE).write_text("fr", encoding="utf-8")
    assert system_language.language_chosen_at_install(tmp_path) is None


def test_the_file_name_is_the_one_the_installer_writes():
    """两边各写一份文件名就是两个会分叉的答案 —— 至少钉住它们相等。"""
    from pathlib import Path

    footprint = (Path(__file__).resolve().parents[3] / "platform/desktop/windows/Speech.cs").read_text(encoding="utf-8")
    assert f'ChoiceFileName = "{system_language.INSTALL_CHOICE_FILE}"' in footprint
