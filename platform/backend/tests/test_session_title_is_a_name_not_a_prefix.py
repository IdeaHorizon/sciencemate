"""会话标题要回答"这是什么课题"，不是"这段话开头是什么"。

## 现场（2026-08-18）

一条真实的科研指令是一整段话：

    你帮我研究一下，为什么英国的饮食文化，包括英国菜，现在基本上就是匮乏的
    代名词……然后我要求你输出一份非常严谨的论文，来阐述英国饮食文化。

平台把它整段塞进标题。侧边栏里它占掉四行还看不出是什么课题，顶栏里它被 CSS
截成半句话。标题这个位置本来是用来在一堆会话里认出这一个的，塞进去的却是原文。

## 这组测试守的是什么

真正难的不是"生成一个短标题"，是**判断该不该覆盖**。用户手挑的名字一旦被
自动命名盖掉，就是平台在破坏用户的东西 —— 所以判据必须是逐字的、可判的，
而不是"看起来像自动生成的"这类猜测。

判据现算、不落 `title_source` 字段：存一个标记，规则一改（截断长度、省略号
形态）库里那份标记就和事实对不上，而且不会报错。

第二条：平台**已知产出过两种**机械标题（`mechanical_session_title` 的 80 字符
收敛形态，和更老的建会话路径留下的裸 `[:300]`）。只认一种，另一种就会被当成
"人挑的"永远不动 —— 而那恰好是绝大多数存量会话的样子，也就是本次要修的那批。
"""
from __future__ import annotations

from app.services.sessions import (
    PLACEHOLDER_SESSION_TITLES,
    machine_made_title_forms,
    mechanical_session_title,
    session_title_is_machine_made,
)

LONG_REQUEST = (
    "你帮我研究一下，为什么英国的饮食文化，包括英国菜，现在基本上就是匮乏的"
    "代名词，大家往往对其评价不高。这背后反映的是什么？然后英国的饮食文化的"
    "发展脉络是怎么样的？它受到比如说工业革命啊，或者说其他的世界大战等等"
    "影响大吗？然后我要求你输出一份非常严谨的论文，来阐述英国饮食文化。"
)


class _Session:
    """只需要 `.title` —— 判据不碰别的字段。"""

    def __init__(self, title: str) -> None:
        self.title = title


def test_the_model_timeout_stays_below_the_subprocess_timeout():
    """内层模型超时必须严格小于外层子进程超时。

    2026-08-18 真机实测：GPUStack 上的 deepseek-v4-pro 起一个标题要 47 秒
    （harness 导入只占 0.07 秒，时间全在模型上 —— 推理模型起标题也先想一轮），
    直接撞穿了当时 45 秒的外层超时。

    调大数字是一回事，**两个超时的相对关系**是另一回事：内层不小于外层，就
    永远是子进程先被杀，调用方拿到的是一句"timed out"，而不是 harness 那条
    说得清是连不上、被拒、还是真的慢的错误。谁以后单独调其中一个，这条会红。
    """
    from app.services import session_naming

    assert session_naming._MODEL_TIMEOUT_SECONDS < session_naming._TIMEOUT_SECONDS
    # 也要留得下真实观测到的耗时，别调回一个必然超时的值。
    assert session_naming._MODEL_TIMEOUT_SECONDS >= 60


def test_placeholder_titles_are_machine_made():
    for placeholder in PLACEHOLDER_SESSION_TITLES:
        assert session_title_is_machine_made(_Session(placeholder), LONG_REQUEST)


def test_the_current_mechanical_truncation_is_recognised():
    title = mechanical_session_title(LONG_REQUEST)
    assert session_title_is_machine_made(_Session(title), LONG_REQUEST)


def test_the_legacy_bare_truncation_is_also_recognised():
    """建会话那条老路径存的是裸 `[:300]`：不收敛空白、不加省略号。

    存量会话绝大多数是这个形态。漏掉它，这次改造对**已经存在的会话**完全
    不生效 —— 而那正是用户看着的那批。
    """
    legacy = LONG_REQUEST.strip()[:300]
    assert session_title_is_machine_made(_Session(legacy), LONG_REQUEST)


def test_a_message_with_newlines_produces_two_distinct_forms():
    """两种形态**确实不同**，所以两条都得认。

    收敛空白的那条把换行变成空格，裸截断那条原样保留。这个测试是上一条测试
    的前提：如果两种形态永远相等，那条测试就什么都没证明。
    """
    message = "第一行\n第二行\n第三行"
    forms = machine_made_title_forms(message)
    assert len(forms) == 2
    assert "第一行 第二行 第三行" in forms
    assert "第一行\n第二行\n第三行" in forms


def test_a_hand_picked_title_is_never_machine_made():
    """人挑的名字不许被自动命名碰。"""
    assert not session_title_is_machine_made(_Session("英国饮食文化"), LONG_REQUEST)


def test_a_generated_title_is_not_machine_made_so_naming_happens_once():
    """命名写进去之后，标题不再等于任何机械形态 —— 于是不会被再命名一次。

    "只命名一次"由判据本身保证，不需要另存一个"已命名"标记。
    """
    generated = "英国饮食文化匮乏之因"
    assert not session_title_is_machine_made(_Session(generated), LONG_REQUEST)


def test_an_empty_title_is_not_treated_as_machine_made():
    """空标题不是"平台写的"，是坏数据。拿它当机械标题会让空消息也触发命名。"""
    assert not session_title_is_machine_made(_Session(""), LONG_REQUEST)
    assert not session_title_is_machine_made(_Session("   "), LONG_REQUEST)


def test_mechanical_title_collapses_whitespace_and_bounds_length():
    assert mechanical_session_title("  a\n\n  b  ") == "a b"
    long = "x" * 200
    title = mechanical_session_title(long)
    assert len(title) == 80
    assert title.endswith("...")


def test_mechanical_title_of_an_empty_message_is_empty():
    """空消息不该产出一个标题 —— 更不该产出 `...` 这种纯省略号。"""
    assert mechanical_session_title("") == ""
    assert mechanical_session_title("   \n  ") == ""
    assert machine_made_title_forms("   ") == set()
