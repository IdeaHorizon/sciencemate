"""SVG 轨创作约定的**唯一真相源**（DOM 标记词表）。

## 为什么有这个模块

SVG 轨（illustration 家族）的图由 agent 手写 SVG。下游有**四个机器消费者**
要判读这份 SVG：

1. 合同断言执行器（`figure_contract.evaluate_svg_assertions`）——按标记选元素、
   计数、核度数；
2. `svg_audit` 机械审计——核"该有标记的元素都打了标记"、文字政策、出界；
3. enhancement 的 code2prompt——按 `data-placeholder` 认出占位框才能替换；
4. `skills/svg-illustration/SKILL.md`——教 agent 怎么打标记的人读文本。

四处各写一份词表，漂移只是时间问题（skill 写 `data-kind`、执行器查
`data-role`，断言全挂还查不出原因）。所以词表只许活在这里，SKILL.md 的
约定章节由 `convention_text()` 生成，并有测试把两边钉死（漂移即红）。

思想来自 AutoFigure 的占位符创作约定（灰框占位 + 外部标签 + 框内图标描述）；
机械化载体（DOM 属性、词表、断言）是 postprocess 原生机制 —— AutoFigure
的约定是写给模型读的散文，这里的约定是写给代码读的属性。

## 颜色纪律

节点主形状的角色色由 `figure_contract.role_colors` 框架发色，**fill 或
stroke 之一承载**即可（白底描边的机箱轮廓是合法形态）；vlm_extraction 按
色计数，但证人是 advisory —— 认不出走 unreadable 披露，不判死。
"""

from __future__ import annotations

import re

# ── 六属性词表 ──────────────────────────────────────────────────────────

#: 节点主形状 → 合同节点的 role（开放词表，与编译轨一致）。
ATTR_ROLE = "data-role"
#: 节点主形状 → 合同节点的 id。
ATTR_NODE = "data-node"
#: 连线 → 两端节点 id。拆两个属性、不拼 "a→b" 字符串：与合同 JSON 的
#: from/to 同形，XML 选择友好，不引入脆解析环节。
ATTR_EDGE_FROM = "data-edge-from"
ATTR_EDGE_TO = "data-edge-to"
#: 文字元素 → 它标注的节点 id（文字政策核查 + vlm 指认）。自由文字
#: （标题/图例/边注）不打这个标记，文字政策全局扫描所有 <text>。
ATTR_LABEL = "data-label"
#: 组容器（<g>/外框）→ 合同 group 的 id；合同的边可以指向 group:<id>。
ATTR_GROUP = "data-group"
#: 占位框 → 图标/插画槽位 id。enhancement 缺席时占位框也必须是可交付
#: 形态（角色色块 + 居中短标签）——enhancement 是锦上添花，不是必需工序。
ATTR_PLACEHOLDER = "data-placeholder"

#: 全量词表（审计与测试用）。
ALL_ATTRIBUTES: tuple[str, ...] = (
    ATTR_ROLE,
    ATTR_NODE,
    ATTR_EDGE_FROM,
    ATTR_EDGE_TO,
    ATTR_LABEL,
    ATTR_GROUP,
    ATTR_PLACEHOLDER,
)

#: 可以当"节点主形状"的元素。path 也在内：插画的节点不一定是矩形。
NODE_ELEMENTS: tuple[str, ...] = ("rect", "circle", "ellipse", "polygon", "path", "image")

#: SVG 命名空间（解析时剥、生成时带）。
SVG_NS = "http://www.w3.org/2000/svg"

#: 标记里的 id 取值约束。与 figure_contract._ID_RE 同一规则（那边是合同 id
#: 的真相源）；两边各写一份必然分叉，所以测试里钉住它们一致。
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


def is_valid_marker_id(value: object) -> bool:
    return isinstance(value, str) and bool(ID_RE.match(value))


def convention_text() -> str:
    """SKILL.md 的「DOM 标记约定」章节正文 —— 由词表生成，不许手抄。

    测试钉住：SKILL.md 里必须出现本函数返回的每一个属性名与这条铁律，
    防止教程和词表各说各话。
    """
    return f"""## DOM 标记约定（机械核查，不是建议）

图里每一个有合同身份的元素必须带标记，缺标记即 finding：

- 节点主形状：`{ATTR_ROLE}="<role>"` + `{ATTR_NODE}="<id>"`
- 连线：`{ATTR_EDGE_FROM}="<id>"` + `{ATTR_EDGE_TO}="<id>"`
- 文字标签：`{ATTR_LABEL}="<它标注的节点 id>"`（标题/图例等自由文字不打）
- 组容器：{ATTR_GROUP}="<group id>"
- 占位框（图标/插画槽位）：{ATTR_PLACEHOLDER}="<槽位 id>"

**铁律：合同里声明的每个 id，必须在 SVG DOM 里出现且恰好出现一次。**
declare 时已经枚举过全部 id —— 打标记只是把它们抄进 SVG，不是新增发明。

颜色：节点主形状的角色色用合同返回的 role_colors，fill 或 stroke 其一承载。
占位框在 enhancement 缺席时也必须可直接交付（色块 + 居中短标签）。"""
