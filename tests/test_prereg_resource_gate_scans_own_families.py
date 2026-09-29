"""预注册可行性闸不能只认手写的「别人家模型」名单。

## 病例（2026-08-22，E2E v28 现场取证时查出）

`_prereg_feasibility_violations` 的职责是：**预注册不得承诺平台没有的资源**。
它的实现是拿正文去撞一份手写常量：

    _KNOWN_EXTERNAL_MODELS = ("gpt-4o", ..., "claude", "gemini", "grok",
                              "llama", "mistral", "mixtral", "qwen")

**平台自己的主力模型族 `deepseek` 不在里面。** 于是 prereg 写一个
`deepseek-v9-ultra`（根本不存在的型号）能一路冻结通过 —— 而这正是这道闸唯一
要拦的东西。硬编码枚举 = 新东西默认漏过（同一个病今天已经在扫盘闸上栽过一次）。

## 修法：族名从已登记 providers 现推

手写名单只留给**别人家**的模型；平台自己那些族由 `list_providers()` 现推，
配置变了自动跟着变，不用回来改名单。

## 这道闸误伤的代价比漏过更大

漏过 = 一份 prereg 承诺了不存在的资源；误伤 = **冻结被拒、流水线卡死**，正是
这个平台反复栽的那个病。所以只扫「族名 + 版本段」（`deepseek-v4-pro`），不扫
裸族名 —— "本平台基于 deepseek" 这类叙述必须放行。下面两类都各有测试守着。
"""
from __future__ import annotations

import pytest

from shared.tools.library import artifacts_extra as ax


class _Spec:
    def __init__(self, name: str, model: str) -> None:
        self.name, self.model = name, model


@pytest.fixture()
def platform_on_deepseek(monkeypatch):
    """假装平台登记的是 deepseek-v4-flash。"""
    monkeypatch.setattr(
        "core.llm_providers.list_providers",
        lambda: [_Spec("deepseek-v4-flash", "deepseek-v4-flash")],
    )


def _violations(content: str, commitment: dict | None = None) -> list[str]:
    record = {"content": content, "metadata": {"execution_commitment": commitment or {}}}
    return ax._prereg_feasibility_violations(record)


def test_a_fabricated_variant_of_our_own_family_is_caught(platform_on_deepseek) -> None:
    """原始病例：编造一个本族型号，必须被拦。"""
    found = _violations("用 deepseek-v9-ultra 做 LLM-as-judge 打分。")
    assert found, "编造的本族型号被放行了 —— 这道闸就是为拦它而存在的"
    assert "deepseek-v9-ultra" in found[0]


def test_the_error_says_what_is_actually_registered(platform_on_deepseek) -> None:
    """报错要给出真正的下一步，不能只说不行。

    这条不只是文案：中途换模型后端时，模型读到的资源登记是冻在 run 稳定前缀里
    的旧值，而闸读的是**当下活的** providers。两边不一致时，如果报错只说
    「你承诺了平台没有的资源」，模型看到的就是「框架先叫我写 X、又because我
    写了 X 罚我」—— 它会去找别的缝钻（今天已经因此赔过一次：#617）。
    所以必须把「登记现在是什么」摊开。
    """
    found = _violations("用 deepseek-v9-ultra 打分。")
    assert "deepseek-v4-flash" in found[0], "报错必须列出当前登记的模型"
    assert "substitutes" in found[0], "必须给出合法出口"


def test_the_registered_model_itself_passes(platform_on_deepseek) -> None:
    """收紧不能变成拦死：点名登记里那个，必须放行。"""
    assert not _violations("用 deepseek-v4-flash 做 LLM-as-judge 打分。")


def test_a_bare_family_mention_is_not_a_commitment(platform_on_deepseek) -> None:
    """裸族名不算承诺 —— 误伤在这里等于死锁，宁可漏也不能乱开火。

    ⚠️ 这条**不区分**「靠正则的 `+`」还是「靠 available 子串检查」：派生族名
    必然是某个已登记型号的前缀，所以两条机制都会让它过。把 `+` 变异成 `*` 这条
    照样绿 —— 我试过，写在这里免得后人误以为它在守正则。
    它守的是**行为**（裸族名不得开火），不是某一处实现。
    """
    assert not _violations("本平台基于 deepseek 系列模型运行，无需额外 API。")


def test_declaring_a_substitute_still_releases_the_gate(platform_on_deepseek) -> None:
    """既有的逃逸口不能被顺手改坏。"""
    assert not _violations(
        "理想情况用 deepseek-v9-ultra。",
        {"substitutes": "deepseek-v9-ultra 不可用时改用 deepseek-v4-flash"},
    )


def test_foreign_models_are_still_caught(platform_on_deepseek) -> None:
    """别人家的模型仍走手写名单，这半边不能丢。"""
    found = _violations("用 gpt-4o 做标注。")
    assert found and "gpt-4o" in found[0]


def test_the_family_list_is_derived_not_hand_written(monkeypatch) -> None:
    """换一家 provider，扫描范围要自动跟着走 —— 这才叫现推。"""
    monkeypatch.setattr(
        "core.llm_providers.list_providers",
        lambda: [_Spec("kimi-k3", "kimi-k3")],
    )
    found = _violations("用 kimi-k9-max 打分。")
    assert found and "kimi-k9-max" in found[0], (
        "换成 kimi 之后没有自动覆盖 kimi 族 —— 说明族名还是写死的"
    )
    # 且此时 deepseek 不再是本平台的族，裸提不该开火
    assert not _violations("对比文献里提到的 deepseek-v4-pro 结果。")
