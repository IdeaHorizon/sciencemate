"""后端说给人听的报错，两种语言都要写 —— 按发问那一屏的语言说。

## 为什么有这一份

2026-10-19 wangd：「英文版设置里面还有中文」。前端早有一道扫盘闸（每句中文都得配
英文），那一刻它是绿的 —— 中文是从后端来的：`HTTPException(detail="现在的密码不对")`
这样的报错有五十来处，前端原样显示。英文界面上，改密码输错一次就是一句中文。

## 判据

`app/` 里每一个 `HTTPException(...)`：参数里出现的中文，必须写在一个**同时有
`"zh"` 和 `"en"` 两个键**的 dict 里（`say({"zh": …, "en": …}, lang)`）。合法的那条路
只有一条，其余一律违规 —— 扫的是"报错里有中文"这件事，不是某几个文件、某种写法。

说给哪一屏听：`language_on_screen`（前端每一问都带 `X-Interface-Language`），一半的端点
那一刻没有用户（注册、桌面本地的组织连接、模型通行证），读不到账号偏好。

带着原话透出来的异常（`detail=str(exc)`）是第二条路（2026-10-19 第二轮：服务层一百多处
中文报错经它原话透到界面上）。判据分两半，都按"这件事"扫，不列文件、不列类名：

- **构造**：定义了 `said_in` 的异常类（`services/say.SaidToAPerson` 及其子类），以及签名里
  有必填的关键字参数 `en` 的函数（`_refused(user, status, 中文, en=…)` 那种），每一处调用
  都得带 `en=`。
- **透传**：`HTTPException(...)` 里、以及 `app/api`、`app/pro/api` 里带 `message` /
  `detail` / `error` 键的 dict 里，不许出现 `str(exc)` / `f"{exc}"` / `exc.message` ——
  `exc` 是 `except … as exc` 接住的那个。说给人听的异常用 `exc.said_in(lang)`；不是我们的（httpx、OSError、harness 的技术原话），
  原话只当证据填进一句两种语言的话里：`say({"zh": "…：{why}", "en": "…: {why}"}, lang, why=exc)`，
  一个 except 两种都接得住时用 `said_of(exc, lang, otherwise=…)`。说给人听的那种也不许
  `str()`：它的 `str()` 是中文那句。
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
HAN = re.compile(r"[一-鿿]")


def _bilingual_values(call: ast.Call) -> set[int]:
    """调用里所有 `{"zh": …, "en": …}` 的值（及其子节点）—— 它们是已经两种语言都写了的。"""
    done: set[int] = set()
    for node in ast.walk(call):
        if not isinstance(node, ast.Dict):
            continue
        keys = {k.value for k in node.keys if isinstance(k, ast.Constant)}
        if not {"zh", "en"} <= keys:
            continue
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value in ("zh", "en"):
                done.update(id(child) for child in ast.walk(value))
    return done


def _offenders() -> list[str]:
    found: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if not (isinstance(node, ast.Call)
                    and getattr(node.func, "id", getattr(node.func, "attr", None)) == "HTTPException"):
                continue
            done = _bilingual_values(node)
            for child in ast.walk(node):
                if (isinstance(child, ast.Constant) and isinstance(child.value, str)
                        and HAN.search(child.value) and id(child) not in done):
                    found.append(f"{path.relative_to(APP.parent)}:{child.lineno} → {child.value.strip()[:60]}")
    return found


def test_there_is_something_to_scan() -> None:
    calls = sum(source.count("HTTPException(") for source in
                (p.read_text(encoding="utf-8") for p in APP.rglob("*.py")))
    assert calls > 50, f"只扫到 {calls} 个 HTTPException —— 扫法塌了，这道闸是空的"


def test_every_error_with_chinese_in_it_also_says_it_in_english() -> None:
    offenders = _offenders()
    assert not offenders, (
        f"这些报错只有中文，英文界面上会原样露出来（{len(offenders)} 处）。"
        "写成 say({\"zh\": …, \"en\": …}, lang)，lang 取 language_on_screen：\n" + "\n".join(offenders))


def test_the_last_failed_update_is_remembered_in_both_languages(tmp_path) -> None:
    """「上一次更新没装上」是启动器写下、「关于」页读的 —— 写的那一刻不知道那一屏是哪种语言。"""
    from app.services import self_update as su

    record = {"zh": "UpdateError: 暂存的 0.5.8 不完整", "en": "UpdateError: the staged 0.5.8 is incomplete"}
    assert su.said_by_the_record(json.dumps(record, ensure_ascii=False), "en") == record["en"]
    assert su.said_by_the_record(json.dumps(record, ensure_ascii=False), "zh") == record["zh"]
    # 老版本写下的是一行原话：照原样给，不因为读不成 JSON 就丢掉。
    assert su.said_by_the_record("RuntimeError: boom", "en") == "RuntimeError: boom"


def test_an_update_error_cannot_be_raised_in_one_language() -> None:
    import pytest

    from app.services import self_update as su

    with pytest.raises(TypeError):
        su.UpdateError("只有中文")  # type: ignore[call-arg]
    refused = su.UpdateError("签名对不上", en="the signature does not match")
    assert refused.said_in("en") == "the signature does not match"
    assert refused.said_in("zh") == str(refused) == "签名对不上"


async def test_the_same_refusal_comes_back_in_the_language_of_the_screen(db_engine, monkeypatch) -> None:
    """走真接口：同一句「没有暂存的更新」，英文那一屏收到英文，中文那一屏收到中文，没说就是默认。"""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.auth import create_access_token, hash_password
    from app.config import settings
    from app.main import app
    from app.models.user import User

    monkeypatch.setattr(settings, "profile", "personal")   # 个人版：本机用户就能点重启
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as db:
        user = User(email="screen@atrium.local", hashed_password=hash_password("ScreenLang2026!"),
                    display_name="Screen", role="researcher")
        db.add(user)
        await db.commit()
        token = create_access_token(user)

    said = {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for language in ("en", "zh", None):
            headers = {"Authorization": f"Bearer {token}"}
            if language:
                headers["X-Interface-Language"] = language
            answer = await client.post("/api/v1/update/restart", headers=headers)
            assert answer.status_code == 409, answer.text
            said[language] = answer.json()["detail"]
    assert not HAN.search(said["en"]), f"英文那一屏收到的是：{said['en']}"
    assert HAN.search(said["zh"]), f"中文那一屏收到的是：{said['zh']}"
    assert said[None] == said["zh"], "没带语言头时该是默认语言（中文）"


def _trees() -> dict[Path, ast.Module]:
    return {p: ast.parse(p.read_text(encoding="utf-8")) for p in APP.rglob("*.py")}


def _called(node: ast.Call) -> str | None:
    return getattr(node.func, "id", getattr(node.func, "attr", None))


def _speaking_classes(trees: dict[Path, ast.Module]) -> set[str]:
    """定义了 `said_in` 的异常类，及其子类 —— "这个类说给人听"，按定义扫，不列类名。"""
    classes = [n for tree in trees.values() for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    speaking = {c.name for c in classes
                if any(isinstance(f, ast.FunctionDef) and f.name == "said_in" for f in c.body)}
    grew = True
    while grew:   # 子类也算
        before = len(speaking)
        speaking |= {c.name for c in classes
                     if any(getattr(b, "id", getattr(b, "attr", None)) in speaking for b in c.bases)}
        grew = len(speaking) != before
    return speaking


def _functions_that_need_the_english(trees: dict[Path, ast.Module]) -> set[str]:
    """签名里有**必填**的关键字参数 `en` 的函数（`__init__` 不算 —— 那是按类名调的，上面管）。"""
    return {n.name for tree in trees.values() for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name != "__init__"
            and any(a.arg == "en" and d is None for a, d in zip(n.args.kwonlyargs, n.args.kw_defaults))}


def test_every_raise_of_an_error_said_to_a_person_brings_the_english_too() -> None:
    """`en=` 必填只在那一行**真跑到**时才报 TypeError —— 没有测试走到的那条失败路径，
    到了用户手里才炸。所以静态地再查一遍：说给人听的异常类（及其子类）、以及要 `en`
    的拒绝帮手，每一处调用都得带 `en=`。"""
    trees = _trees()
    speaking = _speaking_classes(trees)
    # 扫不到就等于没有这道闸。专业版的类只在 app/pro 在的时候要求 ——
    # 公开树（个人版导出）里没有那个目录（2026-10-19 public-tree job 撞出来的）。
    expected = {"SaidToAPerson", "UpdateError", "NothingPublishedYet"}
    if (APP / "pro").is_dir():
        expected.add("InvitationError")
    assert expected <= speaking, speaking
    # 自己算英文那句的子类（`__init__` 不收 `en`，比如由一串违规拼出两种语言）：构造处
    # 不用带，但它的 `__init__` 必须把 `en=` 交给父类 —— 那一处同样查。
    says_it_itself = _classes_that_say_the_english_themselves(trees, speaking)
    callables = (speaking - set(says_it_itself)) | _functions_that_need_the_english(trees)
    missing = [f"{where} {name}.__init__ 没把 en= 交给父类"
               for name, where in says_it_itself.items() if where]
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and _called(node) in callables
                    and not any(k.arg == "en" for k in node.keywords)):
                missing.append(f"{path.relative_to(APP.parent)}:{node.lineno} {_called(node)}(…)")
    assert not missing, "这些地方抛的是说给人听的异常，却只写了中文：\n" + "\n".join(missing)


def _classes_that_say_the_english_themselves(trees: dict[Path, ast.Module],
                                             speaking: set[str]) -> dict[str, str]:
    """说给人听的类里，`__init__` 不收 `en` 的那几个 → 它没把 `en=` 交给父类时的位置（交了就是空）。"""
    found: dict[str, str] = {}
    for path, tree in trees.items():
        for cls in ast.walk(tree):
            if not (isinstance(cls, ast.ClassDef) and cls.name in speaking):
                continue
            init = next((f for f in cls.body if isinstance(f, ast.FunctionDef) and f.name == "__init__"), None)
            if init is None or any(a.arg == "en" for a in (*init.args.args, *init.args.kwonlyargs)):
                continue
            hands_it_on = any(isinstance(n, ast.Call) and _called(n) == "__init__"
                              and any(k.arg == "en" for k in n.keywords) for n in ast.walk(init))
            found[cls.name] = "" if hands_it_on else f"{path.relative_to(APP.parent)}:{init.lineno}"
    return found


def _caught(handler: ast.ExceptHandler) -> list[str]:
    def names(node: ast.expr | None) -> list[str]:
        if node is None:
            return ["<bare except>"]
        if isinstance(node, ast.Tuple):
            return [n for element in node.elts for n in names(element)]
        return [getattr(node, "id", getattr(node, "attr", ast.unparse(node)))]
    return names(handler.type)


#: 接口层答出去的 dict 里，这几个键的值是给人看的那句话（报错的 `message`、探机那一问的
#: `detail`、安装进度的 `error`）。
_SAID_UNDER = frozenset({"message", "detail", "error"})


def _what_reaches_a_person(path: Path, handler: ast.ExceptHandler) -> list[ast.AST]:
    """这个 except 块里，原样到人眼前的那几处：`HTTPException(...)` 的参数，以及接口层
    （`app/api`、`app/pro/api`）里带 `message` / `detail` / `error` 键的 dict。"""
    parts = path.relative_to(APP).parts
    in_an_endpoint = parts[:1] == ("api",) or parts[:2] == ("pro", "api")
    found: list[ast.AST] = []
    for node in ast.walk(handler):
        if isinstance(node, ast.Call) and _called(node) == "HTTPException":
            found.append(node)
        elif (in_an_endpoint and isinstance(node, ast.Dict)
              and any(isinstance(k, ast.Constant) and k.value in _SAID_UNDER for k in node.keys)):
            found.append(node)
    return found


def _passed_through(name: str, where: ast.AST) -> list[ast.AST]:
    """`str(exc)`、`f"…{exc}…"`、`exc.message`：异常的原话被原样塞进了给人看的那句话。"""
    return [node for node in ast.walk(where)
            if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "str" and node.args
                and isinstance(node.args[0], ast.Name) and node.args[0].id == name)
            or (isinstance(node, ast.FormattedValue) and isinstance(node.value, ast.Name)
                and node.value.id == name)
            or (isinstance(node, ast.Attribute) and node.attr == "message"
                and isinstance(node.value, ast.Name) and node.value.id == name)]


def _passthroughs() -> tuple[list[str], int]:
    """(违规的透传, 已经按语言说的透传处数)。后一个数是给"扫到东西没有"用的。"""
    trees = _trees()
    speaking = _speaking_classes(trees)
    offenders: list[str] = []
    spoken = 0
    for path, tree in trees.items():
        for handler in ast.walk(tree):
            if not (isinstance(handler, ast.ExceptHandler) and handler.name):
                continue
            caught = _caught(handler)
            ours = all(name in speaking for name in caught)
            for where in _what_reaches_a_person(path, handler):
                for node in _passed_through(handler.name, where):
                    how = (f"{handler.name}.said_in(lang)" if ours else
                           f"say({{\"zh\": \"…：{{why}}\", \"en\": \"…: {{why}}\"}}, lang, why={handler.name})"
                           f" 或 said_of({handler.name}, lang, otherwise=…)")
                    offenders.append(f"{path.relative_to(APP.parent)}:{node.lineno} "
                                     f"except {' | '.join(caught)} → 原话透传；改成 {how}")
                spoken += sum(
                    1 for node in ast.walk(where) if isinstance(node, ast.Call) and (
                        (isinstance(node.func, ast.Attribute) and node.func.attr == "said_in"
                         and isinstance(node.func.value, ast.Name) and node.func.value.id == handler.name)
                        or (_called(node) in ("said_of", "say") and any(
                            isinstance(a, ast.Name) and a.id == handler.name
                            for a in [*node.args, *(k.value for k in node.keywords)]))))
    return offenders, spoken


def test_an_error_caught_on_the_way_out_is_not_handed_over_in_its_own_words() -> None:
    """`except X as exc: raise HTTPException(detail=str(exc))` —— 服务层的中文原话就这样到了
    英文那一屏（2026-10-19：≈40 处）。说给人听的异常按这一屏说（`said_in`），别人的异常
    原话只当证据（`say(…, why=exc)` / `said_of`）。"""
    offenders, spoken = _passthroughs()
    # 扫不到就等于没有这道闸：公开树（没有 app/pro）里也有几十处已经按语言说的透传。
    assert spoken >= 30, f"只认出 {spoken} 处按语言说的透传 —— 扫法塌了，这道闸是空的"
    assert not offenders, (
        f"这些地方把接住的异常原话透给了人（{len(offenders)} 处），英文那一屏会看到中文：\n"
        + "\n".join(offenders))


def test_a_bilingual_error_says_chinese_by_default_and_english_when_asked() -> None:
    """`SaidToAPerson` 的契约：`str()` 是中文（日志、`match=` 照旧），`en` 必填，`said_of`
    对别人的异常只把原话当证据。"""
    import pytest

    from app.services.say import SaidToAPerson, in_both_languages, said_of

    with pytest.raises(TypeError):
        SaidToAPerson("只有中文")  # type: ignore[call-arg]
    refused = SaidToAPerson("没有这个家", en="There is no such home")
    assert str(refused) == refused.said_in("zh") == "没有这个家"
    assert refused.said_in("en") == "There is no such home"
    otherwise = {"zh": "读不了：{why}", "en": "Cannot read it: {why}"}
    assert said_of(refused, "en", otherwise=otherwise) == "There is no such home"
    assert said_of(OSError("disk full"), "en", otherwise=otherwise) == "Cannot read it: disk full"
    assert said_of(OSError("disk full"), "zh", otherwise=otherwise) == "读不了：disk full"
    assert in_both_languages({"zh": "端口 {port} 用不了", "en": "Port {port} cannot be used"}, port=8) \
        == "端口 8 用不了\nPort 8 cannot be used"


async def test_a_refusal_from_the_service_layer_comes_back_in_the_language_of_the_screen(db_engine) -> None:
    """走真接口，拒绝是**服务层**抛的（`other_homes.ThereIsNoSuchHomeError`）而不是端点里写的：
    从前它经 `detail=refused.message` 原话透出来，英文那一屏也是中文。"""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.auth import create_access_token, hash_password
    from app.main import app
    from app.models.user import User

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as db:
        user = User(email="homeless@atrium.local", hashed_password=hash_password("HomelessLang2026!"),
                    display_name="Homeless", role="researcher")
        db.add(user)
        await db.commit()
        token = create_access_token(user)

    said = {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for language in ("en", "zh"):
            answer = await client.post(
                "/api/v1/projects/", json={"name": "Nowhere", "home": "a-home-nobody-offers"},
                headers={"Authorization": f"Bearer {token}", "X-Interface-Language": language})
            assert answer.status_code == 404, answer.text
            said[language] = answer.json()["detail"]
    assert not HAN.search(said["en"]), f"英文那一屏收到的是：{said['en']}"
    assert HAN.search(said["zh"]), f"中文那一屏收到的是：{said['zh']}"


def test_the_english_side_has_no_chinese_in_it() -> None:
    """`en=` 参数和 `{"en": …}` 里不许有汉字 —— 写了 `en=` 却抄了一遍中文，前面几条全绿，
    英文界面上照样是中文（前端那道闸 2026-09-16 就撞过一次同样的事）。"""
    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            english: list[ast.AST] = []
            if isinstance(node, ast.Call):
                english += [k.value for k in node.keywords if k.arg == "en"]
            if isinstance(node, ast.Dict):
                english += [v for k, v in zip(node.keys, node.values)
                            if isinstance(k, ast.Constant) and k.value == "en"]
            for value in english:
                for child in ast.walk(value):
                    if (isinstance(child, ast.Constant) and isinstance(child.value, str)
                            and HAN.search(child.value)):
                        offenders.append(f"{path.relative_to(APP.parent)}:{child.lineno} → {child.value[:60]}")
    assert not offenders, "英文那一侧写着中文：\n" + "\n".join(offenders)


#: 存进事件、之后才给人看的话。写的那一刻不知道将来谁用哪种语言看，所以两种都写。
STORED_SAYINGS = {"consequences", "changes"}


def test_what_is_stored_for_a_person_to_read_later_is_written_in_both_languages() -> None:
    """环境横幅上那几句（`consequences`、修复提案的 `changes`）跟着事件存下来，之后才显示 ——
    存一种语言就等于替所有将来的读者定了语言（2026-10-20：英文界面的横幅上是一行中文）。
    worker（`platform_runtime.py`）和后端两边都扫。"""
    sources = sorted(APP.rglob("*.py"))
    runtime = APP.parents[2] / "platform_runtime.py"
    if runtime.is_file():
        sources.append(runtime)
    offenders: list[str] = []
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            values: list[ast.AST] = []
            if isinstance(node, ast.Dict):
                values += [v for k, v in zip(node.keys, node.values)
                           if isinstance(k, ast.Constant) and k.value in STORED_SAYINGS]
            if isinstance(node, ast.Call):
                values += [k.value for k in node.keywords if k.arg in STORED_SAYINGS]
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id in STORED_SAYINGS for t in node.targets):
                values.append(node.value)
            for value in values:
                done = _bilingual_values(ast.Call(func=ast.Name(id="_", ctx=ast.Load()), args=[value], keywords=[]))
                for child in ast.walk(value):
                    if (isinstance(child, ast.Constant) and isinstance(child.value, str)
                            and HAN.search(child.value) and id(child) not in done):
                        offenders.append(f"{path.name}:{child.lineno} → {child.value[:50]}")
    assert not offenders, "存下来给人看的话只写了中文：\n" + "\n".join(offenders)
