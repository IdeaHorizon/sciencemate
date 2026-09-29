"""平台只有一套模型接入 —— harness。

## 为什么后端不能自己再持一份

App Server 从前有 `app/llm/`（1104 行）：五个 provider、一个 router、一套重试与
超时。同一件事 harness 那边有一份完整的（`core.llm.LLMClient`），而两处必然
各自演化：凭据解析、base_url 归一化、超时语义、错误分类 —— 每一项分叉时两边
都不报错，只是在某个具体请求上给出不同的行为。

删的时候发现它的生产消费者只剩一个：`app/core/intake.py` 的访谈引擎，而那两个
端点在**前端零调用方**。所以这是一次纯删除，不是搬家 —— 一个没有调用方的功能，
搬到桥上只是把没人走的路挪了个地方。

## 会话命名怎么办

它一直就在桥上（`session_naming` 的 `op=name_session`）。那是这一层唯一真正需要
模型的东西，也是它该在的地方。
"""
from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

#: 例外：为空。App Server 不许再有第二份模型接入。
ALLOWED_LOCAL_LLM: set[str] = set()


def test_the_backend_owns_no_llm_client() -> None:
    assert not (APP / "llm").exists(), "app/llm 又回来了 —— 平台只有一套模型接入"


def test_nothing_imports_a_local_llm_module() -> None:
    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        if str(path.relative_to(APP)) in ALLOWED_LOCAL_LLM:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            module = ""
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            elif isinstance(node, ast.Import):
                module = ",".join(alias.name for alias in node.names)
            if "app.llm" in module:
                offenders.append(f"{path.relative_to(APP)}:{node.lineno}")
    assert not offenders, (
        f"这些地方 import 了后端自己的 LLM 客户端：{offenders}。"
        "模型调用走 harness 那座桥（见 services/session_naming.py 的 op=name_session）。"
    )


#: 自己发 HTTP 的那些库。这一层一个都不许用来打模型 —— 打模型只许经桥。
OWN_HTTP_MODULES = {"httpx", "requests", "aiohttp", "openai", "anthropic",
                    "http.client", "urllib.request", "urllib3"}

#: 到桥的合法接法：自己起 `platform_runtime`，或者调收口后的一次性桥调用。
WAYS_TO_REACH_THE_BRIDGE = ("platform_runtime", "harness_bridge_once")


def test_the_only_model_call_in_this_layer_goes_through_the_bridge() -> None:
    """这一层唯一真正需要模型的东西（会话命名）走的是桥，不是自己的 HTTP。

    ## 判据为什么不是 `"platform_runtime" in source`

    那是**查名字出现**，不是查接线。2026-09-10 一次性桥调用收口到
    `harness_bridge_once` 之后，`session_naming` 里不再有那个字面量 —— 接线一点
    没变（模型调用照样在桥的子进程里），判据却红了。反过来更糟：这个写法对
    「在注释里提一句 platform_runtime、同时自己 `httpx.post` 打模型」是绿的。

    所以这里问两件真事：**它有没有到桥**（自己起子进程 或 调收口那处都算），
    以及**它有没有自己发 HTTP**。后者才是这条规矩真正要守的，而它原先没人守。
    """
    path = APP / "services" / "session_naming.py"
    source = path.read_text(encoding="utf-8")
    assert '"op": "name_session"' in source, "命名不再走 op=name_session —— 模型接入跑到别处了？"

    tree = ast.parse(source)
    code = "\n".join(ast.unparse(node) for node in tree.body)
    assert any(way in code for way in WAYS_TO_REACH_THE_BRIDGE), (
        f"会话命名没有任何一条到桥的路（{WAYS_TO_REACH_THE_BRIDGE}）—— "
        "它要么自己实现了模型调用，要么这条链断了")

    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        else:
            continue
        imported += [n for n in names if n.split(".")[0] in
                     {m.split(".")[0] for m in OWN_HTTP_MODULES}]
    assert not imported, (
        f"{path.name} 自己引了 HTTP 客户端 {imported} —— 模型调用只许经桥"
        "（provider 分支/重试/超时/凭据解析在 core.llm 里只有一份）")


def test_the_interview_engine_is_gone_with_its_endpoints() -> None:
    """引擎和它的两个端点一起走 —— 留一个没有入口的引擎只是留一份会腐烂的代码。"""
    assert not (APP / "core" / "intake.py").exists()
    # 按**路由**判，不按文字判：解释「为什么这两个端点没了」的那段注释里必然
    # 写着它们的路径，而它恰恰是在守这条规矩（2026-09-05 第一版就这么误报的）。
    tree = ast.parse((APP / "api" / "v1" / "projects.py").read_text(encoding="utf-8"))
    routes: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not decorator.args:
                continue
            first = decorator.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                routes.append(first.value)
    assert not [path for path in routes if "intake" in path], routes
