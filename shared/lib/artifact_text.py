"""产物正文的**文本视图** —— 一份实现，所有把 content 当文本用的地方都调它。

## 现场

`content` 声明是字符串，但工具参数来自 LLM 的 JSON，模型给一个对象是常事，
框架也照原样存了（结构化产物本来就该允许是对象）。于是每个"把 content 当文本
用"的地方都成了一颗雷：

    TypeError: expected string or bytes-like object, got 'dict'
        （scan_artifact_disagreements —— 整个 curator 扫描当场炸掉）

`read_artifact` 工具早就写对了：

    text = content if isinstance(content, str) else json.dumps(content, …)

但那一行是**内联的抄件**，另外五处读 content 的地方没有它。同一个问题有几份
抄件就有几个会各自演化的答案，且分叉时两边都不报错（[[一个问题一个真相源]]）。

## 为什么是 dumps 而不是跳过

结构化产物里照样可能写着 "I disagree with claim_x"、可能有公式、有引用。
跳过 = 那些检查对结构化产物**静默失效**，比报错更糟：没人会发现。
"""
from __future__ import annotations

import json
from typing import Any


def as_text(value: Any) -> str:
    """任意值的文本形式。``None`` → ``""``，字符串原样，其余走 JSON。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def artifact_text(record: Any) -> str:
    """**产物记录**的正文文本。

    ⚠️ 参数是记录，不是正文。第一版这个函数两样都收（`record if dict else
    value`）—— 而 content 本身就可以是 dict，于是传正文进来会被当成记录去取
    它的 `content` 键，静默返回空串。写入面因此把产物存成了空的，一声不吭。

    要转裸值用 `as_text`。一个函数只回答一个问题。
    """
    if not isinstance(record, dict):
        return as_text(record)
    return as_text(record.get("content"))
