"""会话短标题走桥，而且模型不听话的时候不会把解释挂成标题。

## 现场

会话标题一直是用户第一条消息的机械截断。一条真实的科研指令是一整段话，截出来
的标题在侧边栏占四行还看不出这是什么课题 —— 标题该回答"这是哪个课题"，截断
回答的是"这段话开头是什么"。

## 为什么模型调用在这一侧

App Server 全流程一次模型调用都没有：provider 分支、重试、超时、密钥解析只
存在于 `core.llm.LLMClient`。为一个标题在那边再实现一遍 HTTP 调用，就是又一处
会各自演化的 provider 逻辑。所以桥上加 `op=name_session`，App Server 只负责
判断该不该命名、把凭据交进来、把结果写回库。

## 这组测试守什么

**不是**"模型能起好标题"（那验不了，也不该由测试来保证）。守的是它不听话时
这一层的兜底：加引号、写成"标题：xxx"、结尾带句号、以及干脆写了一段解释 ——
最后一种必须返回空，让调用方保留机械标题。截断一段解释只会得到半句话，而
半句话会被当成标题永久挂在那儿。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from platform_runtime import _naming_prompt, _SESSION_TITLE_MAX_CHARS, clean_session_title

REPO = Path(__file__).resolve().parents[1]


class TestTitleCleanup:
    def test_a_plain_title_passes_through(self):
        assert clean_session_title("英国饮食文化匮乏之因") == "英国饮食文化匮乏之因"

    def test_surrounding_quotes_are_removed(self):
        assert clean_session_title('"英国饮食文化研究"') == "英国饮食文化研究"
        assert clean_session_title("「英国饮食文化研究」") == "英国饮食文化研究"
        assert clean_session_title("《英国饮食文化研究》") == "英国饮食文化研究"

    def test_a_label_prefix_is_removed(self):
        assert clean_session_title("标题：英国饮食文化") == "英国饮食文化"
        assert clean_session_title("Title: British Food Culture") == "British Food Culture"

    def test_a_prefix_inside_quotes_is_still_removed(self):
        """前缀和引号会同时出现，顺序不能让其中一个漏网。"""
        assert clean_session_title('标题：\n"英国饮食文化"') == "英国饮食文化"

    def test_punctuation_outside_the_closing_quote_is_removed(self):
        """回归：`「标题」。` —— 句号在引号**外面**。

        2026-08-18 接上真实链路跑第一次就撞到了这个形态。当时的清洗只剥一遍：
        `strip` 剥引号时撞上末尾的 `。` 就停住，右引号原样留下；随后 rstrip
        标点也只吃掉句号。落库的标题是「英国饮食文化匮乏之因」少了左引号、
        多了个孤零零的 `」`。

        引号和句号各自的单测当时都是绿的 —— 它们恰好没被套在一起验过。
        """
        assert clean_session_title("「英国饮食文化匮乏之因」。") == "英国饮食文化匮乏之因"
        assert clean_session_title('"British food"?') == "British food"
        # 多层包裹也要剥干净。
        assert clean_session_title("《「英国饮食」》。") == "英国饮食"

    def test_trailing_punctuation_is_removed(self):
        assert clean_session_title("英国饮食文化为何被视为匮乏。") == "英国饮食文化为何被视为匮乏"
        assert clean_session_title("Why British food?") == "Why British food"

    def test_newlines_collapse_instead_of_leaking_into_the_title(self):
        assert clean_session_title("英国饮食\n文化") == "英国饮食 文化"

    def test_an_explanation_yields_no_title_at_all(self):
        """收拾完还这么长 = 它没在起标题，是在解释。

        这里**必须**返回空而不是截断：截一段解释得到的是半句话，而那半句话
        会作为标题永久挂在会话上。返回空则保留机械标题，下一轮还会再试。
        """
        explanation = (
            "好的，我来帮你分析一下这个问题。这个研究课题涉及英国饮食文化的"
            "历史演变，需要从工业革命、两次世界大战等多个角度来考察，因此我"
            "建议将标题定为英国饮食文化研究。"
        )
        assert clean_session_title(explanation) == ""

    def test_a_slightly_long_title_is_trimmed_not_discarded(self):
        """略超上限的**确实是标题**，截一下就好 —— 别和解释混为一谈。"""
        slightly_long = "英" * (_SESSION_TITLE_MAX_CHARS + 4)
        result = clean_session_title(slightly_long)
        assert len(result) == _SESSION_TITLE_MAX_CHARS

    def test_an_answer_to_the_user_is_not_a_title(self):
        """回归（2026-09-10 真机）：模型把用户那句话**当问题回答了**。

        用户第一条消息是「用一句话说说你能帮我做什么」，模型回了一段自我介绍。
        它落在 24～48 字之间，躲过了 `> MAX*2` 那道闸，被截成 24 字挂上去：

            '我可以帮你解答问题、写作、翻译、编程、整理信息、'

        半句话，还以顿号收尾。**截断在这里是有害操作** —— 它把「模型没听话」
        变成了一个看起来像标题的东西，而看起来像的错答案没人会去查。

        触发用例用的就是真出过问题的那段话（不是另造一句）。
        """
        answered = "我可以帮你解答问题、写作、翻译、编程、整理信息、分析数据"
        assert clean_session_title(answered) == "", "一段并列的解释被截成半句话挂上去了"

    def test_a_list_of_clauses_is_never_a_title(self):
        """一串并列短语＝在解释，不是在起标题。"""
        cut_here = "第一步、第二步、第三步、第四步、第五步、第六步、第七步"
        assert clean_session_title(cut_here) == ""

    def test_a_stray_trailing_clause_mark_is_just_cleaned_off(self):
        """尾巴上孤零零的顿号收拾掉就好 —— 那仍然是个好标题，别整条丢掉。"""
        assert clean_session_title("英国饮食文化、") == "英国饮食文化"

    def test_an_empty_answer_yields_an_empty_title(self):
        assert clean_session_title("") == ""
        assert clean_session_title("   \n  ") == ""


class TestTheMessageIsMaterialNotAQuestion:
    """用户那句话是**要被起名的素材**，不是一条对模型说的话。

    真机上模型顺着回答了用户的问句（见 `test_an_answer_to_the_user_is_not_a_title`）。
    根因是那句话此前以 `role="user"` 原样送出 —— 对模型来说就是有人在问它。
    这和 prompt 注入同形：数据被读成了指令。
    """

    def test_the_users_words_are_delimited_as_material(self):
        prompt = _naming_prompt("用一句话说说你能帮我做什么")
        assert "用一句话说说你能帮我做什么" in prompt
        before, _, after = prompt.partition("用一句话说说你能帮我做什么")
        assert "<<<MESSAGE" in before and "MESSAGE>>>" in after, (
            "用户的话没有被定界符围起来 —— 模型看到的就是一条对它说的话")
        assert "不要回答它" in before, "没有明说「这不是要你回答的」"

    def test_the_op_hands_the_model_the_wrapped_prompt_not_the_raw_message(self) -> None:
        """写了没接线等于没有：判据落在**真调用**上（AST），不是措辞。"""
        import ast
        source = (REPO / "platform_runtime.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_name_session")
        body = ast.unparse(fn)
        assert "_naming_prompt(excerpt)" in body, (
            "run_name_session 又把 excerpt 裸着当 user 消息送出去了")
        assert "content=excerpt" not in body


class TestBridgeOp:
    def test_the_op_reaches_the_model_call_instead_of_crashing_earlier(self):
        """请求解析 / 导入 / 消息构造这几步真的走通了。

        判据是它**卡在网络上**：那说明前面每一步都过了，只差连得上模型。
        不钉死具体异常名 —— `LLMClient` 会按重试策略把连接失败包成
        `LLMHTTPError` 或原样抛 `ConnectError`，两者都证明同一件事，而钉死
        其中一个只会让这条测试在重试策略变动时红得没有意义。

        这条测试不需要真模型，也就不会因为没配 key 而假绿。
        """
        request = {
            "op": "name_session",
            "request_id": "t1",
            "project_id": "p1",
            "home_dir": str(REPO / ".tmp" / "naming-home"),
            "message": "你帮我研究一下英国饮食文化为何被视为匮乏",
        }
        (REPO / ".tmp" / "naming-home").mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [sys.executable, "-m", "platform_runtime"],
            input=json.dumps(request, ensure_ascii=False),
            capture_output=True,
            text=True,
            cwd=REPO,
            env={
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": str(REPO),
                # 127.0.0.1:9 是 discard 端口：必然拒连，且不会真发出请求。
                "LLM_BASE_URL": "http://127.0.0.1:9",
                "LLM_API_KEY": "not-a-real-key",
                "LLM_MODEL": "not-a-real-model",
                "LLM_TIMEOUT": "5",
                # 不重试：这里要验的是"够得到模型调用"，不是重试策略。
                # 留着默认重试，这一条要跑 85 秒。
                "LLM_MAX_RETRIES": "0",
                "LLM_RETRY_BUDGET_SECONDS": "1",
            },
            timeout=120,
        )
        events = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.strip().startswith("{")
        ]
        assert events, f"桥没有输出任何事件；stderr={result.stderr[:500]}"
        assert events[-1]["type"] == "error"
        assert events[-1]["error_type"] in {"ConnectError", "LLMHTTPError"}

    def test_a_request_without_a_message_is_rejected_by_name(self):
        """报错要说清是哪个字段，不能是一条被包了两层的通用错误。"""
        result = subprocess.run(
            [sys.executable, "-m", "platform_runtime"],
            input=json.dumps({"op": "name_session", "request_id": "t2", "message": "  "}),
            capture_output=True,
            text=True,
            cwd=REPO,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(REPO)},
            timeout=120,
        )
        events = [
            json.loads(line)
            for line in result.stdout.splitlines()
            if line.strip().startswith("{")
        ]
        assert events[-1]["code"] == "invalid_message"
