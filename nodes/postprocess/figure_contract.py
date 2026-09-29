"""图合同（figure contract）—— 「要求」和「像素」之间唯一的中间真相源。

## 病（2026-09-16 实证，project `astra` 8 版渲染）

出图这条路上，所有**检查**都在看像素，所有**要求**都在自由文本里，两边从来
没有对过账：

- 审图 rubric 的 `forbidden_vlm_outputs` 第一条就是 `scientific correctness`，
  prompt 写死 "do not infer … source fidelity" —— 它只审「渲染有没有坏」。
- 于是 agent 朝着「不重叠不裁切」一路优化：第 1 版每个 PCIe Switch 扇出 4 条
  线到 4 张 GPU（内容对、排版烂），第 8 版排版干净了，**4 条线塌成 1 条**，
  上联从机箱边框而不是网卡引出，两颗 CPU 全程一条线都没有。
- 没有任何东西比较第 N 版和第 N-1 版，所以这种回归不可见。

根因不是「VLM 不够强」也不是「prompt 没写清楚」：是**图里的结构性事实从来
没有被写下来过**。没写下来的东西，既不能被机械核对，也不能跨版本比较，更
不能在渲染时被保证。

## 药：合同先于像素，且合同就是渲染源

一份 figure contract 是 agent 在写任何渲染代码之前声明的、机械可核的事实集。
两种家族、两条落地路径，同一个概念：

- **compiled 家族**（`schematic`）：合同**就是渲染源**。框架按声明的
  nodes/edges/bands 算几何、画图。声明了 8 条边就画 8 条边 —— 图和声明
  不可能分叉，因为它们是同一个东西。「忘了画一条线」这类缺陷在架构上消失。
- **asserted 家族**（`quantitative` / `composite`）：统计图用代码画更合适，
  所以合同不当渲染源，而是**断言**：渲染后读 matplotlib 的对象模型
  （`fig.axes` / `ax.lines` / `get_yscale()`…）逐条核对。分叉被检测出来。

两条路都落在同一条链上：**要求 → 断言 → 图 → 像素**，每一环机械地扣住下
一环。

## 为什么「合同不符」可以拒绝，而这不是判决复辟

[[project_verdict_demolition]] 拆掉的是「够不够发表」这类**质量判决**：那归
referee 与用户，不该由铸记录时算出的字段代答。

合同核查问的是另一个问题：**你声明的和你做出来的是不是同一件事。** 声明
「每张 GPU 一条边」却画出一条线，这不是「质量不够好」，是**自相矛盾** ——
和 figure_hash 对不上时拒绝录入是同一类机械事实。所以：

- 合同结构不自洽 / 断言不成立 → 拒绝铸记录（`VisualContractError`）；
- 「这张图够不够好看 / 够不够发表」→ 仍然没有任何字段回答，仍归 referee。

削弱合同（删条目、放松断言）逃不掉：合同逐版冻结，`contract_diff()` 把
「上一版有、这一版没有」当作回归如实记账（见 tools/figure.py 的回归闸）。

## Owner 说明

`nodes/postprocess/` 的 owner 是 cuikl。本模块由架构组（wangd 指派）在
2026-09-16 按「老板点名代办」例外实现，不是 owner 本人的声明；判断依据写在
各函数的注释里，owner 有权直接改本文件。
"""

from __future__ import annotations

import re
from typing import Any

from .contracts import VisualContractError, hash_json, require_dict, slug

#: 合同 schema 版本。变更即记录里可读（figure 记录带 contract.schema_version）。
CONTRACT_SCHEMA_VERSION = "1.0"

#: 「合同即渲染源」的家族：框架按合同算几何、画图，图与声明不可能分叉。
COMPILED_FAMILIES = frozenset({"schematic"})

#: 「合同即断言」的家族：agent 写渲染代码，框架读对象模型逐条核对。
ASSERTED_FAMILIES = frozenset({"quantitative", "composite"})

#: 目前不受合同管辖的家族（显微/三维/分子等像素型产物，结构事实不在图里）。
#: 它们走原路径：机械审计 + VLM 证人，零行为变化。
CONTRACT_FAMILIES = COMPILED_FAMILIES | ASSERTED_FAMILIES

#: asset_kind → 合同家族。词表与校验同源（contracts.ASSET_KINDS 是上游真相源）。
_ASSET_KIND_FAMILY: dict[str, str] = {
    "schematic": "schematic",
    "quantitative": "quantitative",
    "composite": "composite",
}

#: 节点角色 → 色盲安全填色。角色词表开放（任何字符串都合法），但**框架发色**
#: —— 每个 agent 自己编一套配色是 v1 实测里配色不一致的唯一来源。
#: ── 这张图在哪儿被读 ────────────────────────────────────────────────────
#:
#: 一张科学图的外观是被**复现尺寸**决定的，这是设计师问的第一个问题，而我们
#: 从来没问过。`figsize=(W/72, H/72)` —— 画布的 pt 数就是物理英寸数，校准集
#: 那张是 **422mm 宽**。塞进期刊单栏（85mm）时，11pt 的节点标签变成 **2.2pt
#: （0.8mm）**，谁也读不了；整页也只有 4.4pt。期刊普遍要求终尺寸 ≥5–7pt。
#:
#: **46 轮里没有任何判据、任何一轮问过「印出来还读得了吗」。**
#:
#: `width_mm` 是该媒介的典型复现宽度，`floor_pt` 是终尺寸下的最小字号下限。
#: 两个数都保守取值 —— 这条判据宁可漏报，也不能变成每张图都响的噪声
#: （一条被驳回十二次的提示会教会模型「findings 可以不理」）。
#: 每个媒介给图的**版心**（宽 × 高，mm）+ 终尺寸下节点标签的下限。
#: 高度必须一起给：排版师会把横图转过来放，只比宽度等于假设它永远竖着摆 ——
#: 而我们的图恰恰都是横的。少了这一维，判据会把一批好图判成读不了。
#:
#: 2026-09-18 iter11 改成**印刷版心**的名字（single_column 84mm / double_column
#: 170mm）：写作节点就是按这两个词发政策的（constraints.width），而这边过去叫
#: page / column，两套词各说各的，政策到了也对不上。高度取 200mm = A4 版心
#: 250mm × 0.8 —— 写作侧 `\includegraphics[height=0.8\textheight]` 的上限；
#: 六面板竖排那张 478×798pt 就是被这一维压到 5.6pt 的（宽度本来够）。
#: 印刷下限 7pt 是写作侧 H9/H12 的尺子（期刊普遍 6–8pt），不是这边另拍的数。
MEDIA: dict[str, dict[str, float]] = {
    "single_column": {"w": 84.0, "h": 200.0, "floor_pt": 7.0},   # 期刊单栏
    "double_column": {"w": 170.0, "h": 200.0, "floor_pt": 7.0},  # 通栏 / A4 版心
    "slide": {"w": 254.0, "h": 143.0, "floor_pt": 10.0},         # 16:9 幻灯
    "poster": {"w": 594.0, "h": 841.0, "floor_pt": 16.0},        # A1
}
#: 会上纸的那两个版心 —— 印刷字号下限对它们生效；幻灯/海报各有自己的下限。
PRINT_MEDIA: frozenset[str] = frozenset({"single_column", "double_column"})


def fit_scale(canvas_w_mm: float, canvas_h_mm: float, medium: str) -> float:
    """这张图在该媒介的版心里能放多大：宽、高各算一个，取小的。

    第一版这里还试过「转过来放」取大的那个 —— 排版师会把横图转 90° 是纸媒
    的常识，但我们的消费方是写作节点的 `\\includegraphics[width=…]`，它从不
    旋转；写作侧的尺子（H9）也不旋转。判据必须等于真正的问题：这张图**照
    现在的放法**印出来多大。旋转是一个调用方可以另外做的决定，不是默认。
    """

    spec = MEDIA[medium]
    if canvas_w_mm <= 0 or canvas_h_mm <= 0:
        return 1.0
    return min(spec["w"] / canvas_w_mm, spec["h"] / canvas_h_mm)

#: 调用方给的 purpose → 媒介。**不新增第二个真相源**：调用方在
#: `constraints.width` 里明说的优先；没说时按 purpose 推；作者只在两者都
#: 缺席时才用合同里的 `medium` 表态。
PURPOSE_MEDIUM: dict[str, str] = {
    "presentation": "slide",
    "publication": "double_column",
    "report": "double_column",
    "exploration": "double_column",
}
DEFAULT_MEDIUM = "double_column"


def medium_of(contract: dict[str, Any], purpose: str | None = None) -> str:
    declared = str((contract or {}).get("medium") or "").strip().lower()
    if declared in MEDIA:
        return declared
    return PURPOSE_MEDIUM.get(str(purpose or "").strip().lower(), DEFAULT_MEDIUM)


def font_floor_pt(medium: str, caller_min_font_pt: float | None = None) -> float:
    """该版心下图内文字的字号下限：版心自带一个，调用方可以只往上提。"""

    floor = float(MEDIA[medium]["floor_pt"])
    if caller_min_font_pt is not None:
        floor = max(floor, float(caller_min_font_pt))
    return floor


#: 数据图（quantitative / composite）一张图最多几个面板。iter11 实测 8 面板
#: 在 170mm 下刻度 5.2pt；4 面板是「一栏两行」还读得了的上限。超过就是两张图。
MAX_PANELS = 4

_AXIS_SCALES = ("linear", "log", "symlog", "logit")
#: 「这根轴没有单位」要**明说**，不许留空 —— 留空分不清是忘了还是没有。
NO_UNIT = "none"

#: 数据图的学术调色板：色盲安全、低饱和、与示意图的角色配色同一份
#: （ROLE_PALETTE_ORDER），整个项目的图才一致。matplotlib 默认的 tab10
#: （橙蓝 #1f77b4 / #ff7f0e）不在其中 —— 那是「没选过配色」的签名。
DATA_PALETTE: tuple[str, ...] = (
    "#4C72B0", "#55A868", "#C44E52", "#8172B2",
    "#CCB974", "#64B5CD", "#E69F00", "#009E73",
)
#: 参考线、误差棒、网格这类无语义的墨迹可以用中性色。
DATA_NEUTRALS: tuple[str, ...] = ("#000000", "#333333", "#7F7F7F", "#BFBFBF", "#FFFFFF")


ROLE_PALETTE_ORDER: tuple[str, ...] = (
    "#4C72B0", "#55A868", "#C44E52", "#8172B2",
    "#CCB974", "#64B5CD", "#E69F00", "#009E73",
)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")

#: 节点形状档位。**形状本身要表意** —— 一张好的拓扑图里，32 张 GPU 是密排的
#: 小色块、共享织物是一条通栏长条，读者一眼就知道谁是量、谁是骨干。全用同一
#: 种盒子时，8 张 GPU 会占满整幅宽度而信息量并没有增加（2026-09-16 实测）。
#:   box  —— 标准组件盒（默认）
#:   chip —— 密排小色块：批量同类元件（GPU 阵列、通道、样品孔）
#:   bar  —— 通栏长条：共享骨干（交换机、总线、时间轴、公共基线）
NODE_SHAPES: tuple[str, ...] = ("box", "chip", "bar")

#: ── 谁是主角：图 / 地 ───────────────────────────────────────────────────
#:
#: 设计师动笔前的第一件事是定图/地：读者先看哪儿、什么是背景。我们过去**每个
#: 节点一样响** —— 两张参考图里主角（服务器内部 / 交换机）是重的，机箱是虚的。
#:
#: 编码规则不许串台：**色相=类别，亮度=层级，尺寸=分量**。所以强调走的是
#: 亮度与线宽，不动色相 —— 动色相就等于把「它是什么」和「它多重要」混成一件事。
#:
#: 只有三档，而且默认档不写：强调是**相对关系**，需要一个安静的多数做底。
NODE_EMPHASIS: tuple[str, ...] = ("primary", "muted")

#: 版面 block 的种类。
#:
#: 2026-09-17 的根因（同一个形状第四次出现）：**一张好的架构图不是一个 graph
#: 的渲染，是一个版面，里面包含一个 graph。** 之前合同只能表达 graph，于是
#: 所有产出都长成「盒子用线连起来、按行排列」—— 形式雷同不是模型没创意，是
#: 词表里只有这一种形式。
#:
#: 参考图里至少一半的构件 graph 表达不出来：①②③ 分栏标题、机箱虚线框角标、
#: 「上联 RoCE 交换机」这种出框叙事引出、框内文字标注、两栏项目符号的规格摘要。
#:
#:   diagram —— 一张图（现在这套 nodes/groups/bands/edges）
#:   spec    —— 规格摘要：若干条目，可分栏
#:   note    —— 整段说明文字
BLOCK_KINDS: tuple[str, ...] = ("diagram", "spec", "note")

#: 标注层：既不是节点也不是边，但参考图里到处都是。
#:
#:   annotations —— 出框**叙事引出**：从某个组件朝某方向引一支箭头 + 一句话
#:                  （「上联 RoCE 交换机」）。它说的是「这条线去哪儿了」，
#:                  是叙事不是拓扑，所以不该被硬塞成一条边和一个假节点。
#:   nodes[].ports —— 组件边上的端口小块（交换机上 8 个口）。它让「这是一台
#:                  8 口交换机」一眼可见，而不用去数连线。**连到这个组件的线
#:                  会真的落在端口上**，端口不是装饰。
#:   nodes[].detail_of —— 这个盒子是别处那个组的折叠形态（「② 栏的一台服务器」
#:                  就是「① 栏画开的那个机箱」）。写上它，图上多一道内嵌边框
#:                  和一句「详见 ①」，两栏才连得起来。
#:   groups[].style —— 机箱/边界用虚线框，与实体组件框区分开。
ANNOTATION_SIDES: tuple[str, ...] = ("top", "bottom", "left", "right")
#: 立在整摞层旁边的那一列。这类节点不进 bands —— 由框架摆在侧边。
NODE_SIDES: tuple[str, ...] = ("left", "right")

#: 时序：参与者横排一行，消息按 edges[].step 从上往下各占一行。
#: 同一对参与者之间来回多条消息时，没有「第几步」这个位置，它们就全塌成一条线。
GROUP_STYLES: tuple[str, ...] = ("solid", "dashed")

#: 边的语义角色 → 线型与颜色，并**进图例**。只有节点进图例、边不进，读者就
#: 无从知道红线和蓝线差在哪（参考图里「交换机互联 / RoCE 链路」正是两条边）。
#: 边角色色板。**必须与 ROLE_PALETTE_ORDER 无交集** —— 两个池子各自循环时，
#: 同一张图例里一个颜色会同时代表一个节点角色和一条边。2026-09-17 实测
#: 微服务那张图：15 格图例里 4 个颜色各担 2–3 个含义（#C44E52 = 数据层 /
#: HTTP 调用 / 查询展示）。图例在撒谎，而没有任何判据会说话。
#:
#: 原来 6 色，而那张图有 10 个边角色 —— 光是池内循环就会撞。扩到 12。
EDGE_ROLE_PALETTE: tuple[str, ...] = (
    "#5A5A5A", "#2B6CB0", "#2E9E6B", "#B7791F", "#7C5CD6", "#D1495B",
    "#00838F", "#8D6E63", "#3F51B5", "#7CB342", "#EF6C00", "#AD1457",
)

#: 需求原文里的「量」：数字 + 可选单位。合同没提到的量 = 机械可查的覆盖缺口。
#: 只做 finding，不拒绝 —— 需求文本里也会有与图无关的数字。
#:
#: **中文数词也要认**：2026-09-17 同一条 prompt 连跑三次，丢掉的恰恰是「双CPU」
#: 和「两个 PCIe switch」—— 而正则只认阿拉伯数字，这两条需求对判据完全隐形。
_CN_NUMERALS = {
    "一": "1", "二": "2", "两": "2", "双": "2", "三": "3", "四": "4", "五": "5",
    "六": "6", "七": "7", "八": "8", "九": "9", "十": "10",
}
#: 量词 = 这是在数东西（相对于 400Gbps 这种量纲单位）。数东西的，应当有断言核。
_COUNTING_UNITS = ("张", "个", "台", "条", "颗", "块", "路", "份", "组", "片")
_QUANTITY_RE = re.compile(
    r"(?<![0-9A-Za-z])(\d+(?:\.\d+)?|[一二两双三四五六七八九十])\s*"
    r"(张|个|台|条|颗|块|路|份|组|片|Gbps/s|Gbps|GB/s|TB|GB|us|µs|μs|ms|ns|%)?",
    re.IGNORECASE,
)


def _as_number(token: str) -> str:
    """「双」「两」都是 2。合同侧写的是阿拉伯数字，比对前先归一。"""

    return _CN_NUMERALS.get(token, token)


def family_for_asset_kind(asset_kind: str) -> str | None:
    """asset_kind 落在哪个合同家族；不受合同管辖时返回 None。"""

    return _ASSET_KIND_FAMILY.get(str(asset_kind or "").strip().lower())


# ── 家族自查清单：push 给调用方，不指望它自己去 load_skill ────────────────
#
# v1 实测：`scientific-schematic` skill 里**恰好**有能抓住这次缺陷的那条
# （「每条箭头/连线都对应调用方 spec 里声明的一条 edge」），但 agent 从没
# `list_skills`、也从没加载它 —— `load_skill` 是自愿调用，没有任何机制检查
# 「渲染前是否读过本家族的清单」。
#
# 加一道「你读了吗」的闸是打补丁：那只是给代理指标上闸。根因是**家族知识是
# 建议而不是结构**。所以这里做两件事：
#   1. 家族知识变成 schema（schematic 合同必须填 nodes/edges/bands —— skill 说的
#      "represent the figure as structured components" 现在有地方放了，且不填
#      就铸不出来）；
#   2. 剩下无法 schema 化的那几条，由 `declare_figure_contract` 的**返回值**
#      push 给 agent —— 契约必须送到调用方，不能指望对方来拉。
FAMILY_CHECKLIST: dict[str, tuple[str, ...]] = {
    "schematic": (
        "**词表是有限的，自造字段会被静默丢掉**：想说的事（时长 / 基数 1:N / 泳道 / "
        "边上的流量权重 / 物理位置）在 schema 里找不到词时，别在 node/edge 上自己加一个"
        "键 —— 归一化不认识它，图上不会有，而且不会报错。写进顶层 unexpressed=["
        "\"...\"]：框架照画其余部分，记录里如实披露图上缺这件事。缺词表是词表的问题，"
        "把它藏起来才是你的问题。",
        "**先问这张图印出来多大**：`medium`（slide / page / column / poster）决定版心，"
        "框架按它算「复现之后节点标签还剩几 pt」，低于该媒介下限就报。画布的 pt 数"
        "就是物理英寸数 —— 一张 420mm 宽的图放进期刊单栏，11pt 的标签只剩 2.2pt。"
        "读不了的时候唯一有效的办法是**让一行里并排的东西变少**（折叠一层 / 明细搬进 "
        "spec 块 / 一栏拆两栏）；把字号调大没有用 —— 整张图一起放大，缩完还是那么小。",
        # ── 归你选的三件事（配色/字号/线宽全部归框架，你无从选择）────────────
        # 这三条是 54 轮真跑量出来的，不是审美口味。放在这里而不是只放 skill：
        # `figure_contract_schema` **每一轮都被调**，`load_skill` 最近八轮只被调了
        # 三次（37.5%）。把指南放进一个模型可能不读的渠道，等于没送到 ——
        # 这正是「新词表必须进 example」那条规矩的同一个道理。
        "**规格写在组件上，还是写在图例里**：两者都不算漏（事实都在纸上），"
        "但读者代价不同 —— 写在 `sublabel` 上一眼读完，写进图例要来回对照。"
        "用户给的参考图 17/19 个节点带副标题（GPU 上写 `RTX PRO 6000`、网卡上写 "
        "`400 Gbps`），这是它「看起来更专业」的主要来源之一。判据：**这条规格是"
        "读者看这个盒子时就想知道的吗**？是 → sublabel；只是分类 → 交给 role 和图例。",
        "**谁是主角**：`emphasis=primary` 标一两个，别的不写 —— 强调是相对关系，"
        "需要一个安静的多数做底（全体 primary 会被当作自相矛盾拒绝录入）。"
        "实测一张「两个 switch 各挂 4 张 GPU」的图，把两个 switch 标成 primary，"
        "全图立刻有了视觉重心。",
        # 这条原先写着「3 块≈画幅 1.0、4 块≈0.86」。**那是编的** —— 从 45 个
        # 数据点里挑了两个拼出来的干净故事。全量一核：3 块 n=12 均值 0.86
        # （0.67–1.04），4 块 n=33 均值 0.81（0.70–1.04），**范围几乎完全重叠**。
        # 块数几乎不解释画幅。放一个假数字在每轮必读的渠道里，比不放更坏。
        # 这条改过两次，两次都是我编的因果：先是「3 块≈1.0、4 块≈0.86」
        # （从 45 个点里挑两个），再是「减少最宽一行的元件数」（方向还反了：
        # r=+0.40，元件多反而更横）。第三次不猜了 —— 全量 11 张图核过，
        # 最宽行 r=+0.40、行数 r=+0.11、两者之比 r=-0.03，**没有一个解释画幅**。
        # **必读渠道里只写量过的事实，不写为什么。**
        "**画幅不是你能直接拧的旋钮**：它是布局里许多件事相互作用的结果，"
        "而不是某一个编排选择的函数（实测 11 张图：最宽一行的元件数、行数、"
        "两者之比，没有一个能解释画幅）。所以**别花轮次去猜怎么把它调横** —— "
        "声明完读 `render_figure` 回的 `layout.digest`，那里有真实的数；"
        "想比较两种编排，就都声明一次看哪个更好，不要照着某条经验法则调。",
        "一句话的注记不要单独成块：分栏边框加标题条一块固定 55pt，而一行注记"
        "内容只有 17pt（效率 22%）。框架会把没标题的 note 块降级成页脚并点名提醒你。",
        "每条连线都是 edges 里声明的一条；框架按声明画，声明漏了图上就没有。",
        "没有孤立节点：画在图上却不连任何东西的组件，要么补边，要么显式 isolated=true。",
        "detail_of 指的组必须在**另一个** block 里 —— 折叠形态和展开形态同栏，等于把"
        "同一个东西并排画两遍，「详见」也无处可指。",
        "多栏图里同一个东西的两种画法要连起来：概览栏那个盒子若是细节栏某个组的"
        "折叠形态，写 detail_of=<那个组的 id>，图上才会标出「详见 ①」。**两栏会对账**："
        "细节栏那台机器朝外引了几条 annotation，概览栏这个盒子就得接几条线 —— 一台机器"
        "两张网卡各自上联，概览栏就是两条线，不是一条。",
        "示意图不得走私定量语义（坐标轴/误差棒/带刻度的量纲）——那是 quantitative 家族。",
        "measured / modeled / hypothesized 元素共存时用不同 role 区分，图例会自动带出。",
        "把需求里每一条结构约束写成一条 assertion，让它可被机械核对而不是靠读图。",
        "**归属要写进结构，不能只写成边**：一个子系统（例如「switch 0 和它的 4 张 GPU、"
        "1 张网卡」）用嵌套子组声明（groups[].parent），布局器才知道它们是一伙的；"
        "只靠边表达归属，图上就看不出分组。",
        "重复的同构单元不要全展开：4 台一模一样的服务器画四遍，信息量没增加、面积翻四倍。"
        "一台画细、其余压缩成一个盒子（或整体标 ×N）—— 框架会机械点名同构组。",
        "**一张好的架构图不是一个 graph 的渲染，是一个版面，里面包含一个 graph**："
        "用 blocks 分栏（每栏带标题，例如「① 单台服务器内部」「② 集群网络」），"
        "规格/参数用 spec block 列出来，整段说明用 note block —— 别把它们硬塞成节点。",
        "形状要表意：成批的同类元件用 shape='chip'，共享骨干（交换机/总线）用 "
        "shape='bar'；给边写 role，它会按角色配色并进图例（读者才知道两种线差在哪）。",
        "**边被迫跨级就会绕大弯**：一个节点的两类下游要分居它的**上下两侧**"
        "（例如 GPU 在 switch 上方、网卡在下方），别全堆在一侧 —— 堆在一侧时"
        "switch→网卡必须横穿整条 GPU 级，最长边会是均值的 3 倍。",
        "需求里带量词的数（8 张 / 两个 / 4 台 / 1 台）**每一条都要有断言核**：node_count "
        "或 neighbors，并且 derived_from **引用需求里的那一小段原文**（别整句照抄 —— "
        "整句照抄会让判据以为那句里所有的数都有人核了）。图画对了不等于有人核过它。",
        "节点的 role 说「这是个什么东西」（处理 / 判定点 / 交换机），边的 role 说「这条"
        "连接是什么」（数据流 / 回退 / RoCE 链路）。**同一个名字不要两边都用** —— 图例上"
        "会出现同名两条：一个色块、一条线，两种颜色两种含义。",
        "**自己指向自己是合法的**（状态机的心跳续期、重试；流程里的自查）：框架画成节点"
        "右上角的小回环。别因为画不出来就把它挪进注记 —— 注记说有、图上没有，正是这套"
        "合同要拦的事。",
        "时序/交互图写 edges[].step=1,2,3…：参与者横排一行、消息按步从上往下各占一行，"
        "生命线由框架画。**同一对参与者之间来回多条消息时非写不可** —— 不写，它们全塌成"
        "一条线、标签叠成一团（而交叉判据会说没毛病）。写了 step 就每条都要写，自己发给"
        "自己也合法。",
        "立在整摞层**旁边**的东西（外部依赖、共享服务、管理口）写 nodes[].side=left|right："
        "它不进 bands，框架把它摆到侧边、边走一条公共侧轨。塞进某一层就得横穿那一层，"
        "单独给它一层又会挡住别的层之间的线 —— 两种摆法实测都是几十上百个交叉。",
        "**边的端点可以写 `group:<id>`** —— 和 bands 同一套词法，指的是整个组。"
        "「每个服务都上报 Prometheus」是一条边不是十条，「网关调用整个业务层」是一条边"
        "不是十条。密图里最省交叉的一个动作，而且不动结构：22 节点 / 65 边实测 "
        "267 → 收掉可观测那把扇子 169 → 两把都收 30，22 个节点一个不少。"
        "说得出区别的关系照样单独写 —— 收的是**说不出区别**的那一把。",
        "**图太密时，第一步是把细节那一层折叠掉**：另起一栏，用一个 detail_of 代表节点"
        "顶替那一整层（「业务层（10 个服务）」），细节留在原来那一栏。这是结构动作，"
        "不是挪文字。折叠还会把被并起来的那些节点通向同一目标、同一 role 的平行边"
        "**合并成一条** —— 22 节点 / 65 边那张图，① 栏折叠后只剩 12 条边。"
        "2026-09-17 两次实测：原样 290 → 折叠 133（只挪文字 191）；原样 268 → 折叠 8。",
        "第二步才是把明细搬进 spec 块（哪个服务用哪个存储这类映射，文字比连线清楚）。"
        "只做这一步、不折叠结构，交叉只能从 290 降到 191。",
        "布线层面救不了密集图：按目标拆星形总线、细分层、侧轨锚点散开，三种都实测过，"
        "全部无效或更糟。别在那上面花时间。",
        "多对多别逐条画：一排节点各自连同一组共用服务时，**把每个源到每个目标的边都写全**，"
        "框架会自动收成一条共用总线（少写一条就不是总线，只能逐条画，线立刻织成网）。",
        "组画出来就是一个框，**给它名字**：读者看见一个灰框，得知道它圈的是什么。"
        "这个分组本身没有含义、只是为了对齐，就别单独建组、把成员并进父组。",
        "一条线代表多条链路时写 edges[].represents=N（「2 × 400G」）：图上画成 N 股并行，"
        "端口数与跨栏对账都按 N 算。不写就是 1 条 —— 少写了，图上就真的少那几条。",
        "端口数要**两个方向**都对得上：线比口多，被画的东西本身就不成立（2 口交换机插"
        "不进 5 根线），而且多出来的线不再落在任何口上。",
        "端口数要对得上线数：ports=N 而只有 M 条边落上去，读者看到的是 N-M 个空口、"
        "只能自己猜那些链路存不存在。要么把边补齐，要么写一条 neighbors 断言说清这个"
        "节点的度。",
        "**画出端口**：交换机/背板写 ports=N，边上会画 N 个端口小块，连线会落在"
        "端口上。「这是一台"
        "8 口交换机」一眼可见，比让读者去数连线可靠 —— 连线常常只画代表性的几条。",
        "**机箱/边界/信任域用 style='dashed' 的组框**，与实体子系统的实线框区分；"
        "「某某机箱（单节点）」这类容器就该是虚线。",
        "**「这条线去哪儿了」用 annotations 引出，别造假节点**："
        "{anchor, side, text} 会画一支箭头加一句话（「上联 RoCE 交换机」）。"
        "把它塞成一条边加一个占位节点，会让 node_count/edge_count 凭空多出"
        "一个组件和一条链路，机械断言随之失真。",
        "**chip 的 label 要短且互相可区分**（G0/G1/…、通道号、孔位号）：型号和规格"
        "属于 role（进图例）或 spec block，不该塞进每个小色块 —— 8 个小块上重复 8 遍"
        "同一个型号，既占宽又让读者分不出谁是谁。",
    ),
    "quantitative": (
        "**先问这张图印出来多大**：medium 由调用方的 constraints.width 绑定（没给按 "
        "purpose 推）。渲染后框架按 figsize 换算每段文字的印刷字号：figsize 宽度不超过"
        "版心（double_column 6.69in / single_column 3.31in）时字号 1:1；超过就按比例缩，"
        "8pt 的刻度在 10in 宽的图里只剩 5.3pt —— 低于下限拒绝铸记录。declare 返回的 "
        "render_guidance 里有这些数，照着写 figsize 和字号。",
        f"**一张图最多 {MAX_PANELS} 个面板**：多了在声明时就拒绝。8 面板的全景图拆成"
        "两张、或按模型/网络各一张，各自声明合同。",
        "**≥2 条序列就必须有图例**：条目逐字等于 series[].label、不多不少不重复、整个"
        "图例在画布之内 —— 渲染后从对象模型读 legend 对账。堆叠柱每类只标一次 label"
        "（label=... if first else None），否则图例里出现 48 个重复条目。",
        f"**每根轴写 label 与 unit**（分类/无量纲轴明写 {NO_UNIT!r}）：纸上要出现 "
        "`Label (unit)` 至少一次（共享轴只标外侧面板是允许的）。",
        "**配色用框架给的调色板**：渲染前框架已把 axes.prop_cycle 设成学术调色板，"
        "不指定 color 就自动用它；要指定就从 render_guidance.palette 里挑（中性色可用于"
        "参考线）。matplotlib 默认的橙蓝（#1f77b4/#ff7f0e）会被当作「没选过配色」拒绝。",
        "每条序列绑定一个上游字段；序列数按图例条目（有标签的艺术家）机械读出，不按 "
        "ax.bar 调用次数 —— 4 面板 × 4 组 × 6 根堆叠柱是 192 个 container、2 条序列。",
        "坐标轴刻度（linear/log）、是否截断、误差棒语义都要声明 —— 核对读的是 Axes 对象模型。",
        "不在本节点聚合/平滑/去异常值/算显著性；显示变换必须在渲染代码里可见。",
        "把需求里每一条可数事实（几条序列、几个面板、哪个轴是什么）写成 assertion。",
        "**同一个 request_id 最多渲染 3 次**：第 4 次返回「画不出合同要求的样子」和原因，"
        "由调用方决定。所以先 execute_python 试画自查，定稿再 render_figure。",
    ),
    "composite": (
        "**先问这张图印出来多大**：medium 由调用方绑定；渲染后按 figsize 换算每段文字的"
        "印刷字号，低于下限拒绝。declare 返回的 render_guidance 里有 figsize 上限与字号下限。",
        f"**一张图最多 {MAX_PANELS} 个面板**；≥2 条序列必须有图例（条目逐字等于 "
        "series[].label，且在画布内）；配色从 render_guidance.palette 里选。",
        "每个面板声明它讲什么、用哪些源数据；panel 数与 fig.axes 会被核对（colorbar 不算面板）。",
        "有坐标轴的面板写 axes.x / axes.y 的 label 与 unit；纸上要出现 `Label (unit)`。",
        "panel label 用专用留白带，不叠在数据上。",
        "把需求里「几个面板、什么顺序」写成 assertion。",
        "**同一个 request_id 最多渲染 3 次**：第 4 次返回「画不出合同要求的样子」和原因，"
        "由调用方决定。先 execute_python 试画自查，定稿再 render_figure。",
    ),
}


# ── 断言词表 ──────────────────────────────────────────────────────────────
#
# 断言是**合同里可被机械求值的那部分**。词表刻意保持小：每加一个 kind 就是
# 一类「模型能声明、框架能验」的事实。报错必须列出合法值（要求对方引用一个
# 它无从枚举的标识符，本来就只能靠猜）。
ASSERTION_KINDS: tuple[str, ...] = (
    "node_count",     # 某选择器下的节点数
    "edge_count",     # 某两端选择器之间的边数
    "degree",         # 某选择器下每个节点的度
    "neighbors",      # 某个节点在某选择器下的邻居数
    "group_count",    # 组数
    "series_count",   # (asserted) 序列数
    "panel_count",    # (asserted) 面板数
    "axis_scale",     # (asserted) 某轴的刻度类型
)

_COMPARATORS: tuple[str, ...] = ("equals", "at_least", "at_most")


def _require_endpoint(value: Any, field: str) -> str:
    """边的端点：一个节点 id，或 `group:<id>` —— 指向整个组。

    「网关调用整个业务层」是架构图里最常写的一句话，而它过去在合同里没有位置：
    作者只能写十条边（这张图 65 条边里 54 条是这种多对少的扇子），或者写
    `to="biz"` 然后被告知「unknown node」—— 而 biz 正是它自己刚声明的组。
    2026-09-17 实测：模型第三次声明伸手去够的就是这个，被拒后再没找回来。

    词法不是新造的：`bands` 从一开始就是 'group:<id>' / 'node:<id>'。
    """

    text = str(value or "").strip()
    if text.startswith("group:"):
        return "group:" + _require_id(text[len("group:"):], field)
    return _require_id(value, field)


def _require_id(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not _ID_RE.match(text):
        raise VisualContractError(
            f"{field} must be a short identifier matching {_ID_RE.pattern!r}; got {value!r}"
        )
    return text


def _require_list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise VisualContractError(f"{field} must be an array")
    return value


def _normalize_selector(value: Any, field: str) -> dict[str, Any]:
    """选择器：`{"role": "gpu"}` / `{"group": "srv1"}` / `{"ids": [...]}` / `{}`=全部。"""

    if value is None:
        return {}
    selector = require_dict(value, field)
    allowed = {"role", "group", "ids"}
    unknown = sorted(set(selector) - allowed)
    if unknown:
        # 差一点点的词要指出来。`id` vs `ids`、`node` vs `ids` —— 作者写下的是
        # 一个**几乎对**的词，而「allowed: [...]」这种列举法让他自己去比对三个
        # 词里哪个是他要的。2026-09-17 实测：iter65 写 source={'node': ...}、
        # iter83 写 source={'id': ...}，两轮各废掉一次声明。
        near = {
            "id": "ids", "node": "ids", "nodes": "ids", "ids": "ids",
            "roles": "role", "kind": "role", "groups": "group", "in": "group",
        }
        hints = [
            f"{key!r} → {near[key]!r}" for key in unknown if near.get(key) in allowed
        ]
        raise VisualContractError(
            f"{field} has unknown keys {unknown}; allowed: {sorted(allowed)}"
            + (f". Did you mean {', '.join(hints)}?" if hints else ".")
            + " A selector picks nodes by role, by group, or by an explicit id list."
        )
    out: dict[str, Any] = {}
    if "role" in selector:
        out["role"] = str(selector["role"]).strip()
    if "group" in selector:
        out["group"] = str(selector["group"]).strip()
    if "ids" in selector:
        ids = _require_list(selector["ids"], f"{field}.ids")
        out["ids"] = [_require_id(item, f"{field}.ids[]") for item in ids]
    return out


def _normalize_assertion(raw: Any, index: int) -> dict[str, Any]:
    item = require_dict(raw, f"assertions[{index}]")
    kind = str(item.get("kind") or "").strip()
    if kind not in ASSERTION_KINDS:
        raise VisualContractError(
            f"assertions[{index}].kind must be one of {list(ASSERTION_KINDS)}; got {kind!r}"
        )
    comparators = [key for key in _COMPARATORS if key in item]
    if len(comparators) != 1:
        raise VisualContractError(
            f"assertions[{index}] needs exactly one comparator from {list(_COMPARATORS)}"
        )
    comparator = comparators[0]
    try:
        expected: Any = item[comparator]
        if kind != "axis_scale":
            expected = int(expected)
    except (TypeError, ValueError) as exc:
        raise VisualContractError(
            f"assertions[{index}].{comparator} must be an integer"
        ) from exc

    out: dict[str, Any] = {
        "id": _require_id(item.get("id") or f"a{index + 1}", f"assertions[{index}].id"),
        "kind": kind,
        "comparator": comparator,
        "expected": expected,
        # 这条断言来自需求里的哪句话。不校验它是原文子串（需求可能是转述），
        # 但它必须在场 —— 断言没有出处就没法判断合同是否覆盖了需求。
        "derived_from": str(item.get("derived_from") or "").strip(),
    }
    if not out["derived_from"]:
        raise VisualContractError(
            f"assertions[{index}].derived_from is required: quote the fragment of the "
            "request this assertion comes from, so contract coverage can be reviewed"
        )
    for key in ("selector", "source", "target", "within"):
        if key in item:
            out[key] = _normalize_selector(item[key], f"assertions[{index}].{key}")
    if "node" in item:
        out["node"] = _require_id(item["node"], f"assertions[{index}].node")

    # `degree` 有两种问法，写错哪种都会在求值时给出一个看不懂的答案。
    # 2026-09-16 实测：模型想问「roce_sw 这个节点连着 8 条边」，写成了
    # 不带 selector 的 degree —— 于是框架去核**全部 57 个节点**是否各连 8 条，
    # 回了一句 "1/57 nodes satisfy it"，模型花了一整轮才猜出它问错了。
    # 报错必须当场把两种合法形式并排写出来，而不是让人从答案倒推问题。
    if kind == "degree" and not ({"selector", "node"} & set(out)):
        raise VisualContractError(
            f"assertions[{index}] uses kind='degree' without saying whose degree. "
            "Two legal forms: degree + node=<id> (that one node's edge count), or "
            "degree + selector={'role': ...} (every node in that role must satisfy it). "
            "To count one node's neighbours of a given kind, use kind='neighbors' with "
            "node=<id> and within={'role': ...}."
        )
    if kind == "neighbors" and "node" not in out:
        raise VisualContractError(
            f"assertions[{index}] uses kind='neighbors' but names no node: add "
            "node=<id> (and within={'role': ...} to restrict which neighbours count)."
        )
    if "axis" in item:
        out["axis"] = str(item["axis"]).strip()
    return out


def _one_of(value: Any, allowed: tuple[str, ...], where: str, *, allow_empty: bool) -> str:
    text = str(value or "").strip().lower()
    if not text:
        if allow_empty:
            return ""
        raise VisualContractError(f"{where} must be one of {list(allowed)}")
    if text not in allowed:
        raise VisualContractError(f"{where} must be one of {list(allowed)}; got {text!r}")
    return text


def _normalize_node(raw: Any, index: int) -> dict[str, Any]:
    item = require_dict(raw, f"nodes[{index}]")
    try:
        ports = int(item.get("ports") or 0)
    except (TypeError, ValueError) as exc:
        raise VisualContractError(f"nodes[{index}].ports must be an integer") from exc
    if not 0 <= ports <= 64:
        raise VisualContractError(f"nodes[{index}].ports must be between 0 and 64")
    shape = str(item.get("shape") or "box").strip().lower()
    if shape not in NODE_SHAPES:
        raise VisualContractError(
            f"nodes[{index}].shape must be one of {list(NODE_SHAPES)}; got {shape!r}. "
            "chip = densely packed small tile for a bank of identical parts; "
            "bar = full-width bar for a shared backbone; box = a normal component."
        )
    return {
        "id": _require_id(item.get("id"), f"nodes[{index}].id"),
        "label": str(item.get("label") or "").strip(),
        "sublabel": str(item.get("sublabel") or "").strip(),
        "shape": shape,
        "ports": ports,
        "role": str(item.get("role") or "component").strip() or "component",
        # 图 / 地。不写 = 正常档（安静的多数）。primary 前进、muted 后退，
        # 靠的是亮度与线宽，不是色相 —— 色相已经被「类别」占着了。
        "emphasis": _one_of(
            item.get("emphasis"), NODE_EMPHASIS, f"nodes[{index}].emphasis",
            allow_empty=True,
        ),
        # 孤立节点必须自己举手。v1 里两颗 CPU 画在图上、一条线都没有，八版
        # 无人报 —— 因为「没连任何东西」是语义缺陷，渲染审计不看这个。
        "isolated": bool(item.get("isolated")),
        # 这个节点是另一个块里某个组的折叠形态。多栏图里「② 栏的一台服务器」
        # 就是「① 栏画开的那个机箱」，但合同里过去没有位置说这句话，于是它
        # 只能是一个孤立色块，读者看不出两栏讲的是同一个东西
        # （2026-09-17 看图发现；同型根因第 6 次）。
        "detail_of": str(item.get("detail_of") or "").strip(),
        # 它不站在任何一层里，而是**立在这一摞层的旁边**（外部依赖、共享服务、
        # 总控）。放进某一层就得横穿那一层，中间隔着谁就压谁；单独给它一层，
        # 别的层之间的线又得穿过它 —— 2026-09-17 分层架构基准两种摆法实测
        # 18 / 164 个交叉，都不对。图里存在的这个区别，合同里过去没有位置说。
        "side": _one_of(
            item.get("side"), NODE_SIDES, f"nodes[{index}].side", allow_empty=True
        ),
    }


def _normalize_group(raw: Any, index: int) -> dict[str, Any]:
    item = require_dict(raw, f"groups[{index}]")
    ranks_raw = _require_list(item.get("ranks") or [], f"groups[{index}].ranks")
    ranks: list[list[str]] = []
    dropped_ranks = 0
    for r_index, rank in enumerate(ranks_raw):
        row = _require_list(rank, f"groups[{index}].ranks[{r_index}]")
        if not row:
            # **空行不与任何东西矛盾**，所以不拒 —— 照画，并且点名（见下方 finding）。
            #
            # 架构的拒绝分支只加在自相矛盾上。一个空的 rank 什么内容都没有：它
            # 既不和别的声明打架，也不会画出任何东西，丢掉它零损失。而为它拒一次
            # 要付一个完整往返 —— 2026-09-17 实测那个往返的代价：agent 五次声明
            # 三次被拒，其中两次是**改上一条时把别的弄坏了**（bands 丢了、
            # ranks 又空了）。拒绝一个无害的 no-op，换来的是它去破坏别处。
            dropped_ranks += 1
            continue
        ranks.append([_require_id(n, f"groups[{index}].ranks[{r_index}][]") for n in row])
    # 「这个组空不空」在这里**答不出来**：父组的成员就是它的子组，而这个作用域
    # 一次只看得见一个组。判据留着，但搬到 _validate_structure —— 那里 parent
    # 已经成形，可以问真正的问题：这个组的**子树**里有没有节点。
    #
    # 2026-09-17：交叉判据一直在教作者「declare the sub-systems as nested groups
    # (groups[].parent)」，而照做得到的纯容器父组自身 ranks 为空，在这里被当场
    # 拒掉。22 节点 / 65 边那张图，agent 连发五次声明、几何一模一样（268 交叉），
    # 就是被这条判据挡回去的：框架开的药方，框架自己不收。
    style = str(item.get("style") or "solid").strip().lower()
    if style not in GROUP_STYLES:
        raise VisualContractError(
            f"groups[{index}].style must be one of {list(GROUP_STYLES)}; got {style!r}. "
            "dashed = a boundary/enclosure (chassis, room, trust zone); "
            "solid = a real sub-system."
        )
    position = str(item.get("label_position") or "top").strip().lower()
    if position not in {"top", "bottom"}:
        raise VisualContractError(
            f"groups[{index}].label_position must be 'top' or 'bottom'; got {position!r}"
        )
    return {
        "id": _require_id(item.get("id"), f"groups[{index}].id"),
        "label": str(item.get("label") or "").strip(),
        "ranks": ranks,
        "dropped_empty_ranks": dropped_ranks,
        "label_position": position,
        "style": style,
        # 嵌套子组：一台服务器里「switch 0 和它的 4 张 GPU、1 张网卡」是一个
        # **子系统**，不只是同一行里的若干盒子。2026-09-16 实测：这个归属只
        # 存在于边里而不在结构里，于是布局器只能猜 —— 两个 switch 被推到最左，
        # 8 条线拉成一把长扇子，「每 4 张一组」在图上完全看不出来。
        "parent": (
            _require_id(item["parent"], f"groups[{index}].parent")
            if item.get("parent")
            else None
        ),
    }


def _positive_count(value: Any, where: str) -> int:
    if value in (None, ""):
        return 1
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise VisualContractError(f"{where} must be an integer >= 1") from exc
    if not 1 <= count <= 64:
        raise VisualContractError(f"{where} must be between 1 and 64; got {count}")
    return count


def _normalize_edge(raw: Any, index: int) -> dict[str, Any]:
    item = require_dict(raw, f"edges[{index}]")
    kind = str(item.get("kind") or "link").strip().lower()
    if kind not in {"link", "bus", "dashed"}:
        raise VisualContractError(
            f"edges[{index}].kind must be one of ['bus', 'dashed', 'link']; got {kind!r}"
        )
    return {
        "from": _require_endpoint(item.get("from"), f"edges[{index}].from"),
        "to": _require_endpoint(item.get("to"), f"edges[{index}].to"),
        "label": str(item.get("label") or "").strip(),
        # 这条边**是什么**（"交换机互联" / "RoCE 链路" / "数据流"）。给了就按
        # 角色配色并进图例 —— 不给则全部同色（与改动前一致）。
        "role": str(item.get("role") or "").strip(),
        "kind": kind,
        "bidirectional": bool(item.get("bidirectional")),
        # 这一笔画的是几条链路。架构图里「一条线标 2×400G」是合法简写，但过去
        # 合同里没有位置说它 —— 于是「4 条线 vs 8 个端口」到底是画漏了还是简
        # 写，判据分不出来（iter16 实测，5 条 findings 全指着这件事）。写了
        # represents=N，图上会画成 N 股并行的线，各处计数也按 N 算。
        "represents": _positive_count(item.get("represents"), f"edges[{index}].represents"),
        # 这条消息发生在**第几步**。时序图里同一对参与者之间会来回很多条消息，
        # 合同里过去没有位置说「这是第 3 条」—— 于是七条消息全塌成一条线，
        # 标签叠成一团（2026-09-17 时序图基准实测，而交叉判据说没毛病）。
        # 写了 step，框架把参与者横排、消息按步从上往下排，各占一行。
        "step": 0 if item.get("step") in (None, "") else _positive_count(
            item.get("step"), f"edges[{index}].step"
        ),
    }


def _normalize_band(raw: Any, index: int) -> list[str]:
    """一条顶层带子。空带子**不拒**，返回空表由调用方丢掉 —— 同 ranks 的理由：
    空行不与任何东西矛盾，为它拒一次要付一个完整往返，而那个往返会弄坏别处。"""

    row = _require_list(raw, f"bands[{index}]")
    if not row:
        return []
    out: list[str] = []
    for entry in row:
        text = str(entry or "").strip()
        if not text.startswith(("group:", "node:")):
            raise VisualContractError(
                f"bands[{index}] entries must be 'group:<id>' or 'node:<id>'; got {entry!r}"
            )
        prefix, _, ident = text.partition(":")
        _require_id(ident, f"bands[{index}][]")
        out.append(f"{prefix}:{ident}")
    return out


def _normalize_series(raw: Any, index: int) -> dict[str, Any]:
    item = require_dict(raw, f"series[{index}]")
    return {
        "id": _require_id(item.get("id"), f"series[{index}].id"),
        "label": str(item.get("label") or "").strip(),
        "panel": str(item.get("panel") or "").strip(),
        "source_field": str(item.get("source_field") or "").strip(),
    }


def _normalize_axis(raw: Any, where: str) -> dict[str, str]:
    """一根轴 = {label, unit, scale}。

    2026-09-18 iter11：四张 panorama 的 y 轴没有单位、x 轴没有标题，合同里
    根本没有说这两件事的位置（axes 只能写刻度类型）。同型根因第 N 次：图上
    存在的区别，合同里没有声明位置。
    """

    if isinstance(raw, str):
        raise VisualContractError(
            f"{where} must be an object {{label, unit, scale}}, not the bare scale "
            f"name {raw!r}: write e.g. {{\"label\": \"Wall time\", \"unit\": \"ms\", "
            f"\"scale\": {raw!r}}}"
        )
    item = require_dict(raw, where)
    scale = str(item.get("scale") or "").strip().lower()
    if scale and scale not in _AXIS_SCALES:
        raise VisualContractError(
            f"{where}.scale must be a matplotlib scale name {list(_AXIS_SCALES)}"
        )
    unit = item.get("unit")
    out = {
        "label": str(item.get("label") or "").strip(),
        "unit": str(unit).strip() if unit is not None else "",
        "scale": scale,
    }
    return out


def _normalize_panel(raw: Any, index: int) -> dict[str, Any]:
    item = require_dict(raw, f"panels[{index}]")
    axes = item.get("axes") if isinstance(item.get("axes"), dict) else {}
    out_axes: dict[str, dict[str, str]] = {}
    for axis in ("x", "y"):
        if axes.get(axis) not in (None, "", {}):
            out_axes[axis] = _normalize_axis(axes[axis], f"panels[{index}].axes.{axis}")
    return {
        "id": _require_id(item.get("id"), f"panels[{index}].id"),
        "label": str(item.get("label") or "").strip(),
        "message": str(item.get("message") or "").strip(),
        "axes": out_axes,
    }


def axis_label_text(axis: dict[str, Any]) -> str:
    """合同里一根轴在纸上应有的样子：`Label (unit)`；无单位就只有 Label。"""

    label = str(axis.get("label") or "").strip()
    unit = str(axis.get("unit") or "").strip()
    if not unit or unit.lower() == NO_UNIT:
        return label
    return f"{label} ({unit})"


def normalize_contract(raw: Any, *, family: str) -> dict[str, Any]:
    """把一份 agent 声明的合同归一成机械可读、可 hash、可 diff 的事实集。

    family 由 asset_kind 机械决定（不是另一个可选字段）—— 一个问题一个真相源。
    """

    body = require_dict(raw, "contract")
    if family not in CONTRACT_FAMILIES:
        raise VisualContractError(
            f"contract family {family!r} is not one of {sorted(CONTRACT_FAMILIES)}"
        )
    out: dict[str, Any] = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "family": family,
        "title": str(body.get("title") or "").strip(),
        # 作者显式承认「这件事合同说不出来」。它必须进归一化结果、进 hash、
        # 进记录 —— 只当成一条 finding 打印出来，下一版就没人记得图上缺什么。
        "unexpressed": [
            str(item).strip()
            for item in (
                body.get("unexpressed")
                if isinstance(body.get("unexpressed"), list)
                else ([body["unexpressed"]] if body.get("unexpressed") else [])
            )
            if str(item).strip()
        ],
        "assertions": [
            _normalize_assertion(item, index)
            for index, item in enumerate(_require_list(body.get("assertions") or [], "assertions"))
        ],
    }

    if family in COMPILED_FAMILIES:
        # 平铺旧式（顶层直接写 nodes/groups/bands/edges）= 只有一个 diagram block
        # 的版面。存量合同与现有测试因此原样有效 —— 表达力是**加**上去的，不是
        # 换掉的。
        if body.get("blocks") is not None:
            out["blocks"] = _normalize_blocks(body["blocks"])
            merged = _merge_diagram_blocks(out["blocks"])
            out.update(merged)
            # 顶层 `notes` 在这条分支上**从来没被搬过来** —— schema 里是合法键，
            # 作者写了，图上没有，而 `unexpressed_findings` 也不会响（它问的是
            # 「这个键认不认识」，不是「这个键到没到纸上」）。2026-09-17。
            out["notes"] = [
                str(item).strip()
                for item in body.get("notes") or []
                if str(item).strip()
            ]
            if body.get("medium"):
                out["medium"] = _one_of(body["medium"], tuple(sorted(MEDIA)), "medium", allow_empty=True)
            check_structure({**out, **merged})
            return out
        nodes = [
            _normalize_node(item, index)
            for index, item in enumerate(_require_list(body.get("nodes") or [], "nodes"))
        ]
        if not nodes:
            raise VisualContractError(
                "a schematic contract must declare nodes: the framework renders the "
                "diagram from this declaration, so an empty node list draws nothing"
            )
        out["nodes"] = nodes
        out["groups"] = [
            _normalize_group(item, index)
            for index, item in enumerate(_require_list(body.get("groups") or [], "groups"))
        ]
        out["edges"] = [
            _normalize_edge(item, index)
            for index, item in enumerate(_require_list(body.get("edges") or [], "edges"))
        ]
        bands_raw = _require_list(body.get("bands") or [], "bands")
        if not bands_raw:
            raise VisualContractError(
                "a schematic contract must declare bands (the top-level rows): "
                "bands=[['group:<id>' | 'node:<id>', ...], ...]"
            )
        _bands = [_normalize_band(item, index) for index, item in enumerate(bands_raw)]
        out["bands"] = [band for band in _bands if band]
        out["dropped_empty_bands"] = len(_bands) - len(out["bands"])
        if not out["bands"]:
            raise VisualContractError(
                "every band is empty, so nothing would be placed: bands are the "
                "top-level rows — put 'group:<id>' or 'node:<id>' entries in them"
            )
        out["notes"] = [str(item).strip() for item in body.get("notes") or [] if str(item).strip()]
        if body.get("medium"):
            out["medium"] = _one_of(body["medium"], tuple(sorted(MEDIA)), "medium", allow_empty=True)
        out["annotations"] = [
            _normalize_annotation(a, i, {n["id"] for n in nodes}, "contract")
            for i, a in enumerate(_require_list(body.get("annotations") or [], "annotations"))
        ]
    else:
        out["panels"] = [
            _normalize_panel(item, index)
            for index, item in enumerate(_require_list(body.get("panels") or [], "panels"))
        ]
        out["series"] = [
            _normalize_series(item, index)
            for index, item in enumerate(_require_list(body.get("series") or [], "series"))
        ]

    # 平铺形式归一后也表达成单 block 版面 —— 下游只认 blocks 一种形状。
    if family in COMPILED_FAMILIES:
        out["blocks"] = [
            {
                "kind": "diagram",
                "title": "",
                "nodes": out["nodes"],
                "groups": out["groups"],
                "bands": out["bands"],
                "edges": out["edges"],
                "annotations": out.get("annotations") or [],
                "row": None,
            }
        ]
    check_structure(out)
    return out


def _normalize_block(raw: Any, index: int) -> dict[str, Any]:
    item = require_dict(raw, f"blocks[{index}]")
    kind = str(item.get("kind") or "diagram").strip().lower()
    if kind not in BLOCK_KINDS:
        raise VisualContractError(
            f"blocks[{index}].kind must be one of {list(BLOCK_KINDS)}; got {kind!r}. "
            "diagram = a graph; spec = a bulleted summary (optionally in columns); "
            "note = a paragraph."
        )
    title = str(item.get("title") or "").strip()
    # 同一个 row 的 block **并排**。版面只能纵向堆叠时，两个又矮又宽的块
    # （集群图 + 规格摘要）只能各占一整行，画布被拖成竖条 —— 而参考图是横的。
    # 不给 row 就按声明顺序各占一行（与改动前一致）。
    try:
        row = int(item.get("row")) if item.get("row") is not None else None
    except (TypeError, ValueError) as exc:
        raise VisualContractError(f"blocks[{index}].row must be an integer") from exc
    if kind == "diagram":
        nodes = [
            _normalize_node(n, i)
            for i, n in enumerate(_require_list(item.get("nodes") or [], f"blocks[{index}].nodes"))
        ]
        if not nodes:
            raise VisualContractError(
                f"blocks[{index}] is a diagram with no nodes; a diagram block renders "
                "from its declaration, so an empty node list draws nothing"
            )
        groups = [
            _normalize_group(g, i)
            for i, g in enumerate(
                _require_list(item.get("groups") or [], f"blocks[{index}].groups")
            )
        ]
        edges = [
            _normalize_edge(e, i)
            for i, e in enumerate(_require_list(item.get("edges") or [], f"blocks[{index}].edges"))
        ]
        bands_raw = _require_list(item.get("bands") or [], f"blocks[{index}].bands")
        if not bands_raw:
            raise VisualContractError(
                f"blocks[{index}] is a diagram without bands: "
                "bands=[['group:<id>' | 'node:<id>', ...], ...]"
            )
        bands = [_normalize_band(b, i) for i, b in enumerate(bands_raw)]
        known = {node["id"] for node in nodes}
        annotations = [
            _normalize_annotation(a, i, known, f"blocks[{index}]")
            for i, a in enumerate(
                _require_list(item.get("annotations") or [], f"blocks[{index}].annotations")
            )
        ]
        return {
            "kind": "diagram", "title": title, "row": row,
            "nodes": nodes, "groups": groups, "bands": bands, "edges": edges,
            "annotations": annotations,
        }
    if kind == "spec":
        items = [
            str(entry).strip()
            for entry in _require_list(item.get("items") or [], f"blocks[{index}].items")
            if str(entry).strip()
        ]
        if not items:
            raise VisualContractError(f"blocks[{index}] is a spec block with no items")
        try:
            columns = int(item.get("columns") or 1)
        except (TypeError, ValueError) as exc:
            raise VisualContractError(f"blocks[{index}].columns must be an integer") from exc
        if not 1 <= columns <= 4:
            raise VisualContractError(f"blocks[{index}].columns must be between 1 and 4")
        return {"kind": "spec", "title": title, "row": row, "items": items, "columns": columns}
    text = str(item.get("text") or "").strip()
    if not text:
        raise VisualContractError(f"blocks[{index}] is a note block with no text")
    return {"kind": "note", "title": title, "row": row, "text": text}


def _normalize_annotation(
    raw: Any, index: int, known: set[str], where: str
) -> dict[str, Any]:
    """出框叙事引出：锚在某个组件上，朝某方向引一支箭头加一句话。

    它说的是「这条线去哪儿了」「这一块是什么」，是**叙事**不是拓扑 —— 硬塞成
    一条边加一个假节点，会让「节点数」「边数」这些机械断言全部失真。
    """

    item = require_dict(raw, f"{where}.annotations[{index}]")
    anchor = _require_id(item.get("anchor"), f"{where}.annotations[{index}].anchor")
    if anchor not in known:
        raise VisualContractError(
            f"{where}.annotations[{index}].anchor references unknown node {anchor!r}; "
            f"declared nodes: {sorted(known)}"
        )
    side = str(item.get("side") or "bottom").strip().lower()
    if side not in ANNOTATION_SIDES:
        raise VisualContractError(
            f"{where}.annotations[{index}].side must be one of "
            f"{list(ANNOTATION_SIDES)}; got {side!r}"
        )
    text = str(item.get("text") or "").strip()
    if not text:
        raise VisualContractError(f"{where}.annotations[{index}] has no text")
    return {
        "anchor": anchor,
        "side": side,
        "text": text,
        # 一句引出也可以代表多条链路（「2 × 400G 上联」）。跨栏对账数的是**链路
        # 条数**，不是引出的句数 —— 不给它这个字段，一句话就永远只算一条。
        "represents": _positive_count(
            item.get("represents"), f"{where}.annotations[{index}].represents"
        ),
    }


def _normalize_blocks(raw: Any) -> list[dict[str, Any]]:
    blocks = [
        _normalize_block(item, index)
        for index, item in enumerate(_require_list(raw, "blocks"))
    ]
    if not any(block["kind"] == "diagram" for block in blocks):
        raise VisualContractError(
            "a schematic contract needs at least one diagram block; spec and note "
            "blocks annotate a figure, they are not a figure by themselves"
        )
    return blocks


def _merge_diagram_blocks(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    """所有 diagram block 的并集 —— 断言与结构检查在**整张版面**上求值。

    「8 张 GPU」这类需求不该因为作者把图拆成两栏就失效；反过来，同一个 id 在
    两个 block 里出现也必须当成重复（否则「声明了几个」就有两个答案）。
    """

    nodes: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    bands: list[list[str]] = []
    edges: list[dict[str, Any]] = []
    annotations: list[dict[str, Any]] = []
    for block in blocks:
        if block["kind"] != "diagram":
            continue
        nodes += block["nodes"]
        groups += block["groups"]
        bands += block["bands"]
        edges += block["edges"]
        annotations += block.get("annotations") or []
    return {
        "nodes": nodes,
        "groups": groups,
        "bands": bands,
        "edges": edges,
        "annotations": annotations,
    }


def contract_hash(contract: dict[str, Any]) -> str:
    return hash_json(contract)


# ── 结构自洽（机械，拒绝分支）────────────────────────────────────────────
def _group_member_ids(contract: dict[str, Any], group_id: str) -> set[str]:
    """组自身 ranks 里的节点，加上所有后代组的。"""

    by_id = {group["id"]: group for group in contract["groups"]}
    out: set[str] = set()
    stack = [group_id]
    while stack:
        current = stack.pop()
        group = by_id.get(current)
        if group is None:
            continue
        for rank in group["ranks"]:
            out.update(rank)
        stack.extend(
            other["id"] for other in contract["groups"] if other.get("parent") == current
        )
    return out


def is_sequence(contract: dict[str, Any]) -> bool:
    """这份合同画的是时序（消息按步从上往下排）吗。"""

    return any(edge.get("step") for edge in contract.get("edges") or [])


def check_structure(contract: dict[str, Any]) -> None:
    """合同自身必须自洽。不自洽 = 自相矛盾，不是质量判决，所以拒绝。"""

    family = contract["family"]
    if family not in COMPILED_FAMILIES:
        _check_asserted_structure(contract)
        return

    nodes = contract["nodes"]
    node_ids = [node["id"] for node in nodes]
    duplicates = sorted({nid for nid in node_ids if node_ids.count(nid) > 1})
    if duplicates:
        raise VisualContractError(f"duplicate node ids: {duplicates}")

    group_ids = {group["id"] for group in contract["groups"]}
    for node in nodes:
        target = node.get("detail_of")
        if not target:
            continue
        if target not in group_ids:
            raise VisualContractError(
                f"nodes[{node['id']}].detail_of={target!r} names no group. It must be the "
                f"id of a group drawn elsewhere in this figure; known groups: "
                f"{sorted(group_ids) or '(none)'}."
            )
        if target in {g for g in group_ids if node["id"] in _group_member_ids(contract, g)}:
            raise VisualContractError(
                f"nodes[{node['id']}].detail_of={target!r} points at a group that contains "
                "this very node. A collapsed node lives outside the group it collapses."
            )
        # 折叠形态和展开形态不能待在同一栏 —— 那等于把同一个东西并排画两遍，
        # 而「详见 ①」指向的正是读者眼前这一栏。2026-09-17 实测：这种合同当时
        # 照过不误，而且 detail_ref 解析不出来，**徽章和内嵌边框一个都不画** ——
        # 声明写了、像素为零、没人报。
        for block in contract["blocks"]:
            if block["kind"] != "diagram":
                continue
            here = {item["id"] for item in block["nodes"]}
            if node["id"] in here and any(
                group["id"] == target for group in block["groups"]
            ):
                raise VisualContractError(
                    f"nodes[{node['id']}].detail_of={target!r} names a group in the same "
                    "block. A collapsed node and the group it collapses belong in "
                    "different blocks — otherwise the figure draws the same thing twice "
                    "side by side and there is nowhere for 「详见」 to point."
                )
    known = set(node_ids)

    group_ids = [group["id"] for group in contract["groups"]]
    dupe_groups = sorted({gid for gid in group_ids if group_ids.count(gid) > 1})
    if dupe_groups:
        raise VisualContractError(f"duplicate group ids: {dupe_groups}")

    # 嵌套：parent 必须存在、不能成环。成环的组树画不出来，也说明作者把
    # 「包含」写反了 —— 这是自相矛盾，当场拒绝而不是让布局器去猜。
    parent_of = {g["id"]: g["parent"] for g in contract["groups"]}
    for gid, parent in parent_of.items():
        if parent is None:
            continue
        if parent not in set(group_ids):
            raise VisualContractError(
                f"group {gid!r} declares parent {parent!r}, which is not a declared "
                f"group. Declared groups: {group_ids}"
            )
        seen_chain = {gid}
        cursor = parent
        while cursor is not None:
            if cursor in seen_chain:
                raise VisualContractError(
                    f"group nesting forms a cycle through {gid!r}"
                )
            seen_chain.add(cursor)
            cursor = parent_of.get(cursor)

    # 空组：一个框住空气的框在版面上占地方却没有内容可占 —— 留着拒。但问的是
    # **子树**有没有节点，不是这个组自己的 ranks 有没有写满：父组的成员就是它
    # 的子组。放在这里才问得出来（见 _normalize_group 里搬走的那段注释）。
    for index, group in enumerate(contract["groups"]):
        if _group_member_ids(contract, group["id"]):
            continue
        dropped = group.get("dropped_empty_ranks") or 0
        raise VisualContractError(
            f"groups[{index}] ends up with no members"
            + (f" ({dropped} empty rank(s) were dropped)" if dropped else "")
            + ": a group is a box around some nodes, and a box around nothing takes up "
            "room without saying anything. Put node ids in ranks, give it child groups "
            "(groups[].parent), or drop the group."
        )

    # 每个节点恰好被放置一次：组里的 ranks 或顶层 band。放两次 = 画两遍，
    # 放零次 = 声明了却不在图上（v1 的「声明和图不是一回事」正是这种形状）。
    placed: dict[str, int] = {nid: 0 for nid in node_ids}
    for group in contract["groups"]:
        for rank in group["ranks"]:
            for nid in rank:
                if nid not in known:
                    raise VisualContractError(
                        f"group {group['id']!r} places unknown node {nid!r}; "
                        f"declared nodes: {sorted(known)}"
                    )
                placed[nid] += 1

    band_groups: list[str] = []
    for band in contract["bands"]:
        for entry in band:
            prefix, _, ident = entry.partition(":")
            if prefix == "node":
                if ident not in known:
                    raise VisualContractError(
                        f"bands reference unknown node {ident!r}; declared nodes: {sorted(known)}"
                    )
                placed[ident] += 1
            else:
                if ident not in set(group_ids):
                    raise VisualContractError(
                        f"bands reference unknown group {ident!r}; declared groups: {group_ids}"
                    )
                band_groups.append(ident)

    if is_sequence(contract):
        missing = [
            f"{edge['from']}->{edge['to']}"
            for edge in contract["edges"]
            if not edge.get("step")
        ]
        if missing:
            raise VisualContractError(
                "this figure declares message steps, so **every** edge needs one: "
                f"{missing} have no step. Mixing stepped and unstepped messages leaves "
                "the unstepped ones with no place on the time axis."
            )
        if contract["groups"]:
            raise VisualContractError(
                "a stepped (sequence) figure places participants in one row and messages "
                "down a time axis, so it has no groups; drop them, or drop the steps"
            )
        if len(contract["bands"]) != 1:
            raise VisualContractError(
                "a stepped (sequence) figure needs exactly one band listing the "
                f"participants left to right; got {len(contract['bands'])} bands"
            )

    aside = {node["id"] for node in nodes if node.get("side")}
    misplaced = sorted(nid for nid in aside if placed.get(nid, 0) > 0)
    if misplaced:
        raise VisualContractError(
            f"nodes {misplaced} declare side=… and must not also appear in bands or group "
            "ranks: the framework places a side node beside the whole stack, so putting it "
            "in a row would place it twice"
        )
    for nid in sorted(aside):
        if not any(edge["from"] == nid or edge["to"] == nid for edge in contract["edges"]):
            raise VisualContractError(
                f"node {nid} declares side=… but connects to nothing; a side column only "
                "makes sense for something the rows talk to"
            )
    unplaced = sorted(
        nid for nid, count in placed.items() if count == 0 and nid not in aside
    )
    if unplaced:
        raise VisualContractError(
            f"nodes declared but never placed in a group rank or a band: {unplaced}; "
            "every node must appear exactly once in the layout"
        )
    twice = sorted(nid for nid, count in placed.items() if count > 1)
    if twice:
        raise VisualContractError(f"nodes placed more than once: {twice}")

    # 只有**顶层**组进 band；子组的位置由它的父组决定。
    top_level = sorted(gid for gid, parent in parent_of.items() if parent is None)
    missing_groups = sorted(set(top_level) - set(band_groups))
    if missing_groups:
        raise VisualContractError(
            f"top-level groups declared but not placed in any band: {missing_groups}"
        )
    nested_in_band = sorted(set(band_groups) - set(top_level))
    if nested_in_band:
        raise VisualContractError(
            f"nested groups may not be placed in a band directly: {nested_in_band}; "
            "their position comes from their parent group"
        )
    dupe_band_groups = sorted({g for g in band_groups if band_groups.count(g) > 1})
    if dupe_band_groups:
        raise VisualContractError(f"groups placed in more than one band: {dupe_band_groups}")

    degree = {nid: 0 for nid in node_ids}
    group_set = set(group_ids)
    for index, edge in enumerate(contract["edges"]):
        for end in ("from", "to"):
            target = edge[end]
            if target.startswith("group:"):
                gid = target[len("group:"):]
                if gid not in group_set:
                    raise VisualContractError(
                        f"edges[{index}].{end}={target!r} names no group. "
                        f"Declared groups: {sorted(group_set)}"
                    )
                # 连到组就是连到它的全体成员 —— 否则那些成员会被判成孤儿。
                for member in _group_member_ids(contract, gid):
                    if member in degree:
                        degree[member] += 1
                continue
            if target in known:
                continue
            # **这个名字在别处存在吗。** 光列「合法的是这些」，作者无从知道
            # 自己写的词到底是拼错了、还是写对了但放错了位置。2026-09-17 实测：
            # 模型写 to="biz" 想画「网关调用整个业务层」，而 biz 正是它上一行
            # 刚声明的组；报错只列了 22 个节点 id，它再没找回来。
            if target in group_set:
                raise VisualContractError(
                    f"edges[{index}].{end}={target!r} is a group, not a node. "
                    f"To draw one line to the whole group, write "
                    f"'group:{target}' — that is one edge instead of one per member, "
                    "and it is the single biggest thing you can do about crossings in "
                    "a dense figure. To connect one component inside it, name that "
                    "component."
                )
            raise VisualContractError(
                f"edges[{index}].{end} references unknown node {target!r}; "
                f"declared nodes: {sorted(known)}"
            )
        # 自己指向自己曾经被一律拒绝，理由是「只会画成一个点」。那个理由在
        # 2026-09-17 失效了：框架现在把它画成节点正上方的小回环。而状态机里
        # 「心跳续期」「重试」本来就是自转移 —— 拒掉它，状态机根本画不出来，
        # agent 只能去试时序模式绕道。**拒绝的理由消失时，拒绝也要跟着撤销。**
        for end in ("from", "to"):
            if not edge[end].startswith("group:"):
                degree[edge[end]] += 1

    # 悬空组件：v1 的两颗 CPU 就死在这里 —— 画在图上、八版没有一条线、
    # 八次审图无人报。现在它是一条机械拒绝。
    # 「全都是主角」= 没有主角。强调是一个**相对关系**，它需要一个安静的多数
    # 做底 —— 全体 primary 时这个声明无法成立，这是自相矛盾，不是审美判断
    # （拒绝分支只加在自相矛盾上，这条落在那儿）。
    emphasised = [n for n in nodes if n.get("emphasis") == "primary"]
    if nodes and len(emphasised) == len(nodes):
        raise VisualContractError(
            "every node is emphasis=primary — emphasis is a relation, and a relation "
            "that holds for everything distinguishes nothing. Mark the one or two "
            "things the reader should look at first, and leave the rest unmarked "
            f"({len(nodes)} nodes)."
        )

    # 引出线也是线。这条拒绝的理由是「画在图上、一条线都没有」—— 一个挂着
    # annotation 的节点旁边就画着一支箭头加一句话，那个理由在它身上不成立，
    # 于是拒绝也跟着撤销（同上面自环那条）。
    #
    # 2026-09-17：这不是假想。清单教的「图太密时把细节那层折叠到另一栏」，细节
    # 栏朝外的连接**只能**写成 annotation（边是按 block 校验的，跨栏边不存在）。
    # 按教的做，那些只朝外连的服务就全成了「孤儿」——框架教一个技法，然后拒绝
    # 用了这个技法的合同。22 节点 / 65 边那张图的最优解就卡在这儿。
    for annotation in contract.get("annotations") or []:
        anchor = annotation.get("anchor")
        if anchor in degree:
            degree[anchor] += 1

    orphans = sorted(
        node["id"]
        for node in nodes
        if degree[node["id"]] == 0 and not node["isolated"]
    )
    if orphans:
        raise VisualContractError(
            f"nodes are drawn but connected to nothing: {orphans}; either declare the "
            "edges that connect them, or mark them isolated=true if standing alone is "
            "the intended meaning"
        )


def _check_asserted_structure(contract: dict[str, Any]) -> None:
    panels = contract.get("panels") or []
    series = contract.get("series") or []
    panel_ids = [panel["id"] for panel in panels]
    dupes = sorted({pid for pid in panel_ids if panel_ids.count(pid) > 1})
    if dupes:
        raise VisualContractError(f"duplicate panel ids: {dupes}")
    series_ids = [item["id"] for item in series]
    dupe_series = sorted({sid for sid in series_ids if series_ids.count(sid) > 1})
    if dupe_series:
        raise VisualContractError(f"duplicate series ids: {dupe_series}")
    if panels:
        for item in series:
            if item["panel"] and item["panel"] not in set(panel_ids):
                raise VisualContractError(
                    f"series {item['id']!r} targets unknown panel {item['panel']!r}; "
                    f"declared panels: {panel_ids}"
                )
    if not panels and not series:
        raise VisualContractError(
            "an asserted-family contract must declare panels and/or series: without "
            "them there is nothing for the object-model check to compare against"
        )
    # ── 数据图的设计合同（2026-09-18 iter11）──────────────────────────────
    # 8 面板 5.2pt、y 轴无单位、六根柱无图例：这些过去只核「对象模型自洽」
    # （面板数/序列数对得上），难看但自洽就通过。设计约束里能机械判的三件事
    # 在这里判，其余（图例在场、字号、配色）渲染后从对象模型对账。
    if len(panels) > MAX_PANELS:
        raise VisualContractError(
            f"{len(panels)} panels declared but a single figure carries at most "
            f"{MAX_PANELS}: at print size every panel of an 8-panel figure is a "
            "postage stamp (iter11 measured 5.2pt ticks). Split into several "
            "figures — one per model / per network family — each with ≤ "
            f"{MAX_PANELS} panels, and declare each as its own contract."
        )
    if contract.get("family") == "quantitative":
        for panel in panels:
            axes = panel.get("axes") or {}
            for axis in ("x", "y"):
                spec = axes.get(axis)
                if not spec or not spec.get("label"):
                    raise VisualContractError(
                        f"panels[{panel['id']}].axes.{axis} must declare a label "
                        "(what the axis measures): a reader cannot use an axis "
                        "that is not named. Write axes={\"x\": {\"label\": ..., "
                        f"\"unit\": ...}}, \"y\": {{...}}}}; the unit may be {NO_UNIT!r} "
                        "for a categorical or dimensionless axis"
                    )
                if not spec.get("unit"):
                    raise VisualContractError(
                        f"panels[{panel['id']}].axes.{axis} must declare a unit "
                        f"(e.g. 'ms', 'K', 'MPa') or explicitly {NO_UNIT!r} for a "
                        "categorical / dimensionless axis. A number without a unit "
                        "is not a measurement, and an empty field cannot tell "
                        "'forgot' from 'has none'"
                    )
    if len(series) > 1:
        labels = [item["label"] for item in series]
        if any(not label for label in labels):
            raise VisualContractError(
                "every series must carry a label when a figure has more than one: "
                "the legend is built from these labels, and a reader cannot tell "
                "unlabelled series apart"
            )
        dupes = sorted({label for label in labels if labels.count(label) > 1})
        if dupes:
            raise VisualContractError(
                f"series labels must be distinct, these repeat: {dupes} — two legend "
                "entries with the same name are one entry to the reader"
            )


# ── 断言求值 ──────────────────────────────────────────────────────────────
def _select_nodes(contract: dict[str, Any], selector: dict[str, Any]) -> list[str]:
    nodes = contract.get("nodes") or []
    group_of: dict[str, str] = {}
    for group in contract.get("groups") or []:
        for rank in group["ranks"]:
            for nid in rank:
                group_of[nid] = group["id"]
    out: list[str] = []
    for node in nodes:
        if "role" in selector and node["role"] != selector["role"]:
            continue
        if "group" in selector and group_of.get(node["id"]) != selector["group"]:
            continue
        if "ids" in selector and node["id"] not in set(selector["ids"]):
            continue
        out.append(node["id"])
    return out


def _compare(actual: Any, comparator: str, expected: Any) -> bool:
    if comparator == "equals":
        return actual == expected
    if comparator == "at_least":
        return actual >= expected
    return actual <= expected


def evaluate_assertions(
    contract: dict[str, Any], observed: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """逐条求值，返回结果清单（holds/actual/expected）。

    compiled 家族对**合同自己的图**求值 —— 因为渲染就是从这张图来的，核合同
    等于核图。asserted 家族对 `observed`（渲染后读出的 matplotlib 对象模型）
    求值 —— 那是代码画的，必须真的去读。
    """

    observed = observed or {}
    results: list[dict[str, Any]] = []
    edges = contract.get("edges") or []

    for assertion in contract.get("assertions") or []:
        kind = assertion["kind"]
        actual: Any = None
        detail: dict[str, Any] = {}
        try:
            if kind == "node_count":
                # **把选中的是谁留下来。** 只报「期望 1 实得 8」时，作者无从判断
                # 是选择器写宽了还是图画错了 —— 2026-09-17 实测：同一条断言被拒
                # 两次、错误信息一字不差，agent 两次都没改对（16 轮 / 698k）。
                matched = _select_nodes(contract, assertion.get("selector") or {})
                actual = len(matched)
                detail["matched"] = sorted(matched)[:12]
            elif kind == "group_count":
                actual = len(contract.get("groups") or [])
            elif kind == "edge_count":
                source = set(_select_nodes(contract, assertion.get("source") or {}))
                target = set(_select_nodes(contract, assertion.get("target") or {}))
                hits = [
                    f"{edge['from']}→{edge['to']}"
                    for edge in edges
                    if (edge["from"] in source and edge["to"] in target)
                    or (edge["from"] in target and edge["to"] in source)
                ]
                actual = len(hits)
                detail["matched"] = hits[:12]
            elif kind == "degree" and assertion.get("node"):
                node_id = assertion["node"]
                # **数进去的是哪几条，列出来。** degree 数的是这个节点上的全部
                # 边 —— 入边、存储读写、上报，一条不落。作者心里想的常常是
                # 「它调用了几个服务」，两者差得远。2026-09-17 实测：同一条
                # order-calls 断言被拒四次（5→8→5→8），报错里那句
                # 「the list above is what it actually matched」上面**根本没有
                # list** —— degree 这一支从不记 matched。它只能猜，猜了四次。
                hits = [
                    f"{edge['from']}→{edge['to']}"
                    for edge in edges
                    if node_id in (edge["from"], edge["to"])
                ]
                actual = len(hits)
                detail["matched"] = hits[:12]
            elif kind == "degree":
                selected = _select_nodes(contract, assertion.get("selector") or {})
                degrees = {
                    nid: sum(1 for edge in edges if nid in (edge["from"], edge["to"]))
                    for nid in selected
                }
                offenders = {
                    nid: value
                    for nid, value in degrees.items()
                    if not _compare(value, assertion["comparator"], assertion["expected"])
                }
                # degree 是「每个都要满足」：actual 报不满足的个数，0 = 成立。
                actual = len(offenders)
                detail = {
                    "checked_nodes": len(selected),
                    "offenders": offenders,
                    # 选择器那一支也要交出「选中的是谁」—— 不然作者分不清是
                    # 选择器写宽了还是图画错了。
                    "matched": [f"{nid}:{degrees[nid]}" for nid in sorted(offenders)][:12],
                }
                results.append(
                    {
                        **_assertion_head(assertion),
                        "holds": not offenders and bool(selected),
                        "actual": f"{len(selected) - len(offenders)}/{len(selected)} nodes satisfy it",
                        **detail,
                    }
                )
                continue
            elif kind == "neighbors":
                node_id = assertion.get("node") or ""
                within = set(_select_nodes(contract, assertion.get("within") or {}))
                hits = [
                    f"{edge['from']}→{edge['to']}"
                    for edge in edges
                    if (edge["from"] == node_id and edge["to"] in within)
                    or (edge["to"] == node_id and edge["from"] in within)
                ]
                actual = len(hits)
                detail["matched"] = hits[:12]
            elif kind == "series_count":
                actual = int(observed.get("series_count", -1))
            elif kind == "panel_count":
                actual = int(observed.get("panel_count", -1))
            elif kind == "axis_scale":
                scales = observed.get("axis_scales") or {}
                actual = scales.get(assertion.get("axis") or "y")
            else:  # pragma: no cover - ASSERTION_KINDS is exhaustive
                raise VisualContractError(f"unevaluated assertion kind {kind!r}")
        except VisualContractError:
            raise
        except (TypeError, ValueError) as exc:
            results.append(
                {**_assertion_head(assertion), "holds": False, "actual": f"error: {exc}"}
            )
            continue

        holds = _compare(actual, assertion["comparator"], assertion["expected"])
        entry = {**_assertion_head(assertion), "holds": bool(holds), "actual": actual}
        # 证据只在**不成立**时带回：成立时它是噪声，不成立时它是作者唯一能据以
        # 判断「是选择器写宽了还是图画错了」的东西。
        if not holds and detail:
            entry["detail"] = detail
        results.append(entry)
    return results


def _assertion_head(assertion: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": assertion["id"],
        "kind": assertion["kind"],
        "comparator": assertion["comparator"],
        "expected": assertion["expected"],
        "derived_from": assertion["derived_from"],
    }


def failed_assertions(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in results if not item.get("holds")]


# ── 覆盖：需求原文里的「量」有没有在合同里出现 ─────────────────────────────
# ── 词表之外（合同说不出来的事实）──────────────────────────────────────
# 合同是一门**有限的**描述语言。模型想说一件它没有词的事（「这条关系是 1:N」
# 「这道工序持续 3 天」「这一行是运维的泳道」），过去会发生一件最坏的事：
# 归一化把这个键**静默丢掉**，图照常渲染，所有判据全绿 —— 图上少了这件事，
# 而没有任何一处说过它少了（2026-09-17 实测：duration_days / cardinality /
# swimlane / weight / multiplicity 五个键全部无声蒸发）。
#
# 这里既不拒也不忍：**照画，并且点名**。拒了模型会把这件事删掉重来（状态机
# 那轮就是这么把一条边说没的）；忍了就等于词表的缺口永远不会被发现。点名之
# 后，这条 finding 同时是给读者的披露、和给我的词表待办。
#: 顶层合法键。**schema 里声明的每一个键都必须在这儿**，否则框架会一边教作者
#: 写它、一边告诉他「这个键词表里没有，已被静默丢弃」。
#: 2026-09-17 实测代价：`medium` 加进了 schema、example、家族清单，唯独漏了
#: 这张表 —— agent 按 schema 写 medium，判据每次都说它不存在，于是**九次声明、
#: 14 轮、505k token**（全程最差）。`test_every_schema_key_is_a_real_word`
#: 从此机械地守住这条。
_TOP_KEYS_COMPILED = frozenset({
    "title", "assertions", "nodes", "groups", "bands", "edges", "notes",
    "annotations", "blocks", "unexpressed", "medium", "medium_source", "text_language",
})
_TOP_KEYS_ASSERTED = frozenset({
    "title", "assertions", "panels", "series", "unexpressed", "medium", "medium_source",
    "text_language",
})
_SHAPE_KEYS: dict[str, frozenset[str]] = {
    "nodes": frozenset({"id", "label", "sublabel", "shape", "ports", "role",
                        "isolated", "detail_of", "side", "emphasis"}),
    "edges": frozenset({"from", "to", "label", "role", "kind", "bidirectional",
                        "represents", "step"}),
    "groups": frozenset({"id", "label", "ranks", "label_position", "style", "parent"}),
    "annotations": frozenset({"anchor", "side", "text", "represents"}),
    "assertions": frozenset({"id", "kind", "derived_from", "equals", "at_least",
                             "at_most", "selector", "source", "target", "within",
                             "node", "axis"}),
    "panels": frozenset({"id", "label", "message", "axes"}),
    "series": frozenset({"id", "label", "panel", "source_field"}),
}
_BLOCK_KEYS: dict[str, frozenset[str]] = {
    "diagram": frozenset({"kind", "title", "row", "nodes", "groups", "bands",
                          "edges", "annotations"}),
    "spec": frozenset({"kind", "title", "row", "items", "columns"}),
    "note": frozenset({"kind", "title", "row", "text"}),
}


def _collect_unknown(items: Any, allowed: frozenset[str], where: str,
                     sink: list[tuple[str, str]]) -> None:
    if not isinstance(items, list):
        return
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        for key in sorted(set(item) - allowed):
            sink.append((f"{where}[{index}].{key}", str(item.get(key))[:60]))


def dropped_findings(raw: Any, normalized: Any) -> list[dict[str, Any]]:
    """词表**认识**、作者也填了、可归一化之后没了的顶层键。

    `unexpressed_findings` 问的是「这个键认不认识」；它答不了「这个键到没到纸上」。
    2026-09-17 实测：顶层 `notes` 是 schema 里的合法键，但在「写了 blocks」那条
    分支上从来没被搬过来 —— 作者写的页脚一句话凭空消失，两条判据一条都没响。

    **一个键有两条归一化路径时，只要有一条忘了它，它就会在那条路径上静默消失。**
    这条判据扫的是结果，所以不管将来加多少条分支，都问得住。
    """

    if not isinstance(raw, dict) or not isinstance(normalized, dict):
        return []
    lost: list[str] = []
    for key in sorted(_TOP_KEYS_COMPILED | _TOP_KEYS_ASSERTED):
        if key in ("unexpressed", "family", "kind"):
            continue
        before = raw.get(key)
        if before in (None, "", [], {}):
            continue
        after = normalized.get(key)
        if after in (None, "", [], {}):
            lost.append(key)
    if not lost:
        return []
    return [{
        "code": "contract.dropped_keys",
        "severity": "major",
        "detail": (
            "这些键词表是认识的、你也填了，但归一化之后它们空了 —— 图上不会有"
            "对应的东西，而这不是你写错了，是框架在这条分支上漏接："
            + "、".join(lost)
            + "。这是框架缺陷，请如实报告，不要改写合同去绕开它。"
        ),
        "paths": lost,
    }]


def empty_row_findings(normalized: Any) -> list[dict[str, Any]]:
    """声明里那些**什么也不含的行** —— 照画（丢掉它们），并且点名。

    空的 rank / band 不与任何东西矛盾，所以不该拒（拒一次要付一个完整往返，
    2026-09-17 实测那个往返里 agent 又把别处弄坏了）。但也不能一声不吭地丢：
    作者写下一行，多半是**本来想往里放东西**，只是编辑时漏了。说出来，
    他自己判断是补内容还是本来就多余。
    """

    if not isinstance(normalized, dict):
        return []
    bands = int(normalized.get("dropped_empty_bands") or 0)
    ranks = sum(
        int(group.get("dropped_empty_ranks") or 0)
        for group in normalized.get("groups") or []
        if isinstance(group, dict)
    )
    if not bands and not ranks:
        return []
    parts = []
    if bands:
        parts.append(f"{bands} 条空的顶层带子")
    if ranks:
        parts.append(f"{ranks} 行空的组内 rank")
    return [{
        "code": "contract.empty_rows_dropped",
        "severity": "minor",
        "detail": (
            "合同里有" + "、".join(parts) + "：空行什么也不含，画不出任何东西，"
            "已经丢掉、图照出。**但你写下那一行多半是本来想往里放东西** —— "
            "如果是编辑时漏了，补上节点 id；如果本来就多余，删掉它，"
            "下一版就不会再被点名。"
        ),
        "dropped_empty_bands": bands,
        "dropped_empty_ranks": ranks,
    }]


def redrawn_component_findings(normalized: Any) -> list[dict[str, Any]]:
    """同一个部件在两个 block 里各画了一遍，而没有任何东西说它们是同一个。

    2026-09-17 实测：微服务题跑出 28 个节点，其中 6 个是重复的 —— ① 栏画了
    「订单/支付/库存/风控/优惠券/搜索」，② 栏又用 order2/pay2/... 这套新 id 把
    同样六个画了一遍。读者看见两个「订单」，无从知道是同一个东西的两种画法，
    还是两个不同的东西。**而这件事一条 finding 都没有人报。**

    `detail_of` 存在的意义正是命名这个关系（「① 栏这个盒子是 ② 栏那个组的折叠
    形态」），图上会标出「详见 ②」。这里不拒绝 —— 两个东西真的同名是可能的，
    那不是自相矛盾；但它也绝不该悄无声息。
    """

    if not isinstance(normalized, dict):
        return []
    blocks = normalized.get("blocks") or []
    if len(blocks) < 2:
        return []

    # 已经用 detail_of 认领过的组，它的成员就算说清楚了。
    claimed: set[str] = set()
    for block in blocks:
        for node in block.get("nodes") or []:
            target = node.get("detail_of")
            if target:
                claimed.add(target)
                claimed.update(_group_member_ids(normalized, target))

    seen: dict[str, list[tuple[int, str]]] = {}
    for index, block in enumerate(blocks):
        for node in block.get("nodes") or []:
            label = str(node.get("label") or "").strip()
            if label:
                seen.setdefault(label, []).append((index, node["id"]))

    redrawn = [
        (label, sorted(nid for _, nid in places))
        for label, places in sorted(seen.items())
        if len({index for index, _ in places}) >= 2
        and not any(nid in claimed for _, nid in places)
    ]
    if not redrawn:
        return []
    # **一条，不是六条。** 这是一个决定（② 栏把 ① 栏的部件重画了一遍），不是
    # 六个各自独立的缺陷 —— 六条同形的话会把别的 finding 埋掉。
    return [
        {
            "collector": "OB-CONTRACT-COVERAGE",
            "redrawn": [label for label, _ in redrawn],
            "message": (
                f"{len(redrawn)} component(s) are drawn in more than one block with "
                "nothing saying they are the same thing: "
                + "; ".join(f"{label} as {ids}" for label, ids in redrawn[:6])
                + ". A reader sees two boxes carrying one name. If the second block is "
                "the expanded form of the first, say so: give the overview node "
                "detail_of=<the group in the other block>, and the figure will mark it "
                "'详见 ②'. If they really are different things, give them different "
                "labels."
            ),
        }
    ]


def unroled_edge_findings(normalized: Any) -> list[dict[str, Any]]:
    """有些边写了 `role`、有些没写 —— 没写的那些在图例里没有任何解释。

    图例是按 role 出的。一张图里只要**有**边带 role，图例就会出现；这时候
    不带 role 的边仍然会被画出来，颜色落到默认档，而图例里没有任何一条对应
    它 —— 读者看到一种线，纸上没有任何地方说它是什么。

    2026-09-17 实测：用户点名「高级得多」的参考图 22 条边**全部**带 role；
    我们真跑连着两轮都是「9 条有、8 条没有」（没有的正是 switch→GPU 那 8 条）。
    根因还是 example：它自己就让四条挂着空 role，而模型照抄示范。

    **全都不带 role 不报** —— 那种图根本不出图例，没有「说了一半」的问题。
    报的只是**说了一半**：既然你已经在用图例解释线的种类，就别留下没人认领的线。
    """

    if not isinstance(normalized, dict):
        return []
    edges = [
        edge
        for block in (normalized.get("blocks") or [])
        for edge in (block.get("edges") or [])
    ] or list(normalized.get("edges") or [])
    if not edges:
        return []
    roled = [e for e in edges if str(e.get("role") or "").strip()]
    bare = [e for e in edges if not str(e.get("role") or "").strip()]
    if not roled or not bare:
        return []
    sample = "、".join(f"{e.get('from')}→{e.get('to')}" for e in bare[:5])
    return [{
        "code": "contract.edges_without_role",
        "severity": "major",
        "detail": (
            f"{len(bare)} 条边没有写 `role`，而另外 {len(roled)} 条写了 —— "
            "图例是按 role 出的，所以这几条会被画出来、却在图例里没有任何解释："
            + sample + ("…" if len(bare) > 5 else "")
            + "。读者看到一种线，纸上没有地方说它是什么。给它们补一个 role"
            "（哪怕和别的边同名），或者把全图的 role 都去掉（那样就不出图例）。"
        ),
        "without_role": len(bare),
        "with_role": len(roled),
    }]


def unexpressed_findings(raw: Any, family: str) -> list[dict[str, Any]]:
    """模型在合同里写了、而词表接不住的键 —— 每一条都是图上少掉的一件事。

    走的是**原始**合同（归一化之前），因为归一化正是丢键的那一步。
    """

    if not isinstance(raw, dict):
        return []
    sink: list[tuple[str, str]] = []
    compiled = family in COMPILED_FAMILIES
    top_allowed = _TOP_KEYS_COMPILED if compiled else _TOP_KEYS_ASSERTED
    for key in sorted(set(raw) - top_allowed):
        sink.append((key, str(raw.get(key))[:60]))

    def walk(container: Any, where: str) -> None:
        for field, allowed in _SHAPE_KEYS.items():
            if field in container:
                _collect_unknown(container[field], allowed, f"{where}{field}", sink)

    walk(raw, "")
    for b_index, block in enumerate(raw.get("blocks") or []):
        if not isinstance(block, dict):
            continue
        kind = str(block.get("kind") or "diagram").strip().lower()
        allowed = _BLOCK_KEYS.get(kind)
        if allowed is not None:
            for key in sorted(set(block) - allowed):
                sink.append((f"blocks[{b_index}].{key}", str(block.get(key))[:60]))
        walk(block, f"blocks[{b_index}].")

    findings: list[dict[str, Any]] = []
    if sink:
        findings.append({
            "code": "contract.unexpressed_keys",
            "severity": "major",
            "detail": (
                "这份合同写了 " + str(len(sink)) + " 个词表里没有的键，它们被静默丢弃，"
                "图上不会出现对应的东西：" +
                "；".join(f"{path}={value!r}" for path, value in sink[:8]) +
                ("…" if len(sink) > 8 else "") +
                "。要么用现有词表把这件事说出来（见 figure_contract_schema 的 "
                "example），要么把它写进 unexpressed 里显式承认合同表达不了 —— "
                "别让它无声消失。"
            ),
            "paths": [path for path, _value in sink],
        })

    declared = raw.get("unexpressed")
    if declared:
        entries = declared if isinstance(declared, list) else [declared]
        findings.append({
            "code": "contract.unexpressed_declared",
            "severity": "major",
            "detail": (
                "作者声明这张图有合同表达不了的事实，图上因此缺这几件事：" +
                "；".join(str(item)[:120] for item in entries) +
                "。这是词表缺口，不是作者的疏忽 —— 记录如实披露。"
            ),
            "paths": [str(item)[:120] for item in entries],
        })
    return findings


def coverage_findings(contract: dict[str, Any], request_text: str) -> list[dict[str, Any]]:
    """需求文本里写了、合同里一个字都没提的「量」= 机械可查的覆盖缺口。

    只记 finding 不拒绝：需求里也会有与图无关的数字（日期、编号）。它的价值
    是让「悄悄漏掉一条约束」在账上留下痕迹 —— v1 里没有任何东西记录过
    「这版比上一版少表达了什么」。
    """

    text = str(request_text or "")
    if not text.strip():
        return []
    # 比的是**数字集合**，不是子串：子串匹配下「4」能在「400」里找到，于是
    # 一条真漏掉的约束看起来像已覆盖（假阴性）。
    expressed = {
        _as_number(match.group(1))
        for match in _QUANTITY_RE.finditer(_contract_text(contract))
    }
    # 断言引用的**需求原文**。跟「合同文本里出现过这个数」是两回事：
    # 2026-09-17 实测同一条 prompt 三次，图三次一模一样，而「双CPU」只有一次
    # 被写成断言 —— 图是对的，随机的是**有没有人去核它**。
    #
    # 第一版按**数字**比对，当场被一个变异打穿：把 g0 从 sw0 改接到 sw1
    # （扇出 3/5）、再删掉扇出断言 —— 全线静默，因为数字 4 还在别处
    # （服务器数=4）。**数字对得上，不代表那条需求有人核。** 改成按
    # 「数+量词」在 derived_from 里找：断言引用哪条需求，就只算覆盖了哪条。
    # **比对前先挤掉空白**：同一条需求，agent 有时写「插上8张」有时写
    # 「插上 8 张」，子串匹配会当成两回事 —— 一上来就误报了 iter31 的四条
    # 其实核过的需求。出门检查那次也栽在同一个地方（2026-09-17，同一天第二次）。
    quoted_requirements = "".join(
        "".join(str(assertion.get("derived_from") or "").split())
        for assertion in contract.get("assertions") or []
    )
    seen: set[str] = set()
    missing: list[str] = []
    uncounted: list[str] = []
    for match in _QUANTITY_RE.finditer(text):
        number = _as_number(match.group(1))
        unit = (match.group(2) or "").strip()
        token = f"{match.group(1)}{unit}"
        # 中文里有两种「看着像数量、其实不是」的说法，都得排掉，否则每条 prompt
        # 都会误报（2026-09-17 实测，两次都是我自己的判据咬自己）：
        #   「画一张…图」—— 数的是**图本身**，不是图里的东西；
        #   「第一个节点」—— 序数，不是个数。
        before = text[max(0, match.start() - 2):match.start()].strip()[-1:]
        if before in {"画", "绘", "制", "第"}:
            continue
        if token in seen:
            continue
        seen.add(token)
        if number not in expressed:
            missing.append(token)
        elif unit in _COUNTING_UNITS and not (
            "".join(token.split()) in quoted_requirements
            or f"{number}{unit}" in quoted_requirements
        ):
            # 带量词 = 在数东西 = 图上可数 = 应当有断言核。400Gbps 这种量纲不算。
            uncounted.append(token)
    out: list[dict[str, Any]] = []
    if missing:
        out.append(
            {
                "collector": "OB-CONTRACT-COVERAGE",
                "message": (
                    "quantities appear in the request but nowhere in the contract: "
                    + ", ".join(missing)
                    + "; either express them in the contract (labels, counts, assertions) "
                    "or they are absent from the figure"
                ),
                "uncovered_quantities": missing,
            }
        )
    if uncounted:
        out.append(
            {
                "collector": "OB-CONTRACT-COVERAGE",
                "message": (
                    "no assertion cites these requirements: "
                    + ", ".join(uncounted)
                    + "; each is a countable fact the figure draws, and a drawn number "
                    "nobody checks goes wrong silently — add a node_count or neighbors "
                    "assertion whose derived_from quotes that part of the request"
                ),
                "counts_without_assertion": uncounted,
            }
        )
    return out


def _contract_text(contract: dict[str, Any]) -> str:
    parts: list[str] = [str(contract.get("title") or "")]
    for node in contract.get("nodes") or []:
        parts += [node["label"], node["sublabel"], node["role"]]
    for group in contract.get("groups") or []:
        parts.append(group["label"])
    for edge in contract.get("edges") or []:
        parts.append(edge["label"])
    for panel in contract.get("panels") or []:
        parts += [panel["label"], panel["message"]]
    for item in contract.get("series") or []:
        parts += [item["label"], item["source_field"]]
    for assertion in contract.get("assertions") or []:
        # **不含 derived_from**：那是需求原文的引用。把它算进「合同表达了什么」
        # 会让覆盖检查靠抄一句话就通过 —— 引用了需求不等于在图里表达了它。
        parts += [str(assertion["expected"]), assertion["id"]]
    parts += [str(item) for item in contract.get("notes") or []]
    # 节点/边的条数本身也是「量」：声明了 8 个 gpu 就等于表达了「8 张」。
    parts += [
        str(len(contract.get("nodes") or [])),
        str(len(contract.get("edges") or [])),
        str(len(contract.get("groups") or [])),
        str(len(contract.get("panels") or [])),
        str(len(contract.get("series") or [])),
    ]
    for role in {node["role"] for node in contract.get("nodes") or []}:
        parts.append(str(len(_select_nodes(contract, {"role": role}))))
    return " ".join(parts)


# ── 跨版本回归 ────────────────────────────────────────────────────────────
#: 重新声明时**会丢**的那些可选字段。它们都是「写了才有、不写也合法」的，
#: 所以丢了不会报错、不会拒绝、图照出 —— 只是少了一件事，而没有人知道。
_OPTIONAL_NODE_FIELDS = ("sublabel", "ports", "detail_of", "side", "emphasis", "shape")
_OPTIONAL_EDGE_FIELDS = ("label", "role", "represents", "step", "kind")
_OPTIONAL_TOP_FIELDS = ("medium", "notes", "unexpressed", "title")


def redeclaration_losses(
    previous: dict[str, Any] | None, current: dict[str, Any]
) -> list[dict[str, Any]]:
    """上一版声明里有、这一版没了的**字段**。

    `contract_diff` 比的是断言和节点/边的**数量**，而且只在 render 时跑（拿的是
    上一版 figure 记录）。可是丢失发生在**两次 declare 之间** —— 那一段从来没
    有任何东西比较过。

    2026-09-17 实测三轮：iter50 与 iter51 都在第二次声明时丢掉 `medium`
    （第一版写了 slide，第二版没有了，几何一字不差）；iter49 修断言时丢掉
    `bands`、下一版又把 ranks 弄空。**模型重发整份合同时会掉东西**，
    而这些字段「不写也合法」，所以掉了悄无声息。

    只报**丢失**，不报新增：加东西是作者的自由，减东西才需要他确认一次。
    """

    if not previous:
        return []
    lost: list[str] = []

    def author_said(contract: dict[str, Any], key: str) -> Any:
        # `medium` 有两个来源：作者写的，和框架按 purpose 推的（iter48 起推出来的
        # 也会被写进合同，好让记录里看得见按哪个版心判）。比较**丢没丢**时只能看
        # 作者写的那一份 —— 否则推导值会顶在那里，把丢失挡得严严实实。
        # （我自己上一个修复挡住了这个检查，测试当场抓到。）
        if key == "medium" and contract.get("medium_source") in ("derived", "caller"):
            return None
        return contract.get(key)

    for key in _OPTIONAL_TOP_FIELDS:
        if author_said(previous, key) and not author_said(current, key):
            lost.append(key)

    def by_id(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {str(n.get("id")): n for n in contract.get("nodes") or []}

    prev_nodes, curr_nodes = by_id(previous), by_id(current)
    for node_id in sorted(set(prev_nodes) & set(curr_nodes)):
        for field in _OPTIONAL_NODE_FIELDS:
            before = prev_nodes[node_id].get(field)
            after = curr_nodes[node_id].get(field)
            # shape 的默认值是 "box"，把它当成「没写」
            if field == "shape":
                before = None if before in (None, "", "box") else before
                after = None if after in (None, "", "box") else after
            if before and not after:
                lost.append(f"nodes[{node_id}].{field}")

    def by_ends(contract: dict[str, Any]) -> dict[tuple, dict[str, Any]]:
        return {(e.get("from"), e.get("to")): e for e in contract.get("edges") or []}

    prev_edges, curr_edges = by_ends(previous), by_ends(current)
    for ends in sorted(set(prev_edges) & set(curr_edges), key=repr):
        for field in _OPTIONAL_EDGE_FIELDS:
            before = prev_edges[ends].get(field)
            after = curr_edges[ends].get(field)
            if field == "represents":
                before = None if before in (None, 0, 1) else before
                after = None if after in (None, 0, 1) else after
            if field == "kind":
                before = None if before in (None, "", "link") else before
                after = None if after in (None, "", "link") else after
            if before and not after:
                lost.append(f"edges[{ends[0]}→{ends[1]}].{field}")

    if not lost:
        return []
    return [{
        "code": "contract.redeclaration_losses",
        "severity": "major",
        "detail": (
            f"这一版声明比上一版少了 {len(lost)} 件事，它们都是「写了才有、不写"
            "也合法」的字段 —— 所以丢了不报错、不拒绝、图照出，只是图上少了一件事："
            + "、".join(lost[:10]) + ("…" if len(lost) > 10 else "")
            + "。重发整份合同时掉东西是常事；如果是有意去掉的，忽略这条；"
            "如果不是，把它们补回来。"
        ),
        "lost": lost,
    }]


def contract_diff(previous: dict[str, Any] | None, current: dict[str, Any]) -> list[dict[str, Any]]:
    """同一个 request_id 的上一版合同 → 这一版，少了什么、弱了什么。

    这是「越改越差」的解药。v1 的八版里没有任何东西比较相邻两版，所以
    「4 条 GPU 连线塌成 1 条」「上联从网卡挪到机箱边框」这类回归完全不可见，
    只要新一版的渲染审计干净就算过。
    """

    if not previous:
        return []
    findings: list[dict[str, Any]] = []

    prev_assertions = {item["id"]: item for item in previous.get("assertions") or []}
    curr_assertions = {item["id"]: item for item in current.get("assertions") or []}
    dropped = sorted(set(prev_assertions) - set(curr_assertions))
    if dropped:
        findings.append(
            {
                "collector": "OB-CONTRACT-REGRESSION",
                "message": (
                    "assertions present in the previous version of this figure are gone: "
                    + ", ".join(dropped)
                    + "; dropping a requirement is a regression unless it was wrong"
                ),
                "dropped_assertions": [
                    {"id": aid, "derived_from": prev_assertions[aid]["derived_from"]}
                    for aid in dropped
                ],
            }
        )
    weakened = []
    for aid in sorted(set(prev_assertions) & set(curr_assertions)):
        before, after = prev_assertions[aid], curr_assertions[aid]
        if (before["comparator"], before["expected"]) != (
            after["comparator"],
            after["expected"],
        ):
            weakened.append(
                {
                    "id": aid,
                    "before": f"{before['comparator']} {before['expected']}",
                    "after": f"{after['comparator']} {after['expected']}",
                }
            )
    if weakened:
        findings.append(
            {
                "collector": "OB-CONTRACT-REGRESSION",
                "message": "assertion targets changed between versions",
                "changed_assertions": weakened,
            }
        )

    for label, key in (("nodes", "nodes"), ("edges", "edges"), ("groups", "groups")):
        before_n = len(previous.get(key) or [])
        after_n = len(current.get(key) or [])
        if after_n < before_n:
            findings.append(
                {
                    "collector": "OB-CONTRACT-REGRESSION",
                    "message": (
                        f"the contract declares fewer {label} than the previous version "
                        f"({before_n} → {after_n})"
                    ),
                    "element": label,
                    "before": before_n,
                    "after": after_n,
                }
            )
    return findings


def check_result_diff(
    previous: list[dict[str, Any]] | None, current: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """上一版成立、这一版不成立的断言 —— 最直接的「越改越差」信号。"""

    if not previous:
        return []
    before = {item["id"]: bool(item.get("holds")) for item in previous}
    broke = sorted(
        item["id"]
        for item in current
        if before.get(item["id"]) is True and not item.get("holds")
    )
    if not broke:
        return []
    return [
        {
            "collector": "OB-CONTRACT-REGRESSION",
            "message": (
                "assertions that held in the previous version no longer hold: "
                + ", ".join(broke)
            ),
            "broken_assertions": broke,
        }
    ]


def _medium_schema() -> dict[str, Any]:
    # 这张图在哪儿被读 —— 决定画布预算与字号下限。两个家族同一份定义。
    return {
        "type": "string",
        "enum": sorted(MEDIA),
        "description": (
            "这张图最终印在哪个版心：single_column（期刊单栏 84×200mm）/ "
            "double_column（通栏 170×200mm）/ slide（幻灯 254×143mm）/ poster（A1）。"
            "调用方在 constraints.width 里说了就按调用方的（你写了不同的会被拒）；"
            "没说时按 purpose 推（presentation→slide，其余→double_column）。"
            "**它有机械后果**：schematic 按它算「印出来之后最小的字还剩几 pt」，"
            "低于该版心下限（印刷 7pt）在声明这一刻就拒绝并给出改法；"
            "quantitative/composite 渲染后按 figsize 换算每段文字的印刷字号，"
            "低于下限拒绝铸记录。画布的 pt 数就是物理英寸数 —— 一张 10 英寸宽的"
            "图放进 170mm，8pt 的刻度只剩 5.3pt。"
        ),
    }


def contract_schema(family: str) -> dict[str, Any]:
    """合同的 JSON Schema —— 由本模块的词表生成，词表与校验同源。"""

    common = {
        "title": {"type": "string"},
        # 词表是有限的。想说的事没有词时，写在这里 —— 框架照画其余部分，并在
        # 记录里如实披露图上缺了这件事。**不写**才是错的：不认识的键会被静默
        # 丢掉，图上没有、也没有任何一处说过它没有。
        "unexpressed": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "这张图需要表达、而合同词表表达不了的事实，一条一句"
                "（例：「工序 A 持续 3 天」「这条关系是 1:N」「这一行属于运维泳道」）。"
                "写了会进记录如实披露，并计入词表待办；不写而直接自造字段 = 静默丢失。"
            ),
        },
        "assertions": {
            "type": "array",
            "description": (
                "需求里每一条可数/可判的结构约束写成一条断言；框架机械求值。"
                "每条必须带 derived_from（这条来自需求里的哪句话）。"
            ),
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "kind": {"type": "string", "enum": list(ASSERTION_KINDS)},
                    "equals": {"type": "integer"},
                    "at_least": {"type": "integer"},
                    "at_most": {"type": "integer"},
                    "selector": {"type": "object"},
                    "source": {"type": "object"},
                    "target": {"type": "object"},
                    "within": {"type": "object"},
                    "node": {"type": "string"},
                    "axis": {"type": "string"},
                    "derived_from": {"type": "string"},
                },
                "required": ["kind", "derived_from"],
            },
        },
    }
    if family in COMPILED_FAMILIES:
        return {
            "type": "object",
            "properties": {
                **common,
                "nodes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "label": {"type": "string"},
                            "sublabel": {"type": "string"},
                            "role": {"type": "string"},
                            "shape": {
                                "type": "string",
                                "enum": list(NODE_SHAPES),
                                "description": (
                                    "box=标准组件盒（默认）；chip=密排小色块（成批的同类"
                                    "元件，例如 32 张 GPU）；bar=通栏长条（共享骨干，例如"
                                    "交换机/总线）—— 形状要表意，别让所有东西一样大"
                                ),
                            },
                            "ports": {
                                "type": "integer",
                                "description": (
                                    "这个组件对外有几个接口。画成边上的小方块，而且"
                                    "**连线会真的落在口上** —— 「8 口交换机」不再靠"
                                    "读者去数线。口数与落线数两个方向都会被对账。"
                                ),
                            },
                            "side": {
                                "type": "string",
                                "enum": list(NODE_SIDES),
                                "description": (
                                    "它不站在任何一层里，而是**立在这摞层旁边**"
                                    "（外部依赖、共享服务、管理口）。塞进某一层就得"
                                    "横穿那一层；单独给它一层，别的层的线又得穿过它。"
                                ),
                            },
                            "detail_of": {
                                "type": "string",
                                "description": (
                                    "这个盒子是**另一个 block 里某个组**画开前的样子。"
                                    "写了图上会多一道内嵌边框和「详见 ①」，两栏还会对账；"
                                    "不写，读者认不出两栏讲的是同一台机器。"
                                ),
                            },
                            "emphasis": {
                                "type": "string",
                                "enum": list(NODE_EMPHASIS),
                                "description": (
                                    "图 / 地：primary=读者该先看的那一两个；"
                                    "muted=退成背景的陪衬（外部依赖、上下文）。"
                                    "不写=正常档，**多数节点都该不写** —— 强调是相对"
                                    "关系，全体 primary 会被当作自相矛盾拒绝录入。"
                                    "只动亮度与线宽，不动色相（色相已被「类别」占着）。"
                                ),
                            },
                            "isolated": {"type": "boolean"},
                        },
                        "required": ["id", "label"],
                    },
                },
                "groups": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "label": {"type": "string"},
                            "ranks": {
                                "type": "array",
                                "items": {"type": "array", "items": {"type": "string"}},
                            },
                            "label_position": {"type": "string", "enum": ["top", "bottom"]},
                            "style": {
                                "type": "string",
                                "enum": list(GROUP_STYLES),
                                "description": (
                                    "dashed = 边界/机箱（机箱、房间、信任域）；"
                                    "solid = 一个真的子系统。"
                                ),
                            },
                            "parent": {
                                "type": "string",
                                "description": (
                                    "父组 id：把一个子系统（例如「switch 0 和它的 4 张 GPU、"
                                    "1 张网卡」）声明成嵌套子组，布局器才知道它们是一伙的；"
                                    "只写边表达不了归属"
                                ),
                            },
                        },
                        "required": ["id", "ranks"],
                    },
                },
        "medium": _medium_schema(),
                "bands": {
                    "type": "array",
                    "description": "顶层的行；每项是 'group:<id>' / 'node:<id>' 的数组",
                    "items": {"type": "array", "items": {"type": "string"}},
                },
                "edges": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "from": {"type": "string"},
                            "to": {"type": "string"},
                            "label": {"type": "string"},
                            "kind": {"type": "string", "enum": ["bus", "dashed", "link"]},
                            "represents": {
                                "type": "integer",
                                "description": (
                                    "这一笔代表几条链路。图上画成并行双线，端口数与"
                                    "跨栏对账都按它算；不写就是 1 条 —— 少写了，"
                                    "图上就真的少那几条链路。"
                                ),
                            },
                            "step": {
                                "type": "integer",
                                "description": (
                                    "时序图里这条消息是第几步（1,2,3…）。写了就"
                                    "参与者横排、消息按步下行、生命线由框架画；"
                                    "同一对参与者之间来回多条消息时非写不可，"
                                    "不写会全塌成一条线。写了 step 就每条都要写。"
                                ),
                            },
                            "role": {
                                "type": "string",
                                "description": (
                                    "这条边是什么（「交换机互联」「RoCE 链路」）。"
                                    "给了就按角色配色**并进图例** —— 不然读者不知道"
                                    "两种线差在哪"
                                ),
                            },
                            "bidirectional": {"type": "boolean"},
                        },
                        "required": ["from", "to"],
                    },
                },
                "notes": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["nodes", "groups", "bands", "edges"],
        }
    axis = {
        "type": "object",
        "properties": {
            "label": {"type": "string", "description": "这根轴量的是什么（Wall time / Temperature / TP-EP strategy）"},
            "unit": {
                "type": "string",
                "description": (
                    f"单位（ms / K / MPa …）；分类轴或无量纲轴**明写** {NO_UNIT!r}。"
                    "纸上必须出现 `Label (unit)`，渲染后按对象模型对账"
                ),
            },
            "scale": {"type": "string", "enum": list(_AXIS_SCALES)},
        },
        "required": ["label", "unit"],
    }
    return {
        "type": "object",
        "properties": {
            **common,
            "medium": _medium_schema(),
            "panels": {
                "type": "array",
                "maxItems": MAX_PANELS,
                "description": (
                    f"最多 {MAX_PANELS} 个面板；更多就拆成几张图各自声明。"
                    "quantitative 的每个面板都要写 axes.x / axes.y 的 label 与 unit。"
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "label": {"type": "string", "description": "面板角标（a / b / c）"},
                        "message": {"type": "string", "description": "这个面板要让读者看出什么"},
                        "axes": {
                            "type": "object",
                            "properties": {"x": axis, "y": axis},
                        },
                    },
                    "required": ["id"],
                },
            },
            "series": {
                "type": "array",
                "description": (
                    "读者在图例里能分开的每一类数据一条。≥2 条时图例是义务：纸上"
                    "必须有一个图例、条目恰好是这些 label（不多不少、不重复）、"
                    "而且整个图例在画布之内。序列数按图例条目数（有标签的艺术家）"
                    "机械读出，不按 ax.bar 调用次数。"
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "label": {"type": "string", "description": "图例上的字，逐字"},
                        "panel": {"type": "string"},
                        "source_field": {"type": "string"},
                    },
                    "required": ["id"],
                },
            },
        },
    }


def contract_summary(contract: dict[str, Any]) -> str:
    """一行人读摘要（进 findings / 交付披露）。"""

    if contract["family"] in COMPILED_FAMILIES:
        return (
            f"{len(contract.get('nodes') or [])} nodes / "
            f"{len(contract.get('edges') or [])} edges / "
            f"{len(contract.get('groups') or [])} groups / "
            f"{len(contract.get('assertions') or [])} assertions"
        )
    return (
        f"{len(contract.get('panels') or [])} panels / "
        f"{len(contract.get('series') or [])} series / "
        f"{len(contract.get('assertions') or [])} assertions"
    )


def edge_role_colors(contract: dict[str, Any]) -> dict[str, str]:
    """边角色 → 颜色。框架发色，按首次出现顺序稳定。"""

    order: list[str] = []
    for edge in contract.get("edges") or []:
        role = edge.get("role")
        if role and role not in order:
            order.append(role)
    return {
        role: EDGE_ROLE_PALETTE[index % len(EDGE_ROLE_PALETTE)]
        for index, role in enumerate(order)
    }


def role_colors(contract: dict[str, Any]) -> dict[str, str]:
    """角色 → 颜色。框架发色（稳定按角色首次出现顺序），不让每张图自己编。"""

    order: list[str] = []
    for node in contract.get("nodes") or []:
        if node["role"] not in order:
            order.append(node["role"])
    return {
        role: ROLE_PALETTE_ORDER[index % len(ROLE_PALETTE_ORDER)]
        for index, role in enumerate(order)
    }


def slug_for(contract: dict[str, Any], fallback: str) -> str:
    return slug(contract.get("title") or fallback, fallback=fallback)


# ── 调用方政策：语言与禁用词（2026-09-18 iter11）─────────────────────────
#
# 写作节点在 constraints 里发了 text_language=en 与 forbid_text，这边过去一个
# 字没读：七张示意图画成中文，渲染后写作侧 H8 判红、再派一轮。政策在场而判据
# 不在场。合同里的每一段人读文字在**声明那一刻**就能核 —— 不必等渲染。

_CJK_RE = re.compile(r"[一-鿿぀-ヿ가-힯]")


def contract_strings(contract: dict[str, Any]) -> list[str]:
    """合同里会出现在纸上的每一段文字（两个家族）。"""

    out: list[str] = [str(contract.get("title") or "")]
    out += [str(note) for note in contract.get("notes") or []]
    for node in contract.get("nodes") or []:
        out += [str(node.get("label") or ""), str(node.get("sublabel") or ""),
                str(node.get("role") or "")]
    for group in contract.get("groups") or []:
        out.append(str(group.get("label") or ""))
    for edge in contract.get("edges") or []:
        out += [str(edge.get("label") or ""), str(edge.get("role") or "")]
    for annot in contract.get("annotations") or []:
        out.append(str(annot.get("text") or ""))
    for block in contract.get("blocks") or []:
        out.append(str(block.get("title") or ""))
        out += [str(item) for item in block.get("items") or []]
        if block.get("text"):
            out.append(str(block["text"]))
    for panel in contract.get("panels") or []:
        out += [str(panel.get("label") or ""), str(panel.get("message") or "")]
        for axis in (panel.get("axes") or {}).values():
            out += [str(axis.get("label") or ""), str(axis.get("unit") or "")]
    for item in contract.get("series") or []:
        out.append(str(item.get("label") or ""))
    return [text for text in out if text.strip()]


def text_policy_failures(strings: list[str], policy: dict[str, Any] | None) -> list[str]:
    """语言政策与禁用词：命中的每一段文字点名，一条不漏。"""

    policy = policy or {}
    failures: list[str] = []
    language = str(policy.get("text_language") or "").lower()
    if language == "en":
        hits = [text for text in strings if _CJK_RE.search(text)]
        if hits:
            failures.append(
                "the caller requires figure text in English (constraints.text_language="
                "'en') but these strings contain CJK characters: "
                + "; ".join(repr(text[:40]) for text in hits[:8])
                + ("…" if len(hits) > 8 else "")
                + ". Rewrite them in English (Topology 1, PCIe Switch, GPU ×8)"
            )
    for pattern in policy.get("forbid_text") or []:
        try:
            regex = re.compile(pattern)
        except re.error:
            continue
        hits = [text for text in strings if regex.search(text)]
        if hits:
            failures.append(
                f"the caller forbids figure text matching /{pattern}/ but these strings "
                "match: " + "; ".join(repr(text[:40]) for text in hits[:8])
                + ". Use the caller's naming (see constraints.rename if given)"
            )
    return failures


# ── 数据图的设计合同：渲染后从对象模型对账 ─────────────────────────────────


def _hex(colour: str) -> str:
    return str(colour or "").strip().lower()


def asserted_design_failures(
    contract: dict[str, Any],
    observed: dict[str, Any],
    *,
    medium: str,
    floor_pt: float,
    policy: dict[str, Any] | None = None,
    enforce_print: bool = True,
) -> list[str]:
    """数据图渲染后必须成立的设计事实。**每一条都是自相矛盾**，不是审美判决：

    合同说「两条序列」→ 纸上没有图例 / 图例条目不是那两条 / 图例出了画布；
    合同说「印在 170mm」→ 图上有字印出来不到 7pt；
    合同说「y 轴 Wall time (ms)」→ 纸上没有这行字；
    节点的调色板是合同的一部分 → 序列用了别的颜色。

    读的是 matplotlib 对象树（figure_audit 的 sidecar），不是模型对代码的描述。
    对象模型没采到（attached=False）时一条都不判 —— 缺席由调用方如实记。
    """

    if not observed.get("attached"):
        return []
    failures: list[str] = []
    series = contract.get("series") or []
    panels = contract.get("panels") or []

    # ── 图例：≥2 条序列就是义务 ────────────────────────────────────────
    if len(series) >= 2:
        declared = sorted(str(item.get("label") or "").strip() for item in series)
        legends = [leg for leg in observed.get("legends") or [] if leg.get("labels")]
        if not legends:
            failures.append(
                f"the contract declares {len(series)} series ({', '.join(declared)}) but "
                "the rendered figure has no legend: a reader cannot tell which bar or "
                "line is which. Add ax.legend() (or fig.legend()) with exactly these "
                "labels, inside the canvas"
            )
        else:
            shown = sorted(str(label).strip() for leg in legends for label in leg["labels"])
            if shown != declared:
                failures.append(
                    f"the legend on the page reads {shown} but the contract's series are "
                    f"{declared}: same count, same words, no duplicates — label each "
                    "series once (label=... only on its first artist) or build the "
                    "legend from explicit handles"
                )
            outside = [leg for leg in legends if leg.get("inside") is False]
            if outside:
                failures.append(
                    "the legend is drawn outside the canvas (bbox_to_anchor beyond the "
                    "figure, or tight_layout did not reserve room for it): it will be "
                    "cropped in print. Place it inside the axes, or reserve space with "
                    "fig.subplots_adjust / constrained_layout"
                )

    # ── 坐标轴：合同里的 `Label (unit)` 必须真的印在纸上 ───────────────────
    page_labels = {
        str(text).strip()
        for panel in observed.get("panels") or []
        for text in (panel.get("xlabel"), panel.get("ylabel"))
        if str(text or "").strip()
    }
    missing: dict[str, str] = {}   # 纸上没有的 `Label (unit)` → 该调哪个 setter
    for panel in panels:
        for axis_name, axis in (panel.get("axes") or {}).items():
            wanted = axis_label_text(axis)
            if not wanted or wanted in missing:
                continue
            alternatives = {wanted}
            unit = str(axis.get("unit") or "").strip()
            if unit and unit.lower() != NO_UNIT:
                alternatives.add(f"{axis.get('label')} [{unit}]")
            if not (alternatives & page_labels):
                missing[wanted] = f"ax.set_{axis_name}label({wanted!r})"
    if missing:
        # 同一根轴四个面板不必报四遍，但每根缺的轴都要点名
        failures.append(
            "the contract declares axis labels that are not on the page: "
            + ", ".join(repr(label) for label in missing)
            + f" (page has: {sorted(page_labels) or 'no axis labels'}). Call "
            + " / ".join(missing.values())
            + " on at least one panel"
        )

    # ── 字号：按 figsize 换算到版心，最小的字不得低于下限 ───────────────────
    size_pt = observed.get("size_pt") or []
    min_font = observed.get("min_font_pt")
    if size_pt and min_font:
        spec = MEDIA[medium]
        w_mm = float(size_pt[0]) * 25.4 / 72.0
        h_mm = float(size_pt[1]) * 25.4 / 72.0
        # 不封顶：图会被撑到版心宽（写作侧 width=\textwidth），窄图的字随之变大。
        scale = fit_scale(w_mm, h_mm, medium)
        final = float(min_font) * scale
        if enforce_print and final < floor_pt - 1e-6:
            failures.append(
                f"printed at {medium} ({spec['w']:.0f}×{spec['h']:.0f} mm) this "
                f"{w_mm:.0f}×{h_mm:.0f} mm figure is scaled to {scale:.2f}, so its "
                f"smallest text ({float(min_font):.1f}pt: {observed.get('min_font_sample')!r}) "
                f"prints at {final:.1f}pt — below the {floor_pt:.0f}pt floor. Either keep "
                f"figsize within {spec['w'] / 25.4:.2f}×{spec['h'] / 25.4:.2f} in so the "
                "figure prints 1:1, or raise every fontsize (ticks included) to at least "
                f"{floor_pt / max(scale, 1e-6):.1f}pt at this figsize"
            )

    # ── 配色：序列颜色必须来自节点的调色板 ───────────────────────────────
    allowed = {_hex(c) for c in (*DATA_PALETTE, *DATA_NEUTRALS)}
    colours = sorted({_hex(c) for c in observed.get("series_colors") or [] if c})
    foreign = [c for c in colours if c not in allowed]
    if foreign:
        failures.append(
            f"series colours {foreign} are not in the node's palette. The framework "
            "sets axes.prop_cycle to the academic palette before your code runs — "
            "drop explicit color= arguments, or pick from "
            f"{list(DATA_PALETTE)} (neutrals {list(DATA_NEUTRALS)} for reference lines)"
        )

    # ── 语言与禁用词也要看**渲染出来**的字（刻度/图例来自数据，声明时看不到）──
    failures += text_policy_failures(list(observed.get("texts") or []), policy)
    return failures


def render_guidance(contract: dict[str, Any], *, medium: str, floor_pt: float) -> dict[str, Any]:
    """声明时就把渲染必须遵守的数交给作者 —— 别让它渲染完才知道。"""

    spec = MEDIA[medium]
    return {
        "medium": medium,
        "print_box_mm": [spec["w"], spec["h"]],
        "figsize_max_inches": [round(spec["w"] / 25.4, 2), round(spec["h"] / 25.4, 2)],
        "font_floor_pt": floor_pt,
        "max_panels": MAX_PANELS,
        "palette": list(DATA_PALETTE),
        "neutrals": list(DATA_NEUTRALS),
        "legend_required": len(contract.get("series") or []) >= 2,
        "axis_labels_expected": sorted(
            {
                axis_label_text(axis)
                for panel in contract.get("panels") or []
                for axis in (panel.get("axes") or {}).values()
                if axis_label_text(axis)
            }
        ),
        "rc_applied_before_your_code": {
            "font.size": floor_pt + 1,
            "xtick.labelsize / ytick.labelsize / legend.fontsize": floor_pt,
            "axes.prop_cycle": "cycler(color=palette)",
            "savefig.dpi": 300,
        },
        "rules": [
            f"keep figsize within {spec['w'] / 25.4:.2f}×{spec['h'] / 25.4:.2f} in "
            "(then every fontsize prints 1:1); a wider figure is scaled down and its "
            f"smallest text must still be ≥ {floor_pt:.0f}pt",
            "≥2 series ⇒ one legend whose entries are exactly series[].label, inside the canvas",
            "each declared axis label `Label (unit)` must appear on at least one panel",
            "series colours from the palette (omit color= to get it automatically)",
        ],
    }


def style_preamble(*, medium: str, floor_pt: float) -> str:
    """渲染代码之前注入的 rcParams：让「什么都不指定」的代码默认就合乎合同。

    只设默认，不锁死 —— 模型显式写的字号/颜色照样生效，随后由对象模型对账。
    自足（不 import 本仓库），任何一步失败都吞掉：样式没设上不该让渲染崩。
    """

    palette = list(DATA_PALETTE)
    return (
        "try:\n"
        "    import matplotlib as _afs_mpl\n"
        "    from cycler import cycler as _afs_cycler\n"
        "    _afs_mpl.rcParams.update({\n"
        f"        'font.size': {floor_pt + 1},\n"
        f"        'axes.labelsize': {floor_pt + 1},\n"
        f"        'axes.titlesize': {floor_pt + 2},\n"
        f"        'xtick.labelsize': {floor_pt},\n"
        f"        'ytick.labelsize': {floor_pt},\n"
        f"        'legend.fontsize': {floor_pt},\n"
        f"        'figure.titlesize': {floor_pt + 3},\n"
        f"        'axes.prop_cycle': _afs_cycler(color={palette!r}),\n"
        "        'savefig.dpi': 300,\n"
        "        'axes.unicode_minus': False,\n"
        "        'axes.spines.top': False,\n"
        "        'axes.spines.right': False,\n"
        "    })\n"
        "except Exception:\n"
        "    pass\n"
    )
