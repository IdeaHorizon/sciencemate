"""合同 → 图：compiled 家族的确定性编译器（布局算出来，不是手写坐标）。

## 为什么编译而不是「让模型写坐标」

v1 实测那张拓扑图，`README.md` 里 agent 自述的「迭代中踩过的坑」四条**全是
坐标算术**：

    服务器高度不足会导致四层重叠 → 加高到 6.2
    中央交换机过大（2.6）会遮挡下方服务器 → 缩到 2.2、gap_y 增到 3.4
    GPU 组总宽超过服务器内部宽会出框 → gpu_w 缩到 0.48
    positions 中心坐标与 xlim 边界公式必须一致，否则顶部服务器被裁掉

20 分钟、8 次渲染、147 万 token 的预算，全烧在「别翻车」上，没有一分钟花在
「连线对不对」。而与此同时，`ax.plot([sx+sw_w/2, gc], ...)` 把一整组 4 张
GPU 画成**一条**线 —— 因为那条线是手写的，写一条和写四条在代码里没有区别。

结论：**几何应当被算出来，不是被猜出来。** 框架按合同算盒子尺寸、排版、
布线；模型只声明结构。于是：

- 重叠/出框/裁切在架构上不可能（尺寸由内容算出，容器由内容撑开）；
- 「忘了画一条边」在架构上不可能（边是遍历 `edges` 画的）；
- 配色一致（框架按 role 发色，不是每张图自己编一套）。

## 输出的是「一段自足的 Python」，不是直接画

渲染仍然走 `render_figure` 那条唯一的路：沙箱执行 → 逐字节冻结 → replay
命令。所以编译器产出的是**框架生成的 Python 源码**（几何以字面量内嵌），
它被冻结、被 hash、可被 referee 原样重跑。出处绑定链一点没动。

## 两个后端，一份合同

- `matplotlib`（默认）：今天在沙箱里就能出 PNG/PDF/SVG，零新依赖。
- `tikz`：同一份合同编译成 TikZ（XeTeX 排版），出版级排版与原生矢量。编译走
  ``latex.run_tex``，与稿件同一个编译器（latexmk，缺则随包 tectonic）。沙箱里
  没有**任何 PDF 光栅化器**（pdftoppm /
  pdftocairo / gs / dvisvgm / pypdfium2 全无，2026-09-16 实测）——所以
  tikz 后端在没有光栅化器时只交 PDF，PNG 缺席是**可读事实**而不是异常。

两个后端从同一份合同来，所以不是两个真相源：它们渲染的是同一张声明的图。

## Owner 说明

`nodes/postprocess/` 的 owner 是 cuikl；本模块由架构组按 wangd 2026-09-16
的点名代办实现，不是 owner 本人的声明。
"""

from __future__ import annotations

import math
import itertools
import time

import json
from typing import Any

from .figure_contract import _group_member_ids, edge_role_colors, role_colors

# ── 版面常量（点；72pt = 1 inch）──────────────────────────────────────────
# 全部尺寸由内容算出，这些只是间距与下限 —— 改它们不会让任何东西重叠。
#: ── 版面尺度：一个模数音阶，不是十个拍脑袋的数 ────────────────────────────
#:
#: 改之前 11 个文字角色用了 8 个字号：`9.5` 同时是注记/图例/页脚，`11.0` 同时是
#: 规格条目和节点标签，而 `13.0`（组标题）与 `13.5`（栏标题）**眼睛根本分不出**。
#: 那不是层级，是噪声 —— 也正是「业余」和「讲究」的分界之一。
#:
#: 排版上的做法是**模数音阶**：一个基数 × 一个比例，每一档都与相邻档明显不同。
#: 取 base 8.0 / ratio 1.18（六档）：跨度 2.28，与原来的 2.12 接近 ——
#: **刻意不把图撑大**：整体放大不会改善终尺寸可读性（全图等比缩放后一样），
#: 音阶管的是层级可分辨性，密度才是可读性的事。这两件不能混。
TYPE_BASE = 8.0
TYPE_RATIO = 1.18


def type_size(tier: int) -> float:
    """音阶第 tier 档。0=最细的辅助文字，5=图标题。"""

    return round(TYPE_BASE * TYPE_RATIO ** tier, 1)


#: 档位分配。**同一档的东西在版面上就是同一层**，这是分配的全部依据：
#:   0 fine      盒内副标题 / chip / 边标注 / 引出注记 —— 都活在密集区里
#:   1 body      图例 / 页脚 / 规格条目 —— 正文级
#:   2 object    节点标签 —— 图里的主角
#:   3 container 组标题
#:   4 panel     分栏标题
#:   5 figure    图标题
SUB_FS = type_size(0)
CHIP_FS = type_size(0)
EDGE_LABEL_FS = type_size(0)
ANNOT_FS = type_size(0)
LEGEND_FS = type_size(1)
NOTE_FS = type_size(1)
SPEC_FS = type_size(1)
NODE_FS = type_size(2)
GROUP_LABEL_FS = type_size(3)
PANEL_TITLE_FS = type_size(4)
TITLE_FS = type_size(5)


NODE_PAD_X = 11.0
NODE_PAD_Y = 9.0
NODE_MIN_W = 56.0
NODE_MIN_H = 34.0
NODE_GAP = 14.0

#: chip（密排小色块）与 bar（通栏长条）的尺寸档位。
CHIP_MIN_W = 30.0
CHIP_PAD_X = 5.0
CHIP_H = 22.0
BAR_H = 46.0

#: 图例排版常量。**必须与渲染端用同一份** —— 布局按一套算宽、渲染按另一套画，
#: 就会出现「算的时候不出界、画出来出界」。
LEGEND_ENTRY_GAP = 26.0
LEGEND_TEXT_RATIO = 0.62
LEGEND_LINE_W = 22.0

#: 版面分栏（panel）的排版常量。
PANEL_TITLE_H = 24.0
# 栏标题左边的编号徽章 + 两侧内边距：块的自然宽度不得窄于「标题 + 这一段」。
PANEL_TITLE_LEAD = 34.0
#: 分栏编号徽章半径。编号是画出来的圆圈+数字，不是排出来的 ① —— 衬线正文
#: 字体没有 ① 的字形，整个字符会被静默丢掉。
BADGE_R = 9.0
#: 走外侧的边（回退边）离内容多远，以及多条时彼此的间距。
#: 侧边一列离内容多远，以及它的公共轨道离内容多远。
#: 时序：表头与第一条消息的距离、消息之间的行距、最后一条到生命线末端、
#: 自环的宽高。
SEQ_HEAD_GAP = 26.0
SEQ_STEP_GAP = 34.0
SEQ_TAIL_GAP = 22.0
SEQ_SELF_W = 34.0
SEQ_SELF_H = 16.0
SIDE_GAP = 34.0
SIDE_RAIL_GAP = 16.0
#: 自转移画成节点正上方的小回环：环高，以及它与上一层之间的余量。
#: 有自环的节点，顶边留给别的边的比例（最右那一段归自环）。
SELF_LOOP_TOP_SHARE = 0.72
SELF_LOOP_H = 17.0
SELF_LOOP_W = 16.0
SELF_LOOP_GAP = 7.0
#: 重排建议最多贪心几轮 —— 每轮都要把所有相邻对调真摆一遍，不能无限试。
REORDER_MAX_ROUNDS = 3
# 一行短到这个长度就直接穷举全排列 —— 贪心单步对调出不了局部最优（见
# _cheaper_order）。5 个元素 120 种，单块布局毫秒级，代价可以接受。
REORDER_PERMUTE_MAX = 5
#: 交叉少于这个数就不值得为它重摆几十遍版 —— 判据的成本也是成本。
REORDER_MIN_CROSSINGS = 2
#: 找这条建议最多允许花多久。不封顶时 22 节点的图要花 34 秒 ——
#: **判据的成本也是成本**，它花的每一秒都记在作者的账上。
#:
#: 这条建议因此是**尽力而为**：6 秒能找到第一步（17→15），跑满 34 秒能找到三步
#: （17→4）。没有选后者 —— 干净的图根本不触发这段（交叉 <2 直接跳过），这笔钱
#: 只由已经乱了的图付，但也不该让它付 34 秒。
REORDER_TIME_BUDGET_S = 6.0
AROUND_MARGIN = 14.0
AROUND_LANE_GAP = 10.0
#: 同型标注对齐：对齐轴上的离散度相对于铺开方向的上限（无量纲），
#: 以及单个标注允许被挪动的最大距离（pt）。两条都是为了不把标注拽离它的线。
PEER_ALIGN_SPAN = 0.35
PEER_ALIGN_MAX_SHIFT = 40.0
#: 版面自选并排时的目标画幅（宽/高）。参考图是 1.32；4:3 是论文与幻灯片
#: 都放得下的形状。只在作者一个 blocks[].row 都没给时生效。
PAGE_TARGET_ASPECT = 1.33
#: 一行最多并排几个分栏。原来是 2 —— 六面板只能 2×3 竖排（478×798pt），
#: 在 170mm 版心里被高度压到 0.72、副标题 5.8pt。2026-09-18 iter11 实测同一
#: 份合同 3×2 是 551×589pt、0.875、副标题 7.0pt。并排方式按**印刷版心下的
#: 缩放**选，不再按 4:3 的画幅猜。
PAGE_MAX_PER_ROW = 3
#: 各行自然宽度之比的上限 —— 超过就说明并排把留白挪到了两侧。这一条就够了：
#: 先写过一条「页面不得比最宽的块宽 2.1 倍」，而两栏并排天然就是 2 倍多一点，
#: 于是它把**所有**并排方案都否了，整个自选排版成了死代码。
PAGE_ROW_WIDTH_RATIO = 1.25
#: 同一行里块高之比的上限 —— 超过就说明矮的那个白占一栏。
PAGE_ROW_HEIGHT_RATIO = 1.6
#: 页面级的壳（分栏内边距 / 栏间距 / 栏标题条 / 页边）。
#:
#: **这四个数是扫出来的，不是拍的**：2026-09-17 量到一张真实产出里「我们自己
#: 的壳占了整页 45%，内容只占 55%」，而 aspect 36 轮 36 次都是竖长条。按当年扫
#: dot 间距的同一套办法扫了六组（16/26/30/30 → 8/14/20/18）：**每一组的交叉、
#: 叠线、贴线都完全不变**，aspect 单调改善。取中间一档 12/18/24/22 ——
#: 校准集 1.32→1.41（参考图量级）、真实产出 0.79→0.85、微服务 1.03→1.09，
#: 而渲出来看分栏边界依然清楚，再紧就局促了（度量分辨不出这件事，只能看图）。
PANEL_PAD = 12.0
PANEL_GAP = 18.0
SPEC_LINE_H = 24.0
SPEC_COL_GAP = 44.0
SPEC_BULLET_W = 12.0

#: 标注层排版。
PORT_W = 9.0
PORT_H = 6.0
ANNOT_LEAD = 30.0


def _text_box(text: str, cx: float, y: float, size: float) -> list[float] | None:
    """一段居中文字真正占的地方。判据要的是「这条横带上有没有东西」。"""

    if not text:
        return None
    width = _text_width(text, size)
    return [round(cx - width / 2.0, 2), round(y, 2), round(width, 2), round(size * 1.3, 2)]


def _row_box(cx: float, y: float, width: float, height: float) -> list[float]:
    return [round(cx - width / 2.0, 2), round(y, 2), round(width, 2), round(height, 2)]


def legend_width(
    node_roles: list[str], edge_roles: list[str], font_size: float = LEGEND_FS
) -> float:
    """图例一行摆开有多宽。画布宽必须由内容**和图例**共同撑开。

    2026-09-16 实测：加了边图例之后，图例总宽从来没有算进画布宽 —— 窄图 +
    长 role 名就出界，agent 只能靠「把 role 名改短」去绕（它最后写的是英文
    `PCIe link` 而不是中文）。几何该算对的地方没算对，就会让模型花轮次去猜补。
    """

    widths = [
        LEGEND_SWATCH + 7 + len(role) * font_size * LEGEND_TEXT_RATIO
        for role in node_roles
    ]
    widths += [
        LEGEND_LINE_W + 6 + len(role) * font_size * LEGEND_TEXT_RATIO
        for role in edge_roles
    ]
    if not widths:
        return 0.0
    return sum(widths) + LEGEND_ENTRY_GAP * (len(widths) - 1)

RANK_GAP = 50.0
GROUP_PAD = 18.0
GROUP_LABEL_H = 24.0

#: 顶层带子之间的走廊。**这个数是扫出来的**（2026-09-17，与页面壳同一套办法）：
#: 74 / 62 / 54 / 46 / 38 五档 × 八条基准 + 一份真跑合同。
#:   74 → 62：**每一条都改善或持平，一条都没退化**
#:              state-machine 画幅 0.40 → 0.99（带子变矮之后两栏被打包成并排，
#:              iter44 救活的那个画幅优化器第一次真的改变了排布）
#:              flowchart 0.95→1.02、layered 0.92→0.97、密集图交叉 281→278
#:   62 → 54：密集图当场炸（交叉 281 → 317，+13%）—— 走廊不够，边开始互相挤
#: 所以 62 是拐点，不是口味。再往下换来的画幅是用密集图的可读性买的。
BAND_GAP = 62.0
# 空隙的下限与每条车道的增量 —— 见 _band_gaps：一道空隙多高，由真正穿过它的
# 线数决定，不按最坏情况写死。
BAND_GAP_FLOOR = 26.0
BAND_LANE = 6.0
ENTRY_GAP = 38.0
MARGIN = 22.0

#: block 在分栏里的内边距。页边距是给**页**的，不是给每个 block 的 ——
#: 每个 block 再吃一次 30pt 页边距，栏内上下就各多出一圈空白（实测 ① 栏
#: 底部大片留白）。
INNER_MARGIN = 8.0
TITLE_GAP = 26.0
LEGEND_GAP = 26.0
LEGEND_SWATCH = 13.0
NOTE_GAP = 16.0

#: 画幅长宽比上限。超过就是「摆法」的问题，不是尺寸问题。
MAX_ASPECT = 3.2

#: 平均每条边允许的交叉数上限。超过就是「追不了哪条线通向哪里」。
MAX_CROSSINGS_PER_EDGE = 0.25

#: 「图太空」的判据。
#:
#: **换过一次，因为第一版量错了东西**：原先用 ink_ratio（节点面积 / 画布面积），
#: 它按元件面积算，于是**惩罚 chip**（密排小色块）—— 而 chip 是好设计，参考图
#: 就用它。实测把两个块并排后 aspect 从 0.75 改善到 1.61（更接近参考图 1.19），
#: ink 却掉了 34.7%：两个指标反向，说明至少有一个不是它声称的那件事。
#:
#: 「空」该指**内容之外的浪费**（整片没用到的区域），不是元件之间的间隔 ——
#: 间隔是必要的。「一整片没用到的区域」由**空行/空列**回答（扫图真正占用的
#: 那块区域）。曾经还有一个 content_fill（内容包围盒 / 可画区域）兼做「盲区
#: 探测器」，2026-09-17 变异把它判了死刑：拿掉 panels / spec-note / annotations /
#: groups，它的数值**一动不动** —— 包围盒比值只有最外围那圈丢了才动，
#: 用它防盲区等于用门口的脚垫防小偷。盲区改由
#: `test_the_criteria_can_see_everything_the_renderer_draws` 直接问。
#: 校准集实测（2026-09-17 三次重标）：空带只扫图真正占用的那块区域，
#: content_fill 的分母改成**可画区域**（去掉页边距）。
#:
#: 去掉边距之后所有好图的 content_fill 都落在 1.00-1.02 —— 这不是度量失效，
#: 而是它的真实含义变了：画布本来就是按内容算出来的，所以「内容包围盒 / 可画
#: 区域」结构上就该接近 1。**它掉下来只可能是有画出来的东西没被量到**（度量的
#: 盲区），或者某个块占了远超它需要的地方。判据按这个含义重标。
#: 「一整片区域没东西」这个问题由空行/空列回答（参考图 3、example 2、时序 5）。
#: 旧阈值 0.70 / 4 是照着**算错的**度量定的 —— 那时大标题、图例、规格条目、
#: 段落注记都不算内容（文字只记一个 1×1 的锚点），参考图只算出 0.79/12/2。
#: 度量换了尺，阈值必须跟着重标，否则判据整体变松、等于没有。
MAX_EMPTY_COLS = 1
MAX_EMPTY_ROWS = 5

#: 出图后端。词表与实现同源；报错必须列出合法值。
BACKENDS: tuple[str, ...] = ("matplotlib", "tikz")

#: **示意图的默认后端是 TikZ。**
#:
#: 一度不是。理由当时是「沙箱里没有任何 PDF 光栅化器，TikZ 出不了 PNG，而
#: PNG 是机械审计/审图/预览的入口」—— 但那是个**环境缺口**，不是设计理由，
#: 正解是把光栅化器补上（pypdfium2，纯 wheel，已进 base deps），而不是把更好
#: 的排版藏在一个没人会传的参数后面。一个出版级制图节点默认交出非出版级的
#: 排版，是用「省一个依赖」换掉了它存在的意义。
DEFAULT_SCHEMATIC_BACKEND = "tikz"

#: tikz 后端把 PDF 光栅化成 PNG 所需的包。纯 wheel、无系统依赖；沙箱装不上时
#: 记录如实说「没有光栅化器，PNG 缺席」，不静默降级。
TIKZ_RASTER_REQUIREMENTS: tuple[str, ...] = ("pypdfium2>=4.0",)


#: 一个角色的颜色派生出三样东西：浅底、同色描边、深色字。
#:
#: 原来是**重色实底 + 白字**（`fill={role}, draw=black!85, text=white`）。那组
#: 色板（seaborn deep）是给折线和柱子用的 —— 小面积没问题，拿来填满整个盒子
#: 再压白字就发闷。用户看图的原话是「配色很低级」，而两张参考图全都是
#: **浅底 + 同色系描边 + 深色字**（2026-09-17）。
#:
#: 这不是给模型的旋钮，是把我们自己的默认改对：角色→颜色这件事从头到尾归框架，
#: 模型无从选择，所以它长什么样只能由我们负责。
#: 底色 / 描边 / 文字各自的**目标亮度**（sRGB 相对亮度，0=黑 1=白）。
#:
#: 一开始写的是「混 13% 的白」这种固定比例，结果深色相（蓝 #4C72B0）刚好，
#: 浅色相（土黄 #CCB974）本来就亮，再混就白掉了 —— 通栏那条交换机在图上
#: 几乎看不见。固定比例做不到「一组颜色看上去是一套」。
#:
#: 改成按亮度定标：**所有底色同一亮度、所有描边同一亮度、所有文字同一亮度，
#: 色相只负责身份。** 这正是两张参考图在做的事（浅底 + 同色描边 + 深色字，
#: 每个角色的视觉分量相等），也是「配色低级」和「配色讲究」的真实分界。
#: 底色是**定标**（一律提到同一亮度），描边和文字是**封顶**（只压不提）：
#: 色板里深色相本来就够暗，把它们"调亮到目标"会让描边比原来还虚。封顶只对
#: 过亮的色相（土黄 0.49）生效，把它压到和别人一样重。
FILL_LUMA = 0.855
STROKE_LUMA_CAP = 0.26
TEXT_LUMA_CAP = 0.10


def _hex_rgb(colour: str) -> tuple[float, float, float]:
    raw = str(colour or "#000000").lstrip("#")
    return tuple(int(raw[i : i + 2], 16) / 255.0 for i in (0, 2, 4))


def _rgb_hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c * 255))):02X}" for c in rgb)


def _luma(rgb: tuple[float, float, float]) -> float:
    """sRGB 相对亮度（WCAG 定义）。"""

    def lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _at_luma(colour: str, target: float) -> str:
    """保持色相，把这个颜色调到指定亮度（比自己亮就混白，暗就混黑）。

    亮度对混合比例单调，所以二分即可 —— 确定性，同一个输入永远同一个输出。
    """

    rgb = _hex_rgb(colour)
    here = _luma(rgb)
    if abs(here - target) < 1e-4:
        return _rgb_hex(rgb)
    toward = (1.0, 1.0, 1.0) if target > here else (0.0, 0.0, 0.0)
    lo, hi = 0.0, 1.0
    for _ in range(24):
        mid = (lo + hi) / 2.0
        trial = tuple(c + (t - c) * mid for c, t in zip(rgb, toward))
        if (_luma(trial) < target) == (target > here):
            lo = mid
        else:
            hi = mid
    mix = (lo + hi) / 2.0
    return _rgb_hex(tuple(c + (t - c) * mix for c, t in zip(rgb, toward)))


def _tint(colour: str) -> str:
    return _at_luma(colour, FILL_LUMA)


def _shade(colour: str, ceiling: float) -> str:
    """压到不亮于 ceiling。已经够暗的色相原样保留。"""

    return (
        _rgb_hex(_hex_rgb(colour))
        if _luma(_hex_rgb(colour)) <= ceiling
        else _at_luma(colour, ceiling)
    )


def _text_width(text: str, font_size: float) -> float:
    """这段文字有多宽。

    **有实测就用实测** —— `text_metrics.measured(...)` 在布局外面装了一张由
    xelatex 量出来的表时，这里直接答真值。没装表才退回下面的估算。

    估算：CJK 约 1.0 em，拉丁约 0.55 em。只用来给盒子定下限 —— 估宽了盒子略大
    （无害），估窄了会挤。但它和真实排版差得比想象中多：`RTX PRO 6000` 估
    49.9pt、xelatex 排出来 73.8pt，于是盒子装不下自己的字，而所有几何判据都
    在校验这份估算，一条都没响（2026-09-17）。
    """
    from .text_metrics import lookup

    hit = lookup(text, font_size)
    if hit is not None:
        return hit
    width = 0.0
    for char in str(text or ""):
        code = ord(char)
        if code >= 0x2E80 or code in (0x3000,):
            width += 1.0
        elif char in "MW@%":
            width += 0.78
        elif char in "il.,:;'|! ":
            width += 0.32
        else:
            width += 0.56
    return width * font_size


class _Rect:
    __slots__ = ("x", "y", "w", "h")

    def __init__(self, x: float, y: float, w: float, h: float) -> None:
        self.x, self.y, self.w, self.h = x, y, w, h

    @property
    def cx(self) -> float:
        return self.x + self.w / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.h / 2.0

    @property
    def top(self) -> float:
        return self.y + self.h

    @property
    def right(self) -> float:
        return self.x + self.w

    def as_list(self) -> list[float]:
        return [round(self.x, 2), round(self.y, 2), round(self.w, 2), round(self.h, 2)]

    def shift(self, dx: float, dy: float) -> None:
        self.x += dx
        self.y += dy


def _node_size(node: dict[str, Any]) -> tuple[float, float]:
    """形状决定尺寸档位 —— 形状要表意，不是所有东西都一样大。"""

    shape = node.get("shape", "box")
    if shape == "chip":
        # 密排小色块：只放主标签、小字号、窄内边距。32 张 GPU 这样才排得下，
        # 而且视觉上一眼读成「一批同类元件」而不是 32 个独立组件。
        width = max(CHIP_MIN_W, _text_width(node["label"], CHIP_FS) + 2 * CHIP_PAD_X)
        return width, CHIP_H
    if shape == "bar":
        # 通栏长条：真实宽度在页面编排阶段按内容宽撑满，这里只给下限与高度。
        width = max(
            NODE_MIN_W * 3,
            _text_width(node["label"], NODE_FS) + _text_width(node["sublabel"], SUB_FS) + 80.0,
        )
        return width, BAR_H
    label_w = _text_width(node["label"], NODE_FS)
    sub_w = _text_width(node["sublabel"], SUB_FS) if node["sublabel"] else 0.0
    width = max(NODE_MIN_W, max(label_w, sub_w) + 2 * NODE_PAD_X)
    lines = 2 if node["sublabel"] else 1
    height = max(NODE_MIN_H, NODE_PAD_Y * 2 + NODE_FS * 1.25 + (SUB_FS * 1.5 if lines == 2 else 0))
    return width, height


def _layout_group(
    group: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    inline_labels: dict[tuple[str, str], str],
) -> dict[str, Any]:
    """一个组：ranks 逐行排，行内居中。组盒由内容撑开 —— 不会出框。

    `inline_labels` 是同一行里相邻两个节点之间那条带标注的边（例如两个 PCIe
    Switch 之间的「400 Gbps · 0.1 µs」）。间距按标注宽度**撑开** —— 不撑开就
    会出现 v1 那种「标注压在盒子上」，而那正是手写坐标必然踩的坑。
    """

    rank_boxes: list[list[tuple[str, float, float]]] = []
    for rank in group["ranks"]:
        row = [(nid, *_node_size(nodes[nid])) for nid in rank]
        rank_boxes.append(row)
    rank_gaps: list[list[float]] = []
    for rank in group["ranks"]:
        gaps: list[float] = []
        for left, right in zip(rank, rank[1:]):
            label = inline_labels.get((left, right)) or inline_labels.get((right, left)) or ""
            gaps.append(
                max(NODE_GAP, _text_width(label, EDGE_LABEL_FS) + 22.0) if label else NODE_GAP
            )
        rank_gaps.append(gaps)
    rank_widths = [
        sum(w for _, w, _ in row) + sum(gaps) for row, gaps in zip(rank_boxes, rank_gaps)
    ]
    rank_heights = [max(h for _, _, h in row) for row in rank_boxes]
    inner_w = max(rank_widths) if rank_widths else 0.0
    inner_h = sum(rank_heights) + RANK_GAP * (len(rank_boxes) - 1)
    # 没有标题就不预留标题带 —— 预留了又不写，就是 v1 那种「下半截空着」。
    label_h = GROUP_LABEL_H if group["label"] else 0.0
    width = inner_w + 2 * GROUP_PAD
    height = inner_h + 2 * GROUP_PAD + label_h
    return {
        "id": group["id"],
        "width": width,
        "height": height,
        "rank_boxes": rank_boxes,
        "rank_heights": rank_heights,
        "label": group["label"],
        "label_position": group["label_position"],
        "label_h": label_h,
        "rank_gaps": rank_gaps,
    }


def _place_group(plan: dict[str, Any], x: float, y: float) -> tuple[_Rect, dict[str, _Rect]]:
    """把一个已算好尺寸的组落在 (x, y)（左下角），返回组盒与组内节点盒。"""

    rect = _Rect(x, y, plan["width"], plan["height"])
    label_top = plan["label_position"] == "top"
    content_top = rect.top - GROUP_PAD - (plan["label_h"] if label_top else 0.0)
    placed: dict[str, _Rect] = {}
    cursor_y = content_top
    for row, row_h, gaps in zip(plan["rank_boxes"], plan["rank_heights"], plan["rank_gaps"]):
        row_w = sum(w for _, w, _ in row) + sum(gaps)
        cursor_x = rect.cx - row_w / 2.0
        for index, (nid, w, h) in enumerate(row):
            placed[nid] = _Rect(cursor_x, cursor_y - row_h + (row_h - h) / 2.0, w, h)
            cursor_x += w + (gaps[index] if index < len(gaps) else 0.0)
        cursor_y -= row_h + RANK_GAP
    return rect, placed


def _anchor(rect: _Rect, side: str, t: float = 0.5) -> tuple[float, float]:
    if side == "top":
        return rect.x + rect.w * t, rect.top
    if side == "bottom":
        return rect.x + rect.w * t, rect.y
    if side == "left":
        return rect.x, rect.y + rect.h * t
    return rect.right, rect.y + rect.h * t


def _blocked(x: float, y0: float, y1: float, boxes: list[_Rect], skip: set[int]) -> bool:
    low, high = (y0, y1) if y0 <= y1 else (y1, y0)
    for box in boxes:
        if id(box) in skip:
            continue
        if box.x - 4 <= x <= box.right + 4 and box.y - 4 <= high and box.top + 4 >= low:
            return True
    return False


def _clear_x(
    node: _Rect,
    y0: float,
    y1: float,
    boxes: list[_Rect],
    bounds: _Rect,
    *,
    prefer: float | None = None,
    avoid: tuple[float, ...] = (),
) -> float:
    """从 node 竖直出线时，找一条不穿过别的盒子、也不与已用走廊重合的 x。

    `prefer` 让走廊偏向目标那一侧（否则两条本该分开的线会并排走同一条缝）；
    `avoid` 是本图里已经占用的走廊。找不到就退回中心并由调用方记一条布局
    finding —— 宁可如实记账，也不假装干净（那正是 v1 的病）。
    """
    skip = {id(node)}
    # 步长要比「盒子之间的缝」还细，否则扫不到那些缝、只能绕到整排之外去
    # （实测：步长取节点宽度的 1/4 时，一条本该走 GPU4/GPU5 之间的线绕到了
    # 整行最右边）。缝宽下限 = NODE_GAP，所以步长取它的三分之一。
    step = NODE_GAP / 3.0
    span = max(bounds.w, node.w) + 2 * step
    candidates = [node.cx]
    for k in range(1, int(span / step) + 1):
        candidates += [node.cx - step * k, node.cx + step * k]
    usable = [
        x
        for x in candidates
        if bounds.x + 4 <= x <= bounds.right - 4
        and not _blocked(x, y0, y1, boxes, skip)
        and all(abs(x - used) > 12.0 for used in avoid)
    ]
    if not usable:
        return node.cx
    if prefer is None:
        return usable[0]
    return min(usable, key=lambda x: (abs(x - prefer), abs(x - node.cx)))


def _fallback_footnotes(
    contract: dict[str, Any],
    blocks: list[dict[str, Any]],
    nodes: dict[str, dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """版面层的内容降成页脚：回落引擎不会摆它们，但一个字都不许丢。

    多栏、规格块、注记块、引出注解都是版面层（`_layout_page`）的东西，回落引擎
    只会画一张平铺的图。降级可以，内容消失不行 —— 能降成页脚一行字的就降成一行
    字。返回 (页脚各行, 被降级的种类清单)；清单进 layout_findings，降级是可读事实。
    """

    notes = list(contract.get("notes") or [])
    degraded: list[str] = []
    diagrams = [block for block in blocks if block["kind"] == "diagram"]
    if len(diagrams) > 1:
        degraded.append(
            f"the {len(diagrams)}-panel page (panels were flattened into one diagram)"
        )
    spec_blocks = [block for block in blocks if block["kind"] == "spec"]
    for block in spec_blocks:
        head = f"{block['title']}: " if block.get("title") else ""
        notes.append(head + " · ".join(str(item) for item in block["items"]))
    if spec_blocks:
        degraded.append(f"{len(spec_blocks)} spec block(s)")
    note_blocks = [block for block in blocks if block["kind"] == "note"]
    for block in note_blocks:
        head = f"{block['title']}: " if block.get("title") else ""
        notes.append(head + str(block["text"]))
    if note_blocks:
        degraded.append(f"{len(note_blocks)} note block(s)")
    annotations = list(contract.get("annotations") or [])
    for item in annotations:
        anchor = (nodes.get(item["anchor"]) or {}).get("label") or item["anchor"]
        notes.append(f"{anchor}: {item['text']}")
    if annotations:
        degraded.append(f"{len(annotations)} annotation(s)")
    return notes, degraded


def _layout_builtin(contract: dict[str, Any]) -> dict[str, Any]:
    """自研 band/rank 布局 —— 只在没有 Graphviz 时用的回落实现。

    降级路径，但必须是**全函数**：合同里每个节点都有盒子、每个组都有框、几何的
    键集合与主路径一致。2026-09-18 在没有 dot 的 CI 机器上抓到三处「主路径会画、
    回落直接 KeyError」：侧边节点从没被摆过、嵌套子组的成员没人摆、边图例整个
    缺席（画布宽也没算它）。回落画得差可以，画不出来不行；画不出的东西要在
    layout_findings 里说出来，不许静默消失。
    """

    nodes = {node["id"]: node for node in contract["nodes"]}
    groups = {group["id"]: group for group in contract["groups"]}
    blocks = list(contract.get("blocks") or [])

    # 侧边一列在回落引擎里没有摆放逻辑 —— 当成最后一条带子，别让它崩。
    # 这一段必须在 band_specs 之前改 contract：2026-09-18 之前它写在 band_specs
    # 算完之后，等于从没执行过 —— 侧边节点没有盒子，布线一查就 KeyError。
    aside = [node["id"] for node in contract["nodes"] if node.get("side")]
    if aside:
        contract = {
            **contract,
            "bands": [*contract["bands"], [f"node:{nid}" for nid in aside]],
        }

    # 嵌套子组（groups[].parent）：回落引擎不会把子组当独立单元摆。把子树的
    # ranks 按深度优先接到顶层祖先的 ranks 后面，整棵树当一个组摆；子组的框事后
    # 按成员盒子的包络补出来（组框要画，`group:<子组>` 端点的边也要它）。
    children: dict[str | None, list[str]] = {}
    for group in contract["groups"]:
        children.setdefault(group.get("parent"), []).append(group["id"])

    def _subtree(gid: str) -> list[str]:
        out = [gid]
        for child in children.get(gid, []):
            out.extend(_subtree(child))
        return out

    unit_ranks: dict[str, list[list[str]]] = {
        gid: [rank for member in _subtree(gid) for rank in groups[member]["ranks"]]
        for gid in children.get(None, [])
    }

    # 布线只认「单元」：同一棵组树里的节点算同组，行号按接平之后的顺序数。
    group_of: dict[str, str] = {}
    rank_of: dict[str, int] = {}
    for gid, ranks in unit_ranks.items():
        for r_index, rank in enumerate(ranks):
            for nid in rank:
                group_of[nid] = gid
                rank_of[nid] = r_index

    # 同一组同一行、相邻两个节点之间那条带标注的边 —— 组内间距要为它让位。
    inline_labels: dict[tuple[str, str], str] = {}
    for edge in contract["edges"]:
        if not edge["label"]:
            continue
        src, dst = edge["from"], edge["to"]
        if (
            src in group_of
            and group_of[src] == group_of.get(dst)
            and rank_of[src] == rank_of[dst]
        ):
            inline_labels[(src, dst)] = edge["label"]

    group_plans = {
        gid: _layout_group({**groups[gid], "ranks": ranks}, nodes, inline_labels)
        for gid, ranks in unit_ranks.items()
    }

    # ── 每条带子（band）：条目横排、居中；带子自上而下堆叠 ──────────────
    band_specs: list[list[tuple[str, str, float, float]]] = []
    for band in contract["bands"]:
        entries: list[tuple[str, str, float, float]] = []
        for entry in band:
            prefix, _, ident = entry.partition(":")
            if prefix == "group":
                plan = group_plans[ident]
                entries.append(("group", ident, plan["width"], plan["height"]))
            else:
                w, h = _node_size(nodes[ident])
                if ident in _self_loop_nodes(contract):
                    w += _self_loop_width(contract, ident)
                entries.append(("node", ident, w, h))
        band_specs.append(entries)

    band_widths = [
        sum(w for _, _, w, _ in entries) + ENTRY_GAP * (len(entries) - 1)
        for entries in band_specs
    ]
    band_heights = [
        max(h for _, _, _, h in entries)
        + _band_headroom(contract, {ident for _k, ident, _w, _h in entries})
        for entries in band_specs
    ]
    content_w = (max(band_widths) if band_widths else 0.0) + 2 * _around_gutter(contract)
    gaps = _band_gaps(contract)
    gaps = (gaps + [BAND_GAP] * len(band_specs))[: max(0, len(band_specs) - 1)]
    content_h = sum(band_heights) + sum(gaps)

    has_title = bool(contract.get("title"))
    roles = role_colors(contract)
    # 边图例：主路径 2026-09-16 就把「图例宽要算进画布宽」修了，回落引擎里那个
    # 缺陷原样活着 —— 它压根没有边图例，边按角色着色而图上没有任何地方解释颜色。
    edge_colours = edge_role_colors(contract)
    legend_entries = sorted(roles)
    legend_sources = list(edge_colours)
    has_legend = bool(legend_entries or legend_sources)
    legend_h = (LEGEND_GAP + LEGEND_SWATCH + LEGEND_FS) if has_legend else 0.0
    notes, degraded = _fallback_footnotes(contract, blocks, nodes)
    notes_h = (NOTE_GAP + len(notes) * NOTE_FS * 1.7) if notes else 0.0
    title_h = (TITLE_FS * 1.4 + TITLE_GAP) if has_title else 0.0

    margin = MARGIN  # 回落引擎只在整页模式下用，永远吃页边距
    canvas_w = (
        max(
            content_w,
            legend_width(legend_entries, legend_sources),
            _text_width(contract.get("title") or "", TITLE_FS),
            max((_text_width(note, NOTE_FS) for note in notes), default=0.0),
        )
        + 2 * margin
    )
    canvas_h = content_h + 2 * MARGIN + title_h + legend_h + notes_h

    node_rects: dict[str, _Rect] = {}
    group_rects: dict[str, _Rect] = {}
    band_rects: list[_Rect] = []

    cursor_y = canvas_h - MARGIN - title_h
    for band_index, (entries, band_h) in enumerate(zip(band_specs, band_heights)):
        band_w = sum(w for _, _, w, _ in entries) + ENTRY_GAP * (len(entries) - 1)
        cursor_x = (canvas_w - band_w) / 2.0
        band_rects.append(_Rect((canvas_w - band_w) / 2.0, cursor_y - band_h, band_w, band_h))
        for kind, ident, w, h in entries:
            # 同一条带子里高度不同的条目：垂直居中，视觉上不会一高一低。
            y = cursor_y - band_h + (band_h - h) / 2.0
            if kind == "group":
                rect, placed = _place_group(group_plans[ident], cursor_x, y)
                group_rects[ident] = rect
                node_rects.update(placed)
            else:
                # 自环节点的盒子**连同预留一起变宽** —— 看图时我以为「运行中被
                # 拉成长条」是缺陷，动手改窄，实测交叉 2 → 14：它有 9 条边，
                # 56pt 的盒子根本排不开锚点。宽是对的，只是原因不是自环，而是
                # **度数**。审美判断又一次被度量否掉（2026-09-17）。
                node_rects[ident] = _Rect(cursor_x, y, w, h)
            cursor_x += w + ENTRY_GAP
        cursor_y -= band_h + (gaps[band_index] if band_index < len(gaps) else 0.0)

    # 子组的框：成员盒子的包络加内边距。它不参与摆放（成员已经随单元摆好了），
    # 只为画框、以及给 `group:<子组>` 端点的边一个矩形。
    for gid in groups:
        if gid in group_rects:
            continue
        member_rects = [
            node_rects[nid]
            for nid in _group_member_ids(contract, gid)
            if nid in node_rects
        ]
        if not member_rects:
            continue
        x0 = min(rect.x for rect in member_rects) - GROUP_PAD
        y0 = min(rect.y for rect in member_rects) - GROUP_PAD
        x1 = max(rect.right for rect in member_rects) + GROUP_PAD
        y1 = max(rect.top for rect in member_rects) + GROUP_PAD
        group_rects[gid] = _Rect(x0, y0, x1 - x0, y1 - y0)

    band_of_node: dict[str, int] = {}
    for index, band in enumerate(contract["bands"]):
        for entry in band:
            prefix, _, ident = entry.partition(":")
            if prefix == "node":
                band_of_node[ident] = index
            else:
                for rank in unit_ranks.get(ident, []):
                    for nid in rank:
                        band_of_node[nid] = index

    _port_at: dict[tuple[str, str], list[float]] = {}
    edges, layout_findings = _route_edges(
        contract,
        port_marks=_port_at,
        node_rects=node_rects,
        group_rects=group_rects,
        band_rects=band_rects,
        group_of=group_of,
        band_of_node=band_of_node,
        rank_of=rank_of,
    )
    _place_edge_labels(edges, list(node_rects.values()), (canvas_w, canvas_h))
    for item in edges:
        # 边的颜色和边图例必须是同一张表 —— 图例说「红=读写」而边全是黑的，
        # 图例就成了谎话。
        item["color"] = edge_colours.get(item.get("role", ""))
    if degraded:
        layout_findings.append(
            {
                "collector": "OB-LAYOUT",
                "message": (
                    "the built-in fallback engine does not lay out "
                    + ", ".join(degraded)
                    + "; that content was demoted to footnotes rather than dropped"
                ),
            }
        )

    return {
        "canvas": [round(canvas_w, 2), round(canvas_h, 2)],
        "title": contract.get("title") or "",
        "title_y": round(canvas_h - margin - TITLE_FS, 2) if has_title else None,
        "title_box": _text_box(
            contract.get("title") or "", canvas_w / 2.0,
            canvas_h - margin - TITLE_FS, TITLE_FS,
        ) if has_title else None,
        "legend_box": _row_box(
            canvas_w / 2.0, round(MARGIN + notes_h, 2),
            legend_width(legend_entries, legend_sources), LEGEND_SWATCH + LEGEND_FS,
        ) if has_legend else None,
        "nodes": {
            nid: {
                "rect": rect.as_list(),
                "label": nodes[nid]["label"],
                "sublabel": nodes[nid]["sublabel"],
                "color": roles[nodes[nid]["role"]],
                "role": nodes[nid]["role"],
                "shape": nodes[nid].get("shape", "box"),
                "detail_of": nodes[nid].get("detail_of", ""),
            }
            for nid, rect in node_rects.items()
        },
        "groups": {
            gid: {
                "rect": rect.as_list(),
                "label": groups[gid]["label"],
                "label_position": groups[gid]["label_position"],
            }
            for gid, rect in group_rects.items()
        },
        "edges": edges,
        # 版面层的键回落引擎产不出内容，但键必须在：几何的形状不随引擎变，
        # 下游（渲染模板、判据、测试）才不用按引擎分两套读法。
        "panels": [],
        "annotations": [],
        "spec_items": [],
        "block_notes": [],
        "legend": [{"role": role, "color": roles[role]} for role in legend_entries],
        "edge_legend": [
            {"role": role, "color": colour} for role, colour in edge_colours.items()
        ],
        "legend_y": round(MARGIN + notes_h, 2) if has_legend else None,
        "notes": notes,
        "notes_y": round(MARGIN, 2) if notes else None,
        "layout_findings": layout_findings,
        "fonts": {
            "node": NODE_FS,
            "sub": SUB_FS,
            "group": GROUP_LABEL_FS,
            "title": TITLE_FS,
            "legend": LEGEND_FS,
            "edge": EDGE_LABEL_FS,
            "note": NOTE_FS,
            "panel": PANEL_TITLE_FS,
            "spec": SPEC_FS,
        },
    }


def _place_edge_labels(
    edges: list[dict[str, Any]],
    boxes: list[_Rect],
    canvas: tuple[float, float] | list[float] | None = None,
) -> None:
    """把边标注挪到不压任何盒子的地方，然后**让同型的标注对齐**。

    2026-09-16 实测：上联的 "400G" 压在 "NIC 0" 上（机械审计报了文字碰撞）。
    标注落点原本只挑「最长那一段的中点」，而最长段常常正好横穿别的节点。
    这里沿着那条边的每一段扫一遍采样点，取第一个不压盒子的；一个都没有就
    退回原点并由 savefig 时的文字碰撞审计如实记账（不假装没事）。

    2026-09-17 看图又发现两件：
    - 四条一模一样的上联，四个 "400G" 落在四个高度上 —— 逐条各扫各的，路径
      形状差一点落点就差一截。**对称的东西画得不对称**，读者一眼就看出来。
      所以同角色同走向的一组标注，扫完再一起对齐到中位数（对齐后会压到东西
      的那条留在原地，不为了齐整去压别人）。
    - 标注之间从不互相避让 —— 判据里根本没有别的标注。已放好的标注加进碰撞
      集合。
    """

    def _orientation(points: list[tuple[float, float]]) -> str:
        dx = abs(points[-1][0] - points[0][0])
        dy = abs(points[-1][1] - points[0][1])
        return "vertical" if dy >= dx else "horizontal"

    # 按**边的索引**记账。第一版用一个只含带标注边的 list，索引跟 edges 对不
    # 上，于是「排除自己」排掉的是别人 —— 捆标注居中因此几乎全被挡住
    # （同 [[feedback_two_views_two_questions]]：拿错视图不报错，只悄悄给错答案）。
    placed_by_index: dict[int, _Rect] = {}
    sized: dict[int, tuple[float, float]] = {}
    for index, edge in enumerate(edges):
        label = edge.get("label")
        if not label:
            continue
        width = _text_width(label, EDGE_LABEL_FS) + 6.0
        height = EDGE_LABEL_FS * 1.5
        sized[index] = (width, height)
        best: tuple[float, float] | None = None
        points = [(float(x), float(y)) for x, y in edge["points"]]
        segments = sorted(
            zip(points, points[1:]),
            key=lambda pair: abs(pair[1][0] - pair[0][0]) + abs(pair[1][1] - pair[0][1]),
            reverse=True,
        )
        for (x0, y0), (x1, y1) in segments:
            for step in (0.5, 0.35, 0.65, 0.2, 0.8):
                cx = x0 + (x1 - x0) * step
                cy = y0 + (y1 - y0) * step + 5.0
                box = _Rect(cx - width / 2, cy - height / 2, width, height)
                if not any(
                    _overlaps(box, other)
                    for other in (*boxes, *placed_by_index.values())
                ):
                    best = (cx, cy)
                    break
            if best:
                break
        if best:
            edge["label_xy"] = [round(best[0], 2), round(best[1], 2)]
            edge.pop("label_unplaced", None)
            placed_by_index[index] = _Rect(
                best[0] - width / 2, best[1] - height / 2, width, height
            )
        else:
            # **一个字都没地方放，也得说出来。** 原先只是退回原点，注释说「由
            # savefig 时的文字碰撞审计记账」—— 那道审计只在 matplotlib 后端跑，
            # 默认的 TikZ 后端上完全没人管。2026-09-17 时序图基准实测：7 条消息
            # 全挤在同一行，标签叠成一团根本读不了，而交叉=0、贴线=0，机械判据
            # 一句话没说。**读不了的图比画错的图更危险**，因为它看着像通过了。
            edge["label_unplaced"] = True

    # 一捆 N 股共用一句标注（只挂在中间那股上）。标注要落在**捆的中心**，
    # 否则「2 × 400G」贴在其中一股旁边，读起来像「这一股是 2 × 400G」
    # （iter18 渲染出来看到的）。
    for index, edge in enumerate(edges):
        if index not in sized or not edge.get("label_xy") or edge.get("bundle", 1) < 2:
            continue
        mates = [
            other
            for other in edges
            if other.get("bundle") == edge["bundle"]
            and other["from"] == edge["from"]
            and other["to"] == edge["to"]
        ]
        if len(mates) < 2:
            continue
        centre = sum(m["points"][0][0] for m in mates) / len(mates)
        width, height = sized[index]
        moved = _Rect(centre - width / 2, edge["label_xy"][1] - height / 2, width, height)
        rivals = [r for i, r in placed_by_index.items() if i != index]
        if not any(_overlaps(moved, other) for other in (*boxes, *rivals)):
            edge["label_xy"] = [round(centre, 2), edge["label_xy"][1]]
            placed_by_index[index] = moved

    # 同角色 + 同标注文字 = 同型。它们该落在同一条线上。
    #
    # **对齐哪个轴，问的是这组同型边沿哪个方向铺开的**，不是每条边自己的走向。
    # 第一版按边自己的 dy>=dx 判走向再据此选轴：CPU 0→Switch 0 与
    # CPU 1→Switch 1 是一对镜像边，两条都判成「横」→ 去对齐 x —— 可它们本来
    # 就一左一右分开摆，x 对齐毫无意义，读者看见的是两个 PCIe 标签一高一低
    # （iter23 放大看到的）。铺开方向在 x 上，就该对齐 y。
    peers: dict[tuple[str, str], list[int]] = {}
    for index, edge in enumerate(edges):
        if index not in sized or not edge.get("label_xy"):
            continue
        peers.setdefault((edge.get("role") or "", edge["label"]), []).append(index)

    for members in peers.values():
        if len(members) < 2:
            continue
        xs = [edges[i]["label_xy"][0] for i in members]
        ys = [edges[i]["label_xy"][1] for i in members]
        spread_x, spread_y = max(xs) - min(xs), max(ys) - min(ys)
        axis = 1 if spread_x >= spread_y else 0
        spread_on_axis = spread_y if axis == 1 else spread_x
        # 判据要**无量纲**：对齐轴上的离散度，相对于这组铺开的方向要足够小。
        # 第一版拿画布尺寸当标尺，而版面模式下这函数是**逐块**调的、块画布只有
        # 170pt 高，于是 0.15×170=25.5 把 29.6 的离散度挡掉了 —— 一条本该对齐
        # 的四联上联标注就此散着（iter23）。同一个判据在整页调用时又太松。
        if spread_on_axis > max(spread_x, spread_y) * PEER_ALIGN_SPAN:
            continue
        values = sorted(edges[index]["label_xy"][axis] for index in members)
        target = values[len(values) // 2]
        others = {
            i: _Rect(
                edges[i]["label_xy"][0] - sized[i][0] / 2,
                edges[i]["label_xy"][1] - sized[i][1] / 2,
                *sized[i],
            )
            for i in members
        }
        for index in members:
            xy = list(edges[index]["label_xy"])
            if xy[axis] == target:
                continue
            if abs(xy[axis] - target) > PEER_ALIGN_MAX_SHIFT:
                continue  # 要挪这么远就不是同一处的一组，硬拉会把标注拽离它的线
            xy[axis] = target
            width, height = sized[index]
            box = _Rect(xy[0] - width / 2, xy[1] - height / 2, width, height)
            rivals = [r for i, r in others.items() if i != index]
            if any(_overlaps(box, other) for other in (*boxes, *rivals)):
                continue  # 对齐后会压到东西，就留在原地 —— 整齐不换正确
            edges[index]["label_xy"] = [round(xy[0], 2), round(xy[1], 2)]
            others[index] = box
            placed_by_index[index] = box


def _overlaps(a: _Rect, b: _Rect) -> bool:
    return (
        a.x < b.right and a.right > b.x and a.y < b.top and a.top > b.y
    )


def _side_columns(contract: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """立在整摞层旁边的那些节点，按左右分组。"""

    out: dict[str, list[dict[str, Any]]] = {"left": [], "right": []}
    for node in contract["nodes"]:
        if node.get("side") in out:
            out[node["side"]].append(node)
    return out


def _side_gutter(contract: dict[str, Any], which: str) -> float:
    """侧边一列要占的宽度。画布是在摆放之前定的 —— 不先留出来就画到外面。"""

    column = _side_columns(contract)[which]
    if not column:
        return 0.0
    # 轨道落在 SIDE_GAP 之内（inner_right + SIDE_RAIL_GAP < inner_right + SIDE_GAP），
    # 所以不必再为它单独加一份。
    return SIDE_GAP + max(_node_size(node)[0] for node in column)


def _self_loop_nodes(contract: dict[str, Any]) -> set[str]:
    """有自转移的节点。状态机里「心跳续期」「重试」都是自己指向自己。"""

    return {
        edge["from"] for edge in contract["edges"] if edge["from"] == edge["to"]
    }


def _self_loop_width(contract: dict[str, Any], node_id: str) -> float:
    """自环要占的横向宽度：环本身 + **它的标注**。

    只算环、不算标注时，「心跳续期（每 30 秒）」这行字没地方放，被判据点名
    （2026-09-17 状态机基准）。预留要按真正会画出来的东西算 —— 这条教训在
    图例、外侧车道上已经各栽过一次。
    """

    label = max(
        (
            _text_width(edge["label"], EDGE_LABEL_FS)
            for edge in contract["edges"]
            if edge["from"] == node_id and edge["to"] == node_id and edge["label"]
        ),
        default=0.0,
    )
    return SELF_LOOP_W + SELF_LOOP_GAP + label + 8.0


def _band_gaps(contract: dict[str, Any]) -> list[float]:
    """每一道带间空隙各自多高 —— 由**真正要穿过它的线数**决定。

    BAND_GAP 过去是一个常数 62.0，按最坏情况定的：65 条边全从上往下穿时，车道
    排不开就打结（实测压到 54，那张密图 281 → 317 个交叉）。可一旦扇子被收成
    组端点，大多数空隙只剩 0-2 条线穿过，62pt 就是纯空白。

    2026-09-17 实测那张 22 节点 / 10 边的图：三道空隙 93/78/78pt，而盒子本身
    只有 74.5pt 高；其中 obs↔data 那道**一条线都没穿过**，照样 78pt。画布
    587×1020，aspect 0.58。

    要穿过第 i 道空隙的边，是两端分别落在 i 之上和 i 之下的那些。这个数从合同
    就算得出来，不需要先布线 —— 和字宽两道次是同一个道理：先量，再摆。
    """

    bands = contract.get("bands") or []
    if len(bands) < 2:
        return []
    band_of: dict[str, int] = {}
    group_band: dict[str, int] = {}
    for index, band in enumerate(bands):
        for entry in band:
            text = str(entry)
            if text.startswith("group:"):
                gid = text[len("group:"):]
                group_band[gid] = index
                for nid in _group_member_ids(contract, gid):
                    band_of[nid] = index
            elif text.startswith("node:"):
                band_of[text[len("node:"):]] = index

    def band_index(endpoint: str) -> int | None:
        if endpoint.startswith("group:"):
            return group_band.get(endpoint[len("group:"):])
        return band_of.get(endpoint)

    crossings = [0] * (len(bands) - 1)
    for edge in contract.get("edges") or []:
        top = band_index(str(edge["from"]))
        bottom = band_index(str(edge["to"]))
        if top is None or bottom is None or top == bottom:
            continue
        weight = int(edge.get("represents") or 1)
        for gap in range(min(top, bottom), max(top, bottom)):
            crossings[gap] += weight

    # 一条线一条车道，车道之外还要留转弯的余地。上限保留旧常数 —— 密图（每道
    # 空隙十几条线）拿到的和从前一模一样宽，这条改动只把空掉的那些收回来。
    return [min(BAND_GAP, BAND_GAP_FLOOR + BAND_LANE * lanes) for lanes in crossings]


def _band_headroom(contract: dict[str, Any], idents: set[str]) -> float:
    """这条带子要不要为自环留出头部空间。

    自环画在节点正上方的空档里 —— 不先把它留出来，环就压到上一层身上，或者
    干脆画到画布外面（2026-09-17 状态机基准）。
    """

    return SELF_LOOP_H + SELF_LOOP_GAP if (idents & _self_loop_nodes(contract)) else 0.0


def _around_gutter(contract: dict[str, Any]) -> float:
    """走外侧的边要占的边距。

    画布宽度是在布线**之前**定的 —— 不先把这条留出来，回退边就画到画布外面
    被裁掉，而且什么都不会报（2026-09-17）。

    **留位置的条件必须等于画东西的条件。** 第一版只按「跨了几条带子」数，于是
    把那些其实走侧轨、走总线的边也算了进来：分层架构基准实测**零条边真的走外侧**，
    却两边各留了 84pt，右侧凭空空出 124pt 并触发一条空列 finding。这条教训图例
    那次刚栽过一次（legend_h 的条件用了「手里有的数据」而不是「真会画的东西」）。
    """

    aside = {node["id"] for node in contract["nodes"] if node.get("side")}
    in_group: dict[str, str] = {}
    for group in contract["groups"]:
        for rank in group["ranks"]:
            for nid in rank:
                in_group[nid] = group["id"]

    band_of: dict[str, int] = {}
    for index, band in enumerate(contract["bands"]):
        for entry in band:
            prefix, _, ident = entry.partition(":")
            if prefix == "node":
                band_of[ident] = index
            else:
                for group in contract["groups"]:
                    if group["id"] == ident:
                        for rank in group["ranks"]:
                            for nid in rank:
                                band_of[nid] = index
    count = 0
    for edge in contract["edges"]:
        src, dst = edge["from"], edge["to"]
        # 与 _route_edges 里的判定顺序一一对应：同组 → 侧轨 → 同带 → 外侧。
        if in_group.get(src) is not None and in_group.get(src) == in_group.get(dst):
            continue
        if src in aside or dst in aside:
            continue
        if band_of.get(src) == band_of.get(dst):
            continue
        if abs(band_of.get(src, 0) - band_of.get(dst, 0)) > 1:
            count += 1
    return 0.0 if not count else AROUND_MARGIN + AROUND_LANE_GAP * count


def _route_edges(
    contract: dict[str, Any],
    *,
    port_marks: dict[tuple[str, str], list[float]] | None = None,
    node_rects: dict[str, _Rect],
    group_rects: dict[str, _Rect],
    band_rects: list[_Rect],
    group_of: dict[str, str],
    band_of_node: dict[str, int],
    rank_of: dict[str, int],
    side_rail: dict[str, float] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """按声明逐条布线。遍历 `edges` —— 所以「漏画一条」不可能发生。

    三处排序决定了图好不好看，而它们都是**顺序问题**，不是坐标问题：

    1. 同一侧多条边的端口，按对端位置排序 —— 否则一个交换机扇出到 8 张卡时
       连线互相穿插（端口顺序 = 声明顺序 ≠ 卡的左右顺序）。
    2. 同组跨级（rank 差 > 1）的边走**空走廊**，不走直线 —— 直线会穿过中间那
       一级的盒子（v1 里 switch→NIC 的线正是从 GPU 行里穿过去的）。
    3. 跨带子的边按跨度排车道，跨得远的走外道 —— 否则 8 条上联在中间打结。
    """

    # 端点写成 `group:<id>` 的边，把那个组注册成一个**伪节点** —— 它的矩形就是
    # 组的矩形。这样布线、端口、车道、走廊那二十来处 `node_rects[...]` 一个字
    # 都不用改，组端点自然就走同一条路。伪节点只活在布线这一层：合同里没有它，
    # 节点计数、字宽实测、图例都看不见它。
    endpoint_groups = {
        end[len("group:"):]
        for edge in contract["edges"]
        for end in (edge["from"], edge["to"])
        if end.startswith("group:")
    }
    if endpoint_groups:
        node_rects = dict(node_rects)
        group_of = dict(group_of)
        band_of_node = dict(band_of_node)
        rank_of = dict(rank_of)
        for gid in endpoint_groups:
            rect = group_rects.get(gid)
            if rect is None:
                continue
            key = "group:" + gid
            node_rects[key] = rect
            # 组作为端点时**不属于任何组** —— 否则走廊逻辑会拿它自己的框当边界，
            # 把线关在框里出不来。
            members = [
                nid for nid, g in group_of.items() if g == gid and nid in band_of_node
            ]
            if members:
                band_of_node[key] = band_of_node[members[0]]
            rank_of[key] = 0

    all_boxes = list(node_rects.values())
    nodes_by_id = {node["id"]: node for node in contract["nodes"]}
    loop_nodes = _self_loop_nodes(contract)
    antiparallel = {
        (edge["from"], edge["to"])
        for edge in contract["edges"]
        if edge["from"] != edge["to"]
    }
    findings: list[dict[str, Any]] = []
    plans: list[dict[str, Any]] = []

    # 一笔代表 N 条链路，就画成 N 股 —— 而且每股各占一个端口。只画一股（哪怕
    # 加粗或双线）会让「8 个口」里有 4 个空着，读者数到的还是 4 条
    # （iter17 渲染出来看到的）。规则要一贯：represents=N 就是 N 股。
    # 标注只挂在中间那股上，否则「2 × 400G」会被印 N 遍。
    expanded: list[dict[str, Any]] = []
    for edge in contract["edges"]:
        weight = int(edge.get("represents") or 1)
        if weight <= 1:
            expanded.append(edge)
            continue
        middle = weight // 2
        for strand in range(weight):
            expanded.append(
                {**edge, "label": edge["label"] if strand == middle else "",
                 "represents": 1, "bundle": weight}
            )

    for index, edge in enumerate(expanded):
        src, dst = edge["from"], edge["to"]
        same_group = group_of.get(src) is not None and group_of.get(src) == group_of.get(dst)
        same_band = band_of_node.get(src) == band_of_node.get(dst)
        rank_gap = abs(rank_of.get(src, 0) - rank_of.get(dst, 0)) if same_group else 0
        if same_group and rank_gap == 0:
            mode = "horizontal"
        elif same_group and rank_gap == 1:
            mode = "vertical"
        elif same_group:
            mode = "detour"
        elif (side_rail or {}).get(src) is not None or (side_rail or {}).get(dst) is not None:
            # 一端立在整摞层旁边 —— 走它那条公共侧轨，不从别的行身上横穿。
            mode = "rail"
        elif (dst, src) in antiparallel and abs(
            band_of_node.get(src, 0) - band_of_node.get(dst, 0)
        ) == 1:
            # **反向平行对**（A→B 与 B→A）是状态机的招牌：批准/否决、暂停/恢复。
            # 走车道机制时两条会互相穿过（2026-09-17 状态机基准实测）；而它们
            # 两端的槽位本来就已经把它们分开了，直接连成两条并排的直线即可。
            mode = "vertical"
        elif same_band:
            mode = "lateral"
        elif abs(band_of_node.get(src, 0) - band_of_node.get(dst, 0)) > 1:
            # 跨了不止一条带子 —— 直着穿过去要从中间每一层身上压过。流程图里
            # 这就是**回退边**（「R² < 0.8 回到预处理」），通用画法是走外侧绕
            # 回去。2026-09-17 流程图基准实测：回退边竖穿图心，把两条并行分支
            # 切开，5 个交叉里有 2 个是它。
            mode = "around"
        else:
            mode = "crossband"

        a, b = node_rects[src], node_rects[dst]
        if mode in {"horizontal", "lateral"}:
            sides = ("right", "left") if a.cx <= b.cx else ("left", "right")
        else:
            sides = ("bottom", "top") if a.cy >= b.cy else ("top", "bottom")
        plans.append({"index": index, "edge": edge, "mode": mode, "sides": sides})

    # ① 端口顺序：同一节点同一侧的多条边，按**对端位置**排，连线才不交叉。
    side_members: dict[tuple[str, str], list[tuple[float, int]]] = {}
    for plan in plans:
        edge = plan["edge"]
        s_side, d_side = plan["sides"]
        for node_id, side, other_id in (
            (edge["from"], s_side, edge["to"]),
            (edge["to"], d_side, edge["from"]),
        ):
            other = node_rects[other_id]
            key = other.cx if side in {"top", "bottom"} else other.cy
            side_members.setdefault((node_id, side), []).append((key, plan["index"]))
    side_order: dict[tuple[str, str], list[int]] = {
        key: [index for _, index in sorted(members)]
        for key, members in side_members.items()
    }
    # ①' 端口落点：**对端出口的正下方**，而且**顺序也按对端出口排**。
    #
    # 上面那一遍按「对端**中心**」排 —— 同一台服务器的两条上联中心相同，于是
    # 先后是任意的（按边序号）。但它们真实的出口在自己盒宽的 1/3 和 2/3，
    # 两个顺序不一致时两条线就交叉。**第一次做这条时我只把出口用来定位置、
    # 没用来定顺序，校准集当场 0 个交叉变 4 个。**
    #
    # 能在这一步算出口，是因为上一遍已经定了每个节点自己那侧的 slot；
    # 对端是普通节点，出口就是 (slot+1)/(n+1)。不循环。
    def exit_on(node_id: str, side: str, edge_index: int) -> float | None:
        peers = side_order.get((node_id, side))
        if not peers or edge_index not in peers or node_id not in node_rects:
            return None
        t = (peers.index(edge_index) + 1) / (len(peers) + 1)
        return _anchor(node_rects[node_id], side, t)[0]

    opposite: dict[int, list[tuple[str, str]]] = {}
    for (nid, sd), users in side_order.items():
        for idx in users:
            opposite.setdefault(idx, []).append((nid, sd))

    port_at: dict[tuple[str, str], list[float]] = {}
    for (nid, sd), users in list(side_order.items()):
        declared = int((nodes_by_id.get(nid) or {}).get("ports") or 0)
        if declared < len(users) or sd not in {"top", "bottom"}:
            continue
        wants: list[tuple[float, int]] = []
        for idx in users:
            x = None
            for other, other_side in opposite.get(idx, []):
                if other != nid:
                    x = exit_on(other, other_side, idx)
                    break
            wants.append((x if x is not None else node_rects[nid].cx, idx))
        wants.sort()
        side_order[(nid, sd)] = [idx for _, idx in wants]
        rect = node_rects[nid]
        lo, hi = rect.x + PORT_W, rect.right - PORT_W
        placed: list[float] = []
        for x, _ in wants:
            x = max(lo, min(hi, x))
            if placed:
                x = max(x, placed[-1] + PORT_W * 1.5)
            placed.append(min(x, hi))
        port_at[(nid, sd)] = placed

    def port(node_id: str, side: str, edge_index: int) -> tuple[float, float]:
        users = side_order[(node_id, side)]
        slot = users.index(edge_index)
        declared = int((nodes_by_id.get(node_id) or {}).get("ports") or 0)
        if declared >= len(users) and side in {"top", "bottom"}:
            # **声明了端口，连线就落在端口上**。端口本来只是装饰（均布小方块），
            # 于是 8 个口画在一处、4 条上联落在另一处，对不上（2026-09-17 看图
            # 发现）。接上之后「8 口交换机」不再是装饰，而是线真的接在某号口。
            #
            # 选哪个口：离对端最近的那个，且保持 slot 的先后次序。
            # - 按几何排好的 slot 顺序**不能动** —— 第一版按 edge index 排，
            #   参考图当场从 0 个交叉变 14 个。吸附只改落点，不改顺序。
            # - 也不能均布 —— 第二版把 4 条线摊在第 1/3/5/7 号口上，每条都得
            #   横着拐一段才够得着。
            rect = node_rects[node_id]
            return (port_at[(node_id, side)][slot], _anchor(rect, side, 0.5)[1])
        t = (slot + 1) / (len(users) + 1)
        if side == "top" and node_id in loop_nodes:
            # 自环挂在右上角，顶边最右那一段归它 —— 别的边挤过来就会跟环相交
            # （2026-09-17 状态机基准：approval→running 正好撞上心跳自环）。
            t *= SELF_LOOP_TOP_SHARE
        # 左/右侧的 t 从下往上，视觉顺序与 cy 排序一致。
        return _anchor(node_rects[node_id], side, t)

    # ③' 多对多 → 一条共用总线。
    #
    # 六个执行节点各自连三个共用基础设施 = 18 条边，一条一条排车道时必然织成
    # 一张网：2026-09-17 分层架构基准实测 80-338 个交叉，agent 连渲六次，最后
    # 靠**删边**把 33 条减到 23 条 —— 用删信息换整齐，正是这套系统要拦的事。
    #
    # 这类结构的标准画法是总线：两层之间拉一条横干线，上面每个都垂下来、下面
    # 每个都接上去。只有**完全二分**（每个源都连每个目标）时它才与逐条画等价，
    # 不丢信息 —— 所以这就是判据。少一条边就不是总线，老老实实逐条画。
    bus_of: dict[int, str] = {}
    fans: dict[tuple[str, int, int], list[dict[str, Any]]] = {}
    for plan in plans:
        if plan["mode"] != "crossband":
            continue
        edge = plan["edge"]
        key = (
            edge.get("role", ""),
            band_of_node.get(edge["from"], -1),
            band_of_node.get(edge["to"], -1),
        )
        fans.setdefault(key, []).append(plan)
    for key, members in fans.items():
        sources = {plan["edge"]["from"] for plan in members}
        targets = {plan["edge"]["to"] for plan in members}
        pairs = {(plan["edge"]["from"], plan["edge"]["to"]) for plan in members}
        if len(sources) < 2 or len(targets) < 2:
            continue
        if len(pairs) != len(sources) * len(targets):
            continue
        for plan in members:
            plan["mode"] = "bus"
            bus_of[plan["index"]] = f"{key[0]}|{key[1]}->{key[2]}"

    # ③ 车道顺序：跨带子的边按水平跨度排，跨得远的走离目标更远的车道。
    #
    # 跨度必须按**真正的落点**算，不能按节点中心。一台交换机通栏摆着、8 条
    # 上联全落在它身上时，8 条边的「对端中心」是同一个数 —— 跨度全相等，排序
    # 退化成原始顺序，于是跑得远的那条反而后转弯，横向段被别人的下落段穿过。
    # iter14 实测：n1 的两条平行上联自己跟自己交叉（2026-09-17）。
    # 端口成了真连接点之后，中心就不再是落点了。
    crossband = [plan for plan in plans if plan["mode"] == "crossband"]
    spans = sorted(
        crossband,
        key=lambda plan: abs(
            port(plan["edge"]["from"], plan["sides"][0], plan["index"])[0]
            - port(plan["edge"]["to"], plan["sides"][1], plan["index"])[0]
        ),
    )
    lane_rank = {plan["index"]: rank for rank, plan in enumerate(spans)}

    out: list[dict[str, Any]] = []
    used_corridors: dict[str, list[float]] = {}
    for plan in plans:
        index = plan["index"]
        edge = plan["edge"]
        src, dst = edge["from"], edge["to"]
        s_side, d_side = plan["sides"]
        start = port(src, s_side, index)
        end = port(dst, d_side, index)
        mode = plan["mode"]

        if src == dst:
            # **自转移**：状态机里「心跳续期」「重试」都是自己指向自己。以前
            # 这种边被一律拒绝（注释说「只会画成一个点」）—— 于是状态机根本
            # 画不出来，agent 只好去试时序模式绕道，来回撞了两条拒绝
            # （2026-09-17 状态机基准实测）。画成节点正上方的一个小回环。
            # 画在**右上角**：顶边中间归进来的边用（第一版画在顶边正中，跟
            # 从上一层下来的那条边当场交叉），右边中间归同带子的横向边用。
            rect = node_rects[src]
            start = _anchor(rect, "top", 0.82)
            end = _anchor(rect, "right", 0.78)
            top = rect.top + SELF_LOOP_H
            far = rect.right + SELF_LOOP_W
            points = [start, (start[0], top), (far, top), (far, end[1]), end]
            out.append(
                {
                    "from": src,
                    "to": dst,
                    "points": [[round(x, 2), round(y, 2)] for x, y in points],
                    "label": edge["label"],
                    "label_xy": [round(far + 4.0, 2), round(top + 2.0, 2)],
                    "kind": edge["kind"],
                    "role": edge.get("role", ""),
                    "bidirectional": edge["bidirectional"],
                    "bundle": 1,
                    "arrow": True,
                    "mode": "self-loop",
                    "bus": "",
                }
            )
            continue
        if mode == "rail":
            rails = side_rail or {}
            aside = src if rails.get(src) is not None else dst
            other = dst if aside == src else src
            rail_x = rails[aside]
            a, b = node_rects[aside], node_rects[other]
            facing = "left" if rail_x < a.cx else "right"
            aside_anchor = _anchor(a, facing, 0.5)
            other_anchor = _anchor(b, "right" if facing == "left" else "left", 0.5)
            path = [
                other_anchor,
                (rail_x, other_anchor[1]),
                (rail_x, aside_anchor[1]),
                aside_anchor,
            ]
            points = path if other == src else list(reversed(path))
            bus_of[index] = f"rail:{aside}"
        elif mode == "bus":
            a, b = node_rects[src], node_rects[dst]
            top, bottom = (a, b) if a.cy >= b.cy else (b, a)
            bus_y = (top.y + bottom.top) / 2.0
            start = _anchor(a, "bottom" if a is top else "top", 0.5)
            end = _anchor(b, "top" if a is top else "bottom", 0.5)
            points = [start, (start[0], bus_y), (end[0], bus_y), end]
        elif mode == "around":
            # 走图外侧：从离得近的那一侧出线 → 竖着走到目标那一层 → 横回目标。
            left = min(rect.x for rect in node_rects.values())
            right = max(rect.right for rect in node_rects.values())
            a, b = node_rects[src], node_rects[dst]
            go_left = (a.cx + b.cx) / 2.0 <= (left + right) / 2.0
            taken = used_corridors.setdefault("around", [])
            step = AROUND_LANE_GAP * (len(taken) + 1)
            lane_x = (left - AROUND_MARGIN - step) if go_left else (
                right + AROUND_MARGIN + step
            )
            taken.append(lane_x)
            side = "left" if go_left else "right"
            start = _anchor(a, side, 0.5)
            end = _anchor(b, side, 0.5)
            points = [start, (lane_x, start[1]), (lane_x, end[1]), end]
        elif mode in {"horizontal", "vertical"}:
            points = [start, end]
        elif mode == "detour":
            # ② 同组跨级：走一条不碰任何盒子的竖直走廊，不从中间那级身上压过去。
            bounds = group_rects[group_of[src]]
            taken = used_corridors.setdefault(group_of[src], [])
            lane_x = _clear_x(
                node_rects[src], start[1], end[1], all_boxes, bounds,
                prefer=end[0], avoid=tuple(taken),
            )
            taken.append(lane_x)
            points = [start, (lane_x, start[1] - 10.0), (lane_x, end[1] + 10.0), end]
            if abs(lane_x - start[0]) <= 1 and _blocked(
                start[0], start[1], end[1], all_boxes, {id(node_rects[src]), id(node_rects[dst])}
            ):
                findings.append(
                    {
                        "collector": "OB-LAYOUT",
                        "message": (
                            f"edge {src}->{dst} spans non-adjacent ranks and no clear "
                            "corridor was found inside the group; the line may cross a "
                            "component"
                        ),
                    }
                )
        elif mode == "lateral":
            lane_x = (start[0] + end[0]) / 2.0
            points = [start, (lane_x, start[1]), (lane_x, end[1]), end]
        else:
            src_box = group_rects.get(group_of.get(src, ""), node_rects[src])
            dst_box = group_rects.get(group_of.get(dst, ""), node_rects[dst])
            going_down = start[1] >= end[1]
            exit_y = src_box.y if going_down else src_box.top
            entry_y = dst_box.top if going_down else dst_box.y
            src_taken = used_corridors.setdefault("band:" + str(group_of.get(src) or src), [])
            dst_taken = used_corridors.setdefault("band:" + str(group_of.get(dst) or dst), [])
            exit_x = (
                _clear_x(
                    node_rects[src], start[1], exit_y, all_boxes, src_box,
                    prefer=start[0], avoid=tuple(src_taken),
                )
                if group_of.get(src)
                else start[0]
            )
            entry_x = (
                _clear_x(
                    node_rects[dst], end[1], entry_y, all_boxes, dst_box,
                    prefer=end[0], avoid=tuple(dst_taken),
                )
                if group_of.get(dst)
                else end[0]
            )
            src_taken.append(exit_x)
            dst_taken.append(entry_x)
            if abs(exit_x - start[0]) > 1:
                findings.append(
                    {
                        "collector": "OB-LAYOUT",
                        "message": (
                            f"edge {src}->{dst} leaves its group through a shifted "
                            "corridor because the straight exit crossed another component"
                        ),
                    }
                )
            gutter = abs(exit_y - entry_y)
            total_lanes = max(1, len(lane_rank))
            step = min(14.0, max(4.0, gutter / (total_lanes + 2)))
            # 跨度最小的贴近目标，最大的走最外侧 —— 车道因此不互相穿越。
            offset = (lane_rank.get(index, 0) + 1) * step
            lane_y = entry_y + (offset if going_down else -offset)
            points = [
                start,
                (exit_x, start[1]),
                (exit_x, lane_y),
                (entry_x, lane_y),
                (entry_x, end[1]),
                end,
            ]
            deduped: list[tuple[float, float]] = []
            for point in points:
                if not deduped or (
                    abs(point[0] - deduped[-1][0]) > 0.5 or abs(point[1] - deduped[-1][1]) > 0.5
                ):
                    deduped.append(point)
            points = deduped

        # 标注挂在**最长那一段**的中点，不是折线的中间那个点。折线中点常常
        # 正好落在拐角或汇聚处 —— v1 那张图里 8 条上联的「400G」全挤成一团，
        # 就是这么来的。最长段在每条边上各不相同，标注因此自然分开。
        longest = max(
            zip(points, points[1:]),
            key=lambda pair: abs(pair[1][0] - pair[0][0]) + abs(pair[1][1] - pair[0][1]),
        )
        (lx0, ly0), (lx1, ly1) = longest
        label_xy = ((lx0 + lx1) / 2.0, (ly0 + ly1) / 2.0 + 5.0)
        out.append(
            {
                "from": src,
                "to": dst,
                "points": [[round(x, 2), round(y, 2)] for x, y in points],
                "label": edge["label"],
                "label_xy": [round(label_xy[0], 2), round(label_xy[1], 2)],
                "kind": edge["kind"],
                "role": edge.get("role", ""),
                "bidirectional": edge["bidirectional"],
                # 几何里每条都是一股；bundle=N 说明它属于一笔 represents=N 的捆。
                "bundle": int(edge.get("bundle") or 1),
                "mode": mode,
                # 同一条总线上的边共线是有意为之，不是「两条线叠成一条」。
                "bus": bus_of.get(index, ""),
            }
        )
    # 端口小方块要画在**线真正落下的地方** —— 位置由布线器定，标记照着画。
    # 两边各算各的，正是 zigzag 的来源。
    if port_marks is not None:
        port_marks.update(port_at)
    return out, findings


# ── 后端 1：matplotlib（默认；今天就能出 PNG/PDF/SVG）────────────────────
_MPL_TEMPLATE = '''\
# 由 harness figure-contract 编译器生成（backend=matplotlib）。
# 这段代码是**从合同算出来的**，不是手写坐标：改图请改合同，不要改这里。
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, FancyBboxPatch, Rectangle

# 字体栈内嵌在脚本里，不靠执行环境注入：这段代码要能被 referee 在**harness
# 之外**原样重跑并复现字节，靠环境注入的东西在那边就不在场（中文全成豆腐块）。
plt.rcParams["font.sans-serif"] = json.loads(r"""{font_stack}""")
plt.rcParams["axes.unicode_minus"] = False

GEOMETRY = json.loads(r"""{geometry}""")
OUTPUTS = json.loads(r"""{outputs}""")

W, H = GEOMETRY["canvas"]
FS = GEOMETRY["fonts"]
DPI = {dpi}
EDGE = "#2B2B2B"
LINK = "#5A5A5A"
GROUP_FACE = "#F6F7F9"
# detail_of 徽章的字是框架写的：跟调用方的语言政策走
SEE, SEE_OTHER = (("see", "see other panel") if GEOMETRY.get("text_language") == "en"
                  else ("详见", "详见另一栏"))

fig = plt.figure(figsize=(W / 72.0, H / 72.0), dpi=DPI)
ax = fig.add_axes([0, 0, 1, 1])
ax.set_xlim(0, W)
ax.set_ylim(0, H)
ax.axis("off")
fig.patch.set_facecolor("white")


def _pt(size):
    """点 → matplotlib 字号：坐标系就是点，所以 1:1。"""
    return size


if GEOMETRY.get("title"):
    ax.text(W / 2.0, GEOMETRY["title_y"], GEOMETRY["title"], ha="center", va="bottom",
            fontsize=_pt(FS["title"]), fontweight="bold", color="#111111", zorder=6)

# 分栏（panel）：带标题的版面块。①②③ 那种叙事就落在这里。
for panel in GEOMETRY.get("panels") or []:
    px, py, pw, ph = panel["rect"]
    ax.add_patch(FancyBboxPatch((px, py), pw, ph,
                                boxstyle="round,pad=0,rounding_size=10",
                                linewidth=1.2, edgecolor="#D7DCE3",
                                facecolor="#FFFFFF", zorder=0))
    if panel["title"]:
        tx, ty = panel["title_xy"]
        if panel.get("number"):
            # 编号是**画**出来的圆圈+数字，不是排出来的 ①：任何字体都画得出。
            cy = ty + FS["panel"] * 0.36
            ax.add_patch(Circle((tx + {badge_r}, cy), {badge_r}, linewidth=1.1,
                                edgecolor="#2B3038", facecolor="none", zorder=5))
            ax.text(tx + {badge_r}, cy, str(panel["number"]), ha="center",
                    va="center", fontsize=_pt(FS["panel"]) * 0.8,
                    fontweight="bold", color="#2B3038", zorder=6)
            tx += {badge_r} * 2 + 7
        ax.text(tx, ty, panel["title"], ha="left", va="bottom",
                fontsize=_pt(FS["panel"]), fontweight="bold",
                color="#2B3038", zorder=5)

# 出框叙事引出：说的是「这条线去哪儿了」，不是拓扑边。
for annot in GEOMETRY.get("annotations") or []:
    sx, sy = annot["start"]; ex, ey = annot["end"]
    ax.annotate("", xy=(ex, ey), xytext=(sx, sy), zorder=4,
                arrowprops=dict(arrowstyle="-|>", color="#8A6D3B", lw=1.6,
                                shrinkA=0, shrinkB=0))
    tx, ty = annot["text_xy"]
    ha, va = annot["align"]
    ax.text(tx, ty, annot["text"], ha=ha, va=va,
            fontsize={annot_fs}, color="#8A6D3B", zorder=5)

for item in GEOMETRY.get("spec_items") or []:
    sx, sy = item["xy"]
    ax.text(sx, sy, "\u2022 " + item["text"], ha="left", va="baseline",
            fontsize=_pt(FS["spec"]), color="#3C4349", zorder=5)

for item in GEOMETRY.get("block_notes") or []:
    nx, ny = item["xy"]
    ax.text(nx, ny, item["text"], ha="center", va="baseline",
            fontsize=_pt(FS["note"]), color="#4A5158", zorder=5)

for gid, group in GEOMETRY["groups"].items():
    x, y, w, h = group["rect"]
    dashed = group.get("style") == "dashed"
    ax.add_patch(FancyBboxPatch((x, y), w, h,
                                boxstyle="round,pad=0,rounding_size=7",
                                linewidth=1.6, edgecolor="#9AA3AD",
                                linestyle="--" if dashed else "-",
                                facecolor="none" if dashed else GROUP_FACE,
                                zorder=1))
    if group["label"]:
        if group["label_position"] == "top":
            ax.text(x + w / 2.0, y + h - 16, group["label"], ha="center", va="center",
                    fontsize=_pt(FS["group"]), fontweight="bold", color="#33383D", zorder=5)
        else:
            ax.text(x + w / 2.0, y + 12, group["label"], ha="center", va="center",
                    fontsize=_pt(FS["group"]), fontweight="bold", color="#33383D", zorder=5)

for edge in GEOMETRY["edges"]:
    pts = edge["points"]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    style = dict(color=edge.get("color") or LINK, zorder=2,
                 linewidth=2.6 if edge["kind"] == "bus" else 1.5,
                 linestyle="--" if edge["kind"] == "dashed" else "-",
                 solid_capstyle="round")
    ax.plot(xs, ys, **style)
    if edge.get("arrow") and not edge["bidirectional"]:
        (x0, y0), (x1, y1) = pts[-2], pts[-1]
        ax.annotate("", xy=(x1, y1), xytext=(x0, y0), zorder=3,
                    arrowprops=dict(arrowstyle="-|>", color=style["color"],
                                    lw=style["linewidth"], shrinkA=0, shrinkB=0))
    if edge["bidirectional"]:
        for (x0, y0), (x1, y1) in ((pts[1], pts[0]), (pts[-2], pts[-1])):
            ax.annotate("", xy=(x1, y1), xytext=(x0, y0), zorder=3,
                        arrowprops=dict(arrowstyle="-|>", color=LINK,
                                        lw=style["linewidth"], shrinkA=0, shrinkB=0))
    if edge["label"]:
        lx, ly = edge["label_xy"]
        ax.text(lx, ly, edge["label"], ha="center", va="bottom",
                fontsize=_pt(FS["edge"]), color="#3C4349", zorder=5,
                bbox=dict(boxstyle="round,pad=0.22", facecolor="white",
                          edgecolor="none", alpha=0.92))

for life in GEOMETRY.get("lifelines") or []:
    ax.plot([life["x"], life["x"]], [life["bottom"], life["top"]],
            color="#9AA3AE", linewidth=1.0, linestyle=(0, (4, 4)), zorder=1)

for nid, node in GEOMETRY["nodes"].items():
    x, y, w, h = node["rect"]
    shape = node.get("shape", "box")
    face = node.get("fill", node["color"])
    line = node.get("stroke", EDGE)
    ink = node.get("ink", "#222222")
    ax.add_patch(Rectangle((x, y), w, h,
                           linewidth=(0.8 if shape == "chip" else 1.3) * node.get("line_weight", 1.0),
                           edgecolor=line, facecolor=face, zorder=3))
    for px, py, pwid, phgt in node.get("ports") or []:
        ax.add_patch(Rectangle((px, py), pwid, phgt, linewidth=0.8,
                               edgecolor=line, facecolor=line, zorder=5))
    if node.get("detail_ref") is not None:
        # 内嵌第二道边框 = 「这个盒子里面还有东西，画在别处」。
        # 用 is not None 而不是真值判断：0 表示「有归属但那一栏没编号」，
        # 仍然要画框（只是没有号可指）—— 写成 if detail_ref: 时 0 被当成没有，
        # 声明又变回零像素（2026-09-17）。
        ax.add_patch(Rectangle((x + 3, y + 3), w - 6, h - 6, linewidth=0.7,
                               edgecolor=ink, facecolor="none",
                               alpha=0.35, zorder=4))
        _r = FS["sub"] * 0.62
        if node["detail_ref"]:
            ax.add_patch(Circle((x + w - 5 - _r, y + 4 + _r), _r, linewidth=0.8,
                                edgecolor=ink, facecolor="none", zorder=5))
            ax.text(x + w - 5 - _r, y + 4 + _r, str(node["detail_ref"]),
                    ha="center", va="center", fontsize=_pt(FS["sub"]) * 0.75,
                    color=ink, zorder=6)
            ax.text(x + w - 5 - _r * 2 - 3, y + 4, SEE, ha="right", va="bottom",
                    fontsize=_pt(FS["sub"]) * 0.85, color=ink, zorder=5)
        else:
            ax.text(x + w - 5, y + 4, SEE_OTHER, ha="right", va="bottom",
                    fontsize=_pt(FS["sub"]) * 0.85, color=ink, zorder=5)
    if shape == "chip":
        ax.text(x + w / 2.0, y + h / 2.0, node["label"], ha="center", va="center",
                fontsize={chip_fs}, fontweight="bold", color=ink, zorder=4)
    elif node["sublabel"]:
        ax.text(x + w / 2.0, y + h / 2.0 + FS["sub"] * 0.55, node["label"],
                ha="center", va="center", fontsize=_pt(FS["node"]),
                fontweight="bold", color=ink, zorder=4)
        ax.text(x + w / 2.0, y + h / 2.0 - FS["node"] * 0.62, node["sublabel"],
                ha="center", va="center", fontsize=_pt(FS["sub"]),
                color=ink, zorder=4)
    else:
        ax.text(x + w / 2.0, y + h / 2.0, node["label"], ha="center", va="center",
                fontsize=_pt(FS["node"]), fontweight="bold", color=ink, zorder=4)

if GEOMETRY.get("legend") or GEOMETRY.get("edge_legend"):
    entries = GEOMETRY["legend"]
    swatch = {swatch}
    gap = {legend_gap}
    widths = [swatch + 7 + len(e["role"]) * FS["legend"] * {legend_ratio} for e in entries]
    total = sum(widths) + gap * (len(entries) - 1)
    cursor = (W - total) / 2.0
    base = GEOMETRY["legend_y"]
    edge_entries = GEOMETRY.get("edge_legend") or []
    edge_widths = [{legend_line} + 6 + len(e["role"]) * FS["legend"] * {legend_ratio}
                   for e in edge_entries]
    total = sum(widths) + sum(edge_widths) + gap * (len(entries) + len(edge_entries) - 1)
    cursor = (W - total) / 2.0
    for entry, width in zip(entries, widths):
        ax.add_patch(Rectangle((cursor, base), swatch, swatch, linewidth=1.0,
                               edgecolor=entry.get("stroke", EDGE),
                               facecolor=entry.get("fill", entry["color"]), zorder=4))
        ax.text(cursor + swatch + 7, base + swatch / 2.0, entry["role"],
                ha="left", va="center", fontsize=_pt(FS["legend"]), color="#33383D", zorder=4)
        cursor += width + gap
    # 边的图例用一小段线，不是色块 —— 图例里的样子要等于图上的样子。
    for entry, width in zip(edge_entries, edge_widths):
        ax.plot([cursor, cursor + {legend_line}], [base + swatch / 2.0] * 2,
                color=entry["color"], lw=2.2, zorder=4, solid_capstyle="round")
        ax.text(cursor + {legend_line} + 6, base + swatch / 2.0, entry["role"],
                ha="left", va="center", fontsize=_pt(FS["legend"]), color="#33383D", zorder=4)
        cursor += width + gap

if GEOMETRY.get("notes"):
    base = GEOMETRY["notes_y"]
    for offset, note in enumerate(reversed(GEOMETRY["notes"])):
        ax.text(W / 2.0, base + offset * FS["note"] * 1.7, note, ha="center", va="bottom",
                fontsize=_pt(FS["note"]), color="#4A5158", zorder=4)

for path in OUTPUTS:
    fig.savefig(path, dpi=DPI, facecolor="white")
print("contract-render: wrote " + ", ".join(OUTPUTS))
'''


# ── 后端 2：TikZ / xelatex（出版级排版，原生矢量）────────────────────────
_TIKZ_TEMPLATE = '''\
# 由 harness figure-contract 编译器生成（backend=tikz）。
# 同一份合同的第二个后端：几何与 matplotlib 后端来自同一次 layout_contract()。
#
# 这个脚本**只写 .tex 和清单，不起任何子进程**。TeX 编译由框架经 latex.run_tex 执行
# （与稿件同一个编译器、同一堵墙），光栅化在框架进程里（tools/figure.py::_finish_tikz_render）。
# 2026-09-18 实测：脚本自己起 xelatex 子进程会被模型代码的高危扫描器当成
# shell-out 拦下等人批；无人值守的 run 里同一堵墙连撞七次，模型只好绕去手写
# matplotlib。框架生成的代码不该走模型代码的审批漏斗 —— 与其加一个绕过开关，
# 不如让它根本没有可被拦的东西。
import json
from pathlib import Path

TEX = r\"\"\"{tex}\"\"\"
OUTPUTS = json.loads(r\"\"\"{outputs}\"\"\")
DPI = {dpi}

work = Path("_contract_tikz")
work.mkdir(exist_ok=True)
(work / "figure.tex").write_text(TEX, encoding="utf-8")
(work / "manifest.json").write_text(
    json.dumps({{"outputs": OUTPUTS, "dpi": DPI}}, ensure_ascii=False), encoding="utf-8"
)
print("contract-render: figure.tex + manifest.json written; the framework compiles them")
'''

_TIKZ_DOC = r"""\documentclass[border=6pt]{standalone}
\usepackage{fontspec}
\usepackage{tikz}
\usetikzlibrary{positioning,fit,backgrounds,arrows.meta}
%(cjk)s
\begin{document}
\begin{tikzpicture}[x=1pt,y=1pt,every node/.style={inner sep=0pt,outer sep=0pt}]
%(body)s
\end{tikzpicture}
\end{document}
"""


def _tex_escape(text: str) -> str:
    out = []
    for char in str(text or ""):
        if char in "&%$#_{}":
            out.append("\\" + char)
        elif char == "~":
            out.append(r"\textasciitilde{}")
        elif char == "^":
            out.append(r"\textasciicircum{}")
        elif char == "\\":
            out.append(r"\textbackslash{}")
        else:
            out.append(char)
    return "".join(out)


def _needs_cjk(geometry: dict[str, Any]) -> bool:
    blob = json.dumps(geometry, ensure_ascii=False)
    return any(ord(char) >= 0x2E80 for char in blob)


def _cjk_preamble() -> str:
    """CJK 字体：**扫盘**，不写名单。

    这是 fonts.py 立过的规矩（它就是因为「名单里列的全是开发机上的字体、
    Linux 上一个都没有、于是静默落到无 CJK 字形的 DejaVu」才存在的）。TeX
    这边同理：写死 `\\setCJKmainfont{Noto Sans CJK SC}` 在没装它的机器上
    直接编译失败（本机实测 fontspec Error）。

    所以：按字形覆盖扫出本机真正装了的 CJK 字体族，点名第一个；一个都没扫到
    就只 `\\usepackage{ctex}` 让它自己挑（挑不到会响亮报错，不会静默出方块）。
    """

    try:
        from .fonts import cjk_families

        families = list(cjk_families())
    except Exception:
        families = []
    if not families:
        return r"\usepackage{ctex}"
    # BoldFont 不猜名字：粗体缺失时 fontspec 用合成粗体，比猜错一个族名安全。
    #
    # 符号也交给 CJK 字体。xeCJK 默认只把汉字判成 CJK，`≥ ① → ×` 这类符号走
    # **拉丁正文字体**（Latin Modern），而它没有这些字形 —— 整个字符被静默丢掉。
    # 2026-09-17 流程图基准实测：合同写「判定：R² ≥ 0.8 ？」，图上只有
    # 「判定：R² 0.8」。更麻烦的是 fonts.missing_glyphs() 查的是 CJK 字体，
    # 于是**判据说没问题、图上字没了** —— 判据和渲染各看各的。
    #
    # 与其削弱判据，不如让渲染跟判据对齐：把符号区也划给 CJK 字体，判据问的
    # 那份覆盖就真的是渲染用的那份。CJK 字体万一也没有，出来的是方框（看得见）
    # 而且判据会如实报，不再是静默消失。
    ranges = ",".join([
        '"2000->"206F',   # 常用标点
        '"2190->"21FF',   # 箭头
        '"2200->"22FF',   # 数学算子（≥ ≤ ≈ ×）
        '"2460->"24FF',   # 带圈数字 ①②③
        '"25A0->"27BF',   # 几何图形与符号
        '"2E80->"2EFF',   # CJK 部首补充
        '"3000->"303F',   # CJK 标点
        '"FF00->"FFEF',   # 全角形式
    ])
    return "\n".join([
        r"\usepackage{ctex}",
        rf"\setCJKmainfont{{{families[0]}}}",
        rf"\xeCJKDeclareCharClass{{CJK}}{{{ranges}}}",
    ])


def _tikz_body(geometry: dict[str, Any]) -> str:
    lines: list[str] = []
    fonts = geometry["fonts"]
    # detail_of 徽章的字是框架写的：跟调用方的语言政策走
    see, see_other = (
        ("see", "see other panel") if geometry.get("text_language") == "en"
        else ("详见", "详见另一栏")
    )
    for panel in geometry.get("panels") or []:
        px, py, pw, ph = panel["rect"]
        lines.append(
            rf"\draw[rounded corners=10pt,draw=black!18,fill=white,line width=0.9pt] "
            rf"({px},{py}) rectangle ({px + pw},{py + ph});"
        )
        if panel["title"]:
            tx, ty = panel["title_xy"]
            if panel.get("number"):
                cy = ty + fonts["panel"] * 0.36
                lines.append(
                    rf"\draw[draw=black!82,line width=0.8pt] "
                    rf"({tx + BADGE_R},{cy}) circle ({BADGE_R});"
                )
                lines.append(
                    rf"\node[anchor=center,text=black!82] at ({tx + BADGE_R},{cy}) "
                    rf"{{\fontsize{{{fonts['panel'] * 0.8}}}"
                    rf"{{{fonts['panel']}}}\selectfont\bfseries {panel['number']}}};"
                )
                tx += BADGE_R * 2 + 7
            lines.append(
                rf"\node[anchor=south west,text=black!82] at ({tx},{ty}) "
                rf"{{\fontsize{{{fonts['panel']}}}{{{fonts['panel'] * 1.2}}}\selectfont"
                rf"\bfseries {_tex_escape(panel['title'])}}};"
            )
    for annot in geometry.get("annotations") or []:
        sx, sy = annot["start"]; ex, ey = annot["end"]
        lines.append(
            rf"\draw[-{{Latex[length=5pt]}},draw=brown!70!black,line width=1.1pt] "
            rf"({sx},{sy}) -- ({ex},{ey});"
        )
        tx, ty = annot["text_xy"]
        anchor = {
            ("center", "top"): "north", ("center", "bottom"): "south",
            ("right", "center"): "east", ("left", "center"): "west",
        }[tuple(annot["align"])]
        lines.append(
            rf"\node[anchor={anchor},text=brown!70!black] at ({tx},{ty}) "
            rf"{{\fontsize{{{ANNOT_FS}}}{{{ANNOT_FS * 1.2}}}\selectfont "
            rf"{_tex_escape(annot['text'])}}};"
        )
    for item in geometry.get("spec_items") or []:
        sx, sy = item["xy"]
        lines.append(
            rf"\node[anchor=base west,text=black!72] at ({sx},{sy}) "
            rf"{{\fontsize{{{fonts['spec']}}}{{{fonts['spec'] * 1.2}}}\selectfont "
            rf"$\bullet$~{_tex_escape(item['text'])}}};"
        )
    for item in geometry.get("block_notes") or []:
        nx, ny = item["xy"]
        lines.append(
            rf"\node[anchor=base,text=black!70] at ({nx},{ny}) "
            rf"{{\fontsize{{{fonts['note']}}}{{{fonts['note'] * 1.2}}}\selectfont "
            rf"{_tex_escape(item['text'])}}};"
        )
    for group in geometry["groups"].values():
        x, y, w, h = group["rect"]
        dashed = group.get("style") == "dashed"
        fill = "none" if dashed else "gray!6"
        dash = ",dashed" if dashed else ""
        lines.append(
            rf"\draw[rounded corners=7pt,draw=gray!60,fill={fill},"
            rf"line width=1.2pt{dash}] ({x},{y}) rectangle ({x + w},{y + h});"
        )
        if group["label"]:
            ly = y + h - 16 if group["label_position"] == "top" else y + 12
            lines.append(
                rf"\node[anchor=center,text=black!80] at ({x + w / 2},{ly}) "
                rf"{{\fontsize{{{fonts['group']}}}{{{fonts['group'] * 1.2}}}\selectfont"
                rf"\bfseries {_tex_escape(group['label'])}}};"
            )
    for life in geometry.get("lifelines") or []:
        lines.append(
            rf"\draw[draw=black!35,line width=0.7pt,dashed] "
            rf"({life['x']},{life['bottom']}) -- ({life['x']},{life['top']});"
        )
    for edge in geometry["edges"]:
        path = " -- ".join(f"({x},{y})" for x, y in edge["points"])
        width = 2.2 if edge["kind"] == "bus" else 1.2
        dash = ",dashed" if edge["kind"] == "dashed" else ""
        arrow = (
            "{Latex[length=5pt]}-{Latex[length=5pt]}"
            if edge["bidirectional"]
            else ("-{Latex[length=5pt]}" if edge.get("arrow") else "-")
        )
        colour = (
            _tikz_color(edge["color"]) if edge.get("color") else "black!60"
        )
        lines.append(
            rf"\draw[{arrow},draw={colour},line width={width}pt{dash}] {path};"
        )
        if edge["label"]:
            lx, ly = edge["label_xy"]
            lines.append(
                rf"\node[anchor=south,fill=white,inner sep=1.5pt,text=black!75] "
                rf"at ({lx},{ly}) {{\fontsize{{{fonts['edge']}}}{{{fonts['edge'] * 1.2}}}"
                rf"\selectfont {_tex_escape(edge['label'])}}};"
            )
    for node in geometry["nodes"].values():
        x, y, w, h = node["rect"]
        shape = node.get("shape", "box")
        stroke = (0.6 if shape == "chip" else 1.0) * node.get("line_weight", 1.0)
        face = _tikz_color(node["fill"])
        line = _tikz_color(node["stroke"])
        ink = _tikz_color(node["ink"])
        lines.append(
            rf"\draw[fill={{{face}}},draw={{{line}}},"
            rf"line width={stroke}pt] ({x},{y}) rectangle ({x + w},{y + h});"
        )
        for px, py, pwid, phgt in node.get("ports") or []:
            # 端口是实心的：它小，浅底会看不见
            lines.append(
                rf"\draw[fill={{{line}}},draw={{{line}}},"
                rf"line width=0.5pt] ({px},{py}) rectangle ({px + pwid},{py + phgt});"
            )
        if node.get("detail_ref") is not None:
            lines.append(
                rf"\draw[draw={{{ink}}},opacity=0.35,line width=0.4pt] "
                rf"({x + 3},{y + 3}) rectangle ({x + w - 3},{y + h - 3});"
            )
            _r = fonts["sub"] * 0.62
            if node["detail_ref"]:
                lines.append(
                    rf"\draw[draw={{{ink}}},opacity=0.75,line width=0.5pt] "
                    rf"({x + w - 5 - _r},{y + 4 + _r}) circle ({_r});"
                )
                lines.append(
                    rf"\node[anchor=center,text={{{ink}}}] at "
                    rf"({x + w - 5 - _r},{y + 4 + _r}) "
                    rf"{{\fontsize{{{fonts['sub'] * 0.75}}}{{{fonts['sub']}}}"
                    rf"\selectfont {node['detail_ref']}}};"
                )
                lines.append(
                    rf"\node[anchor=south east,text={{{ink}}}] at "
                    rf"({x + w - 5 - _r * 2 - 3},{y + 4}) "
                    rf"{{\fontsize{{{fonts['sub'] * 0.85}}}{{{fonts['sub']}}}\selectfont "
                    rf"{_tex_escape(see)}}};"
                )
            else:
                lines.append(
                    rf"\node[anchor=south east,text={{{ink}}}] at ({x + w - 5},{y + 4}) "
                    rf"{{\fontsize{{{fonts['sub'] * 0.85}}}{{{fonts['sub']}}}\selectfont "
                    rf"{_tex_escape(see_other)}}};"
                )
        if shape == "chip":
            lines.append(
                rf"\node[anchor=center,text={{{ink}}}] at ({x + w / 2},{y + h / 2}) "
                rf"{{\fontsize{{{CHIP_FS}}}{{{CHIP_FS * 1.2}}}\selectfont"
                rf"\bfseries {_tex_escape(node['label'])}}};"
            )
        elif node["sublabel"]:
            lines.append(
                rf"\node[anchor=center,text={{{ink}}}] at ({x + w / 2},"
                rf"{y + h / 2 + fonts['sub'] * 0.55}) "
                rf"{{\fontsize{{{fonts['node']}}}{{{fonts['node'] * 1.2}}}\selectfont"
                rf"\bfseries {_tex_escape(node['label'])}}};"
            )
            lines.append(
                rf"\node[anchor=center,text={{{ink}}}] at ({x + w / 2},"
                rf"{y + h / 2 - fonts['node'] * 0.62}) "
                rf"{{\fontsize{{{fonts['sub']}}}{{{fonts['sub'] * 1.2}}}\selectfont "
                rf"{_tex_escape(node['sublabel'])}}};"
            )
        else:
            lines.append(
                rf"\node[anchor=center,text={{{ink}}}] at ({x + w / 2},{y + h / 2}) "
                rf"{{\fontsize{{{fonts['node']}}}{{{fonts['node'] * 1.2}}}\selectfont"
                rf"\bfseries {_tex_escape(node['label'])}}};"
            )
    if geometry.get("title"):
        lines.append(
            rf"\node[anchor=south] at ({geometry['canvas'][0] / 2},{geometry['title_y']}) "
            rf"{{\fontsize{{{fonts['title']}}}{{{fonts['title'] * 1.2}}}\selectfont"
            rf"\bfseries {_tex_escape(geometry['title'])}}};"
        )
    if geometry.get("legend") or geometry.get("edge_legend"):
        entries = geometry.get("legend") or []
        edge_entries = geometry.get("edge_legend") or []
        swatch = LEGEND_SWATCH
        gap = LEGEND_ENTRY_GAP
        widths = [
            swatch + 7 + len(e["role"]) * fonts["legend"] * LEGEND_TEXT_RATIO
            for e in entries
        ]
        edge_widths = [
            LEGEND_LINE_W + 6 + len(e["role"]) * fonts["legend"] * LEGEND_TEXT_RATIO
            for e in edge_entries
        ]
        total = sum(widths) + sum(edge_widths) + gap * (
            len(entries) + len(edge_entries) - 1
        )
        cursor = (geometry["canvas"][0] - total) / 2.0
        base = geometry["legend_y"]
        for entry, width in zip(entries, widths):
            lines.append(
                rf"\draw[fill={{{_tikz_color(entry['fill'])}}},"
                rf"draw={{{_tikz_color(entry['stroke'])}}},line width=0.8pt] "
                rf"({cursor},{base}) rectangle ({cursor + swatch},{base + swatch});"
            )
            lines.append(
                rf"\node[anchor=west,text=black!80] at ({cursor + swatch + 7},"
                rf"{base + swatch / 2}) {{\fontsize{{{fonts['legend']}}}"
                rf"{{{fonts['legend'] * 1.2}}}\selectfont {_tex_escape(entry['role'])}}};"
            )
            cursor += width + gap
        for entry, width in zip(edge_entries, edge_widths):
            lines.append(
                rf"\draw[draw={{{_tikz_color(entry['color'])}}},line width=2pt] "
                rf"({cursor},{base + swatch / 2}) -- "
                rf"({cursor + LEGEND_LINE_W},{base + swatch / 2});"
            )
            lines.append(
                rf"\node[anchor=west,text=black!80] at ({cursor + LEGEND_LINE_W + 6},"
                rf"{base + swatch / 2}) {{\fontsize{{{fonts['legend']}}}"
                rf"{{{fonts['legend'] * 1.2}}}\selectfont {_tex_escape(entry['role'])}}};"
            )
            cursor += width + gap
    for offset, note in enumerate(reversed(geometry.get("notes") or [])):
        lines.append(
            rf"\node[anchor=south,text=black!70] at ({geometry['canvas'][0] / 2},"
            rf"{geometry['notes_y'] + offset * fonts['note'] * 1.7}) "
            rf"{{\fontsize{{{fonts['note']}}}{{{fonts['note'] * 1.2}}}\selectfont "
            rf"{_tex_escape(note)}}};"
        )
    # 画布靠一个不可见矩形钉住（standalone 会按内容裁剪，留白才稳定）。
    lines.insert(
        0,
        rf"\path (0,0) rectangle ({geometry['canvas'][0]},{geometry['canvas'][1]});",
    )
    return "\n".join(lines)


def _tikz_color(hex_color: str) -> str:
    return "HTML_" + hex_color.lstrip("#").upper()


def _tikz_color_defs(geometry: dict[str, Any]) -> str:
    """把**几何里真会被画出来的**每一个颜色登记给 xcolor。

    原来这里按角色色**重新推导**底/边/字三个颜色 —— 一加 emphasis（三档各有
    自己的亮度）就漏，xcolor 直接 `Undefined color`、整篇编译失败。
    注册的必须是「真会画的那些」，不是「我以为会画的那些」：几何是真相源，
    这里只负责把它扫一遍。
    """

    seen: dict[str, str] = {}

    def register(*colours: Any) -> None:
        for colour in colours:
            if isinstance(colour, str) and colour.startswith("#"):
                seen[colour] = _tikz_color(colour)

    for node in (geometry.get("nodes") or {}).values():
        register(node.get("color"), node.get("fill"), node.get("stroke"), node.get("ink"))
    for entry in (geometry.get("legend") or []) + (geometry.get("edge_legend") or []):
        register(entry.get("color"), entry.get("fill"), entry.get("stroke"))
    for edge in geometry.get("edges") or []:
        register(edge.get("color"))
    return "\n".join(
        rf"\definecolor{{{name}}}{{HTML}}{{{color.lstrip('#').upper()}}}"
        for color, name in sorted(seen.items())
    )


def _visible_strings(geometry: dict[str, Any]) -> list[str]:
    """图上真会被排出来的每一段文字。缺字判据必须落在这上面。"""

    out = [geometry.get("title") or ""]
    out += [str(note) for note in geometry.get("notes") or []]
    # 这两项是 {"text":…, "xy":…} 的字典，要取 text —— 原先直接 str(item)，
    # 比对的是 "{'text': '每台服务器：…', 'xy': [119.0, 223.6]}" 这一整串。
    # 缺字判据从写下那天起就一直在核错的字符串（大括号引号都是 ASCII，所以它
    # 一直「通过」），而出门检查一上线就把这条报了出来 —— 新判据抓到的第一件事，
    # 是它依赖的老判据的 bug（2026-09-17）。
    out += [str(item.get("text") or "") for item in geometry.get("spec_items") or []]
    out += [str(item.get("text") or "") for item in geometry.get("block_notes") or []]
    for panel in geometry.get("panels") or []:
        out.append(panel.get("title") or "")
    for node in (geometry.get("nodes") or {}).values():
        out += [node.get("label") or "", node.get("sublabel") or ""]
    for group in (geometry.get("groups") or {}).values():
        out.append(group.get("label") or "")
    for edge in geometry.get("edges") or []:
        out.append(edge.get("label") or "")
    for annot in geometry.get("annotations") or []:
        out.append(annot.get("text") or "")
    for entry in geometry.get("legend") or []:
        out.append(entry.get("role") or "")
    for entry in geometry.get("edge_legend") or []:
        out.append(entry.get("role") or "")
    return [text for text in out if text]


def _measurable_items(geometry: dict[str, Any]) -> list[tuple[str, float]]:
    """图上每一段文字 **连同它将被排出来的字号** —— 测量道次要量的就是这批。

    与 `_visible_strings` 逐项对应：那边回答「排不排得出来」，这边回答「排出来
    有多宽」。两份必须一起长 —— 只加进一边，就又出现一段没人量的文字。
    """

    fonts = geometry.get("fonts") or {}

    def size(key: str, fallback: float) -> float:
        return float(fonts.get(key, fallback))

    out: list[tuple[str, float]] = []
    if geometry.get("title"):
        out.append((geometry["title"], size("title", TITLE_FS)))
    out += [(str(note), size("note", NOTE_FS)) for note in geometry.get("notes") or []]
    out += [
        (str(item.get("text") or ""), size("spec", SPEC_FS))
        for item in geometry.get("spec_items") or []
    ]
    out += [
        (str(item.get("text") or ""), size("note", NOTE_FS))
        for item in geometry.get("block_notes") or []
    ]
    for panel in geometry.get("panels") or []:
        out.append((panel.get("title") or "", size("panel", PANEL_TITLE_FS)))
    for node in (geometry.get("nodes") or {}).values():
        label_fs = CHIP_FS if node.get("shape") == "chip" else size("node", NODE_FS)
        out.append((node.get("label") or "", label_fs))
        if node.get("sublabel"):
            out.append((node["sublabel"], size("sub", SUB_FS)))
    for group in (geometry.get("groups") or {}).values():
        out.append((group.get("label") or "", size("group", GROUP_LABEL_FS)))
    for edge in geometry.get("edges") or []:
        if edge.get("label"):
            out.append((edge["label"], size("edge", EDGE_LABEL_FS)))
    for annot in geometry.get("annotations") or []:
        out.append((annot.get("text") or "", ANNOT_FS))
    for key in ("legend", "edge_legend"):
        for entry in geometry.get(key) or []:
            out.append((entry.get("role") or "", size("legend", LEGEND_FS)))
    return [(text, font) for text, font in out if text and str(text).strip()]


def _note_missing_glyphs(geometry: dict[str, Any], backend: str) -> None:
    """字体画不出的字会**静默消失** —— 合同写了、图上没有、没人报。

    实测：合同标题「① 单台服务器内部拓扑」，TikZ 出的图上只剩后半截，因为
    衬线正文字体没有 ① 的字形（2026-09-17）。这正是这套图合同要拦的那类事，
    所以它得是一条机械判据，而不是靠人看出来。

    我们自己的分栏编号已经改成画圆圈+数字、不依赖字体；这条扫的是**作者写
    进合同的文字**。
    """

    try:
        from .fonts import cjk_families, missing_glyphs, sans_stack

        stack = tuple(cjk_families()) if backend == "tikz" else tuple(sans_stack())
        missing = missing_glyphs("".join(_visible_strings(geometry)), stack)
    except Exception as exc:  # noqa: BLE001
        # 扫不动不等于没问题，但更不该因此画不出图 —— 如实记一条。
        geometry.setdefault("layout_findings", []).append(
            {
                "collector": "OB-LAYOUT",
                "message": f"字形覆盖没扫成（{type(exc).__name__}: {exc}）；"
                "图上若有缺字不会被发现。",
            }
        )
        return
    if missing:
        geometry.setdefault("layout_findings", []).append(
            {
                "collector": "OB-LAYOUT",
                "message": (
                    f"这些字符本机字体栈画不出来，图上会**静默消失**："
                    f"{''.join(missing)}（后端 {backend}，字体栈 "
                    f"{list(stack)[:3]}）。换个写法，或在本机装一款带这些字形的字体。"
                ),
                "missing_glyphs": missing,
            }
        )


def layout_for_backend(
    contract: dict[str, Any], backend: str
) -> dict[str, Any]:
    """布局两道次：先估一版看这张图要排哪些字、多大字号，量完真宽再摆一次。

    为什么不能一道次：要量什么字、什么字号，得先摆过一次才知道（字号在几何的
    `fonts` 里，分栏与 chip 还会换号）。两道次的代价是多跑一次纯布局（零点几
    秒），换来的是几何里的每个盒子都真装得下自己的字。

    量不到就如实退回估算，并把原因写进 `geometry["text_metrics"]` —— 下游判据
    据此知道自己校验的是实测还是估算。**缺席不许长得像测过。**
    """

    from .text_metrics import measure_with_tex, measured

    # 第一道次只为枚举「要排哪些字、多大字号」，它的建议没人读 —— 重排探针
    # 有 6 秒预算，跑两遍就是白烧 6 秒（22 节点的图上实测 14.4s → 8.4s）。
    geometry = layout_contract({**contract, "_no_reorder_probe": True})
    if backend != "tikz":
        # matplotlib 侧的真相源是 matplotlib 自己的字体度量，不是 TeX 的；
        # 拿 TeX 量出来的数去摆 matplotlib 的图，是换了一个错的真相源。
        geometry["text_metrics"] = {
            "source": f"estimated ({backend} backend is not measured)"
        }
        return geometry

    preamble = "\n".join(
        [
            r"\usepackage{fontspec}",
            _cjk_preamble() if _needs_cjk(geometry) else "",
        ]
    )
    table, why = measure_with_tex(
        _measurable_items(geometry), preamble, escape=_tex_escape
    )
    if table is None:
        geometry["text_metrics"] = {"source": why}
        geometry["layout_findings"] = _collapse_repeated_findings(
            geometry.get("layout_findings") or []
        )
        return geometry
    with measured(table):
        geometry = layout_contract(contract)
    geometry["text_metrics"] = {"source": why, "strings": len(table)}
    # declare 和 render 都从这里拿几何 —— 并在这个共同出口，两条通道看到的
    # 是同一份判据。
    geometry["layout_findings"] = _collapse_repeated_findings(
        geometry.get("layout_findings") or []
    )
    return geometry


def _collapse_repeated_findings(
    findings: list[dict[str, Any]], *, keep: int = 3
) -> list[dict[str, Any]]:
    """同一类判据重复太多次时并成一条，带计数和几个样例。

    2026-09-17：22 节点 / 65 边那张图报 30 条，其中 **28 条是同一句**
    「edge X->Y leaves its group through a shifted corridor」—— 那是密度的
    *后果*，不是 28 个各自独立的缺陷。真正致命的两条（268 个交叉、97 对叠线）
    被埋在里面。判据现在在声明那一刻就交出去，噪声比直接决定作者看不看得见
    要改的是什么。

    并的是**同一模板**（把 id 和数字抹掉之后一样的句子），不是同一个词 ——
    单独出现的判据一条都不动。
    """

    import re as _re
    from collections import Counter as _Counter

    def signature(message: str) -> str:
        return _re.sub(r"\d+", "#", _re.sub(r"\b[\w.]+->[\w.]+\b", "<edge>", message))

    counts = _Counter(signature(item.get("message", "")) for item in findings)
    out: list[dict[str, Any]] = []
    spent: set[str] = set()
    for item in findings:
        sig = signature(item.get("message", ""))
        if counts[sig] <= keep:
            out.append(item)
            continue
        if sig in spent:
            continue
        spent.add(sig)
        same = [f for f in findings if signature(f.get("message", "")) == sig]
        out.append(
            {
                **same[0],
                "message": (
                    f"{len(same)} edges hit the same thing — "
                    + same[0].get("message", "")
                    + ". Examples: "
                    + "; ".join(f.get("message", "")[:70] for f in same[1:3])
                    + ". One cause, not "
                    f"{len(same)} separate defects: fix the structure, not the lines."
                ),
                "repeats": len(same),
            }
        )
    return out


TIKZ_WORK_DIR = "_contract_tikz"


def tikz_log_tail(work: Path, limit: int = 4000) -> str:
    log = work / "figure.log"
    return log.read_text(encoding="utf-8", errors="replace")[-limit:] if log.exists() else ""


def finish_tikz_outputs(work: Path, base: Path) -> tuple[list[str], str | None]:
    """TeX 编完之后：按 manifest 把 figure.pdf 交付到声明的输出文件。

    返回 (写出的相对路径, PNG 没产出时的原因)。光栅化在**框架进程**里用 pypdfium2
    做，沙箱不再需要装它（那条「venv 没有 pip 装不上」的老坑随之消失）。缺席是
    可读事实：PNG 没出来就把原因交回去，由 render_figure 如实记 finding。
    """

    manifest = json.loads((work / "manifest.json").read_text(encoding="utf-8"))
    pdf = work / "figure.pdf"
    written: list[str] = []
    png_reason: str | None = None
    for rel in manifest.get("outputs") or []:
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        suffix = target.suffix.lower()
        if suffix == ".pdf":
            target.write_bytes(pdf.read_bytes())
            written.append(rel)
        elif suffix == ".png":
            try:
                import pypdfium2

                page = pypdfium2.PdfDocument(str(pdf))[0]
                page.render(scale=float(manifest.get("dpi") or 300) / 72.0).to_pil().save(str(target))
                written.append(rel)
            except Exception as exc:  # noqa: BLE001
                png_reason = f"no PDF rasterizer in the framework process ({type(exc).__name__}: {exc})"
    return written, png_reason


def _widen_for_titles(laid: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """块的自然宽度不得窄于它自己的栏标题。

    2026-09-18 实测：六拓扑图里 ④⑥ 两栏内容窄、标题长，标题直接被截在栏外
    （「2 PCIe switch，」后面没了）。栏宽过去只由内容决定，从没和标题比过。
    """

    for item in laid:
        title = str(item["block"].get("title") or "").strip()
        if not title:
            continue
        floor = _text_width(title, PANEL_TITLE_FS) + PANEL_TITLE_LEAD
        w, h = item["size"]
        item["size"] = (max(w, floor), h)
    return laid


def compile_render_script(
    contract: dict[str, Any],
    *,
    outputs: list[str],
    backend: str = "matplotlib",
    dpi: int = 300,
) -> tuple[str, dict[str, Any]]:
    """合同 → (框架生成的渲染脚本, 几何)。

    脚本被 render_figure 冻结、hash、replay —— 与 agent 手写代码走**同一条**
    出处绑定链，不新增执行器。
    """

    if backend not in BACKENDS:
        raise ValueError(f"backend must be one of {list(BACKENDS)}; got {backend!r}")
    geometry = layout_for_backend(contract, backend)
    _note_missing_glyphs(geometry, backend)
    geometry_json = json.dumps(geometry, ensure_ascii=False, sort_keys=True)
    outputs_json = json.dumps(list(outputs), ensure_ascii=False)
    if backend == "matplotlib":
        from .figure_helpers import sans_font_stack

        script = _MPL_TEMPLATE.format(
            geometry=geometry_json,
            outputs=outputs_json,
            dpi=int(dpi),
            swatch=LEGEND_SWATCH,
            chip_fs=CHIP_FS,
            badge_r=BADGE_R,
            legend_gap=LEGEND_ENTRY_GAP,
            legend_ratio=LEGEND_TEXT_RATIO,
            legend_line=LEGEND_LINE_W,
            annot_fs=ANNOT_FS,
            font_stack=json.dumps(sans_font_stack(), ensure_ascii=False),
        )
    else:
        cjk = _cjk_preamble() if _needs_cjk(geometry) else ""
        doc = _TIKZ_DOC % {
            "cjk": _tikz_color_defs(geometry) + ("\n" + cjk if cjk else ""),
            "body": _tikz_body(geometry),
        }
        script = _TIKZ_TEMPLATE.format(
            tex=doc, outputs=outputs_json, dpi=int(dpi)
        )
    return script, geometry


def layout_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """合同 → 几何。确定性；同一份合同永远同一张图。

    主路径是**两层布局**：单元内部交给 Graphviz `dot`（分层、交叉最小化、簇
    —— 我们做不好的部分），单元之间的摆放由我们 pack（作者在 bands 里显式
    声明过，是确定性的）。没有 `dot` 时回落自研引擎，并在 findings 里说出来。
    """

    from . import dot_layout

    blocks = contract.get("blocks") or []
    paged = len(blocks) > 1 or any(
        b["kind"] != "diagram" or b.get("title") for b in blocks
    )
    if paged and dot_layout.dot_available():
        try:
            return _with_readability_findings(_layout_page(contract), contract)
        except Exception as exc:  # noqa: BLE001
            fallback = _layout_builtin(contract)
            fallback["layout_engine"] = "builtin-fallback"
            fallback.setdefault("layout_findings", []).append(
                {
                    "collector": "OB-LAYOUT",
                    "message": (
                        "the page layout failed and the built-in fallback engine was "
                        f"used ({type(exc).__name__}: {exc})"
                    )[:300],
                }
            )
            return _with_readability_findings(fallback, contract)

    if dot_layout.dot_available():
        try:
            return _with_readability_findings(_layout_two_layer(contract), contract)
        except Exception as exc:  # noqa: BLE001 —— 布局器坏了不许拖死出图
            fallback = _layout_builtin(contract)
            fallback["layout_engine"] = "builtin-fallback"
            fallback.setdefault("layout_findings", []).append(
                {
                    "collector": "OB-LAYOUT",
                    "message": (
                        "the Graphviz layout failed and the built-in fallback engine was "
                        f"used; readability will be worse ({type(exc).__name__}: {exc})"
                    )[:300],
                }
            )
            return _with_readability_findings(fallback, contract)

    geometry = _layout_builtin(contract)
    geometry["layout_engine"] = "builtin-fallback"
    geometry.setdefault("layout_findings", []).append(
        {
            "collector": "OB-LAYOUT",
            "message": (
                "Graphviz `dot` is not installed on the minting host, so the built-in "
                "fallback layout engine was used; edge crossings and whitespace will be "
                "worse than the main path"
            ),
        }
    )
    return _with_readability_findings(geometry, contract)


def _cheaper_order(
    contract: dict[str, Any], baseline: int
) -> list[tuple[str, str, str, int]] | None:
    """把相邻两个对调，交叉会不会降下来 —— **真摆一遍量出来**，而且是贪心连续做。

    两处可以对调：带子里的条目顺序，以及**组内每一 rank 的顺序**。
    2026-09-17 实测：只试带子时，密集图那 17 个交叉一条建议都给不出 —— 因为
    12 个交叉出在组内同一行（十个服务一行、七条内部调用互相拱过）。加上组内
    rank 之后，三次对调把 17 降到 4。

    顺序可能是有语义的（「CPU 0 在左、CPU 1 在右」），所以这里只给建议、不自己
    动手 —— 与 iter38 想清楚的那条一致。
    """

    def _containers(current: dict[str, Any]):
        """要试的「顺序」都在哪儿。

        **版面合同必须改 blocks 里的那一份** —— 顶层 groups/bands 是合并出来的
        副本，渲染根本不读它（读的是 blocks）。第一版只改顶层，于是探针在所有
        多栏图上什么也没做，而我还以为「没有更优解」（2026-09-17 —— 又一次
        「改了却什么也没发生」，这次是我自己刚写的代码）。
        """

        # **两处都要试。** 渲染读哪一份取决于这张图是不是版面：多栏走 blocks，
        # 单个无标题的块走顶层 bands。只改 blocks 时，玩具图上手动对调明明能把
        # 交叉从 2 降到 0，探针却一无所获（2026-09-17）。与其在这里复刻
        # layout_contract 的分支判断（复刻就会分叉），不如两份都生成变体 ——
        # 多试几次的代价由时间预算兜着。
        yield ("root", None, current)
        for bi, block in enumerate(current.get("blocks") or []):
            if block.get("kind") == "diagram":
                yield ("blocks", bi, block)

    def _rebuilt(current, kind, bi, patched):
        if kind == "root":
            return {**current, **patched}
        blocks = list(current["blocks"])
        blocks[bi] = {**blocks[bi], **patched}
        return {**current, "blocks": blocks}

    def _orderings(row: list[str]) -> list[list[str]]:
        """这一行要试哪些顺序。

        短行**穷举全排列**：相邻对调是贪心爬山，出不了局部最优。2026-09-17 实测
        那张三栏图，② 栏 6 节点 7 边、交叉 4，而它的**四种**单步对调全是上坡
        （6 / 4 / 4 / 5）—— 探针如实返回「没有更优解」，可手动换成
        [支付,订单,搜索] / [风控,优惠券,库存] 就是 **0**。最优解要走三步上坡，
        贪心永远走不到。
        """

        if len(row) <= REORDER_PERMUTE_MAX:
            return [list(p) for p in itertools.permutations(row) if list(p) != row]
        out = []
        for index in range(len(row) - 1):
            swapped = list(row)
            swapped[index], swapped[index + 1] = swapped[index + 1], swapped[index]
            out.append(swapped)
        return out

    def _describe(before: list[str], after: list[str]) -> tuple[str, list[str]]:
        """(给人看的一句话, 正好对调的那一对 —— 不是对调就空)。

        全排列表达不成一对 swap，所以机读字段以 order 为准；恰好是两个元素互换时
        额外给出 swap，老的消费方不必改。
        """

        moved = [x for x, y in zip(before, after) if x != y]
        if len(moved) == 2:
            return f"swap {moved[0]!r} and {moved[1]!r}", moved
        return "reorder to [" + ", ".join(after) + "]", []

    def _variants(current: dict[str, Any]):
        for kind, bi, holder in _containers(current):
            for row, band in enumerate(holder["bands"]):
                names = [entry.partition(":")[2] for entry in band]
                prefix = {entry.partition(":")[2]: entry for entry in band}
                for order in _orderings(names):
                    bands = [list(b) for b in holder["bands"]]
                    bands[row] = [prefix[name] for name in order]
                    yield (
                        _rebuilt(current, kind, bi, {"bands": bands}),
                        (f"bands[{row}]", *_describe(names, order), list(order)),
                        bi if kind == "blocks" else None,
                    )
            for gi, group in enumerate(holder["groups"]):
                for ri, rank in enumerate(group["ranks"]):
                    for order in _orderings(list(rank)):
                        groups = [
                            {**g, "ranks": [list(r) for r in g["ranks"]]}
                            for g in holder["groups"]
                        ]
                        groups[gi]["ranks"][ri] = order
                        yield (
                            _rebuilt(current, kind, bi, {"groups": groups}),
                            (f"{group['id']} 第 {ri + 1} 行",
                             *_describe(list(rank), order), list(order)),
                            bi if kind == "blocks" else None,
                        )

    def _cheap_score(probe: dict[str, Any], bi: int | None) -> float:
        """筛选用**只摆被改的那一块**，整图一次 0.47s、单块毫秒级。

        2026-09-17 实测那张三栏图：交叉按 block 可加（① 栏 5 + ② 栏 4 = 整图 9），
        而块内换顺序只改得动本块。所以排序可以用便宜的那一份 —— **但报出去的
        数字仍旧在整图上量**，选中的候选要过一遍真布局，整图没真降就不采纳。
        """

        if bi is None:
            return float("inf")
        blocks = probe.get("blocks") or []
        if not (0 <= bi < len(blocks)):
            return float("inf")
        try:
            return float(
                layout_contract(
                    {**probe, "blocks": [blocks[bi]], "assertions": [],
                     "_no_reorder_probe": True}
                )["layout_metrics"]["edge_crossings"]
            )
        except Exception:  # noqa: BLE001
            return float("inf")

    steps: list[tuple[str, str, list[str], list[str], int]] = []
    current, best = contract, baseline
    # **按时间封顶，不按次数。** 按次数封顶时，22 节点的图一次布局就要半秒，
    # 60 次仍然是 33 秒；而小图跑满 60 次也不到一秒。判据花的每一秒都记在作者
    # 账上，所以限的应该是「最多花多久」。
    deadline = time.monotonic() + REORDER_TIME_BUDGET_S
    for _round in range(REORDER_MAX_ROUNDS):
        improved = None
        # 按块分桶，**便宜的块先处理，而且当场核实**。两个坑都踩过（2026-09-17，
        # 都是我自己刚写的那一版）：
        #   ① 不分桶：光给 ① 栏一百多个候选打分就用光 6 秒，② 栏一个都轮不上 ——
        #      和原先那个固定顺序的毛病一模一样，只是挪了个地方。
        #   ② 先全局排序再统一核实：排序把预算吃光，**核实一次都没跑到**。
        # 由可加性（① 栏 5 + ② 栏 4 = 整图 9）得：块内换顺序只改得动本块，所以
        # 一个候选只要它自己那块没变好，就不可能让整图变好 —— 拿便宜的那一份筛掉。
        buckets: dict[Any, list[tuple[dict[str, Any], tuple[str, str]]]] = {}
        for probe, label, bi in _variants(current):
            buckets.setdefault(bi, []).append((probe, label))
        cost: dict[Any, float] = {}
        for bi, items in buckets.items():
            mark = time.monotonic()
            _cheap_score(items[0][0], bi)
            cost[bi] = time.monotonic() - mark
        for bi in sorted(buckets, key=lambda key: (key is None, cost.get(key, 0.0))):
            if time.monotonic() > deadline or improved is not None:
                break
            here = _cheap_score(current, bi)
            shortlist = []
            if bi is None:
                # 顶层 bands 这一桶**算不出便宜分数**（它不是某一个块），所以不筛，
                # 直接拿整图核实。平铺合同（单个无标题的块）渲染读的正是这一份，
                # 把它筛掉等于在所有平铺图上把探针关掉 —— 2026-09-17 一条老测试
                # 当场转红，它钉的就是这种玩具图。
                shortlist = [(0.0, probe, label) for probe, label in buckets[bi]]
            else:
                for probe, label in buckets[bi]:
                    if time.monotonic() > deadline:
                        break
                    score = _cheap_score(probe, bi)
                    if score < here:
                        shortlist.append((score, probe, label))
                shortlist.sort(key=lambda item: item[0])
            # 报出去的数字必须量在**真会画出来的那张图**上 —— 便宜的那一份只筛选。
            for _score, probe, label in shortlist[: (None if bi is None else 4)]:
                if time.monotonic() > deadline:
                    break
                try:
                    crossings = layout_contract(
                        {**probe, "_no_reorder_probe": True}
                    )["layout_metrics"]["edge_crossings"]
                except Exception:  # noqa: BLE001
                    continue
                if crossings < best:
                    improved = (crossings, probe, label)
                    break
        if improved is None:
            break
        best, current, label = improved[0], improved[1], improved[2]
        steps.append((*label, best))
    return steps or None


#: 图 / 地的三档亮度。**只动亮度和线宽，不动色相** —— 色相已经被「类别」占着，
#: 拿它兼职表达「重要性」就是把两件事混成一件（Bertin：色相是名义变量，
#: 没有次序；亮度才有）。
#:   primary  底更实、边更粗、字最黑 —— 前进
#:   默认      现状
#:   muted    底近白、边细而淡、字发灰 —— 后退成背景
EMPHASIS_PAINT: dict[str, tuple[float, float, float, float]] = {
    # (底色亮度, 描边亮度上限, 文字亮度上限, 线宽倍数)
    "primary": (0.78, 0.22, 0.06, 1.7),
    "": (0.855, 0.26, 0.10, 1.0),
    # muted 的两个数都被**既有的可读性下限**钉死，不是挑出来的：
    #   字 ≤0.171 —— 底 0.945 上要清 4.5:1（WCAG AA）
    #   边 ≤0.43  —— 盒子边界对底要清 2:1，再淡盒子就糊没了
    # 所以 muted 的后退靠底色、线宽和这两条线**以内**的余量，
    # **不靠把字弄看不清** —— 灰字压白底是常见做法，也是常见的错做法。
    "muted": (0.945, 0.43, 0.16, 0.7),
}


def _derive_paint(
    geometry: dict[str, Any], contract: dict[str, Any] | None = None
) -> dict[str, Any]:
    """给每个角色色派生出**要画的那三个颜色**：底、边、字。

    放在这里而不是各个渲染器里，有两个理由：
    1. 赋色点有六处（三个引擎 × 节点/图例），逐个改就会漏，而新引擎默认漏；
    2. 几何是「要画什么」的真相源 —— 两个后端读同一份，就不会长得不一样。
    """

    # 强调档从**合同**里取，不从几何里取：几何的节点在六个引擎里各自逐字段
    # 构造，新字段默认漏（颜色那次已经栽过一回）。合同是这件事的真相源。
    declared = {
        str(node.get("id")): (node.get("emphasis") or "")
        for node in ((contract or {}).get("nodes") or [])
    }
    for node_id, node in (geometry.get("nodes") or {}).items():
        base = node.get("color")
        if not base:
            continue
        emphasis = declared.get(str(node_id), "")
        node["emphasis"] = emphasis or None
        fill_l, stroke_l, ink_l, weight = EMPHASIS_PAINT[emphasis]
        # primary / 默认：**封顶**（只压不提，深色相原样保留）。
        # muted：**定标**（必须真的提亮才后退 —— 用封顶时深色相低于上限，
        # 三档描边会长得一模一样，muted 等于没写）。
        level = _at_luma if emphasis == "muted" else _shade
        node["fill"] = _at_luma(base, fill_l)
        node["stroke"] = level(base, stroke_l)
        node["ink"] = level(base, ink_l)
        node["line_weight"] = weight
    for entry in (geometry.get("legend") or []):
        base = entry.get("color")
        if base:
            entry["fill"] = _tint(base)
            entry["stroke"] = _shade(base, STROKE_LUMA_CAP)
    return geometry


def _with_readability_findings(
    geometry: dict[str, Any], contract: dict[str, Any]
) -> dict[str, Any]:
    """可读性判据 —— 属于**布局这件事**，不属于某一个引擎。

    合同保证结构对，不保证读得了。2026-09-16 实测：结构全对的图仍然读不了，
    而记录里只有一条极粗的判据（长宽比），于是「读不了」几乎不留痕。这些量
    全都可计算（见 layout_quality），所以由框架算出来说；改不改是作者的编排
    判断，因此一律是 finding 不是拒绝。
    """

    from .layout_quality import measure

    _derive_paint(geometry, contract)
    metrics = measure(geometry, contract)
    geometry["layout_metrics"] = metrics
    # 调用方的语言政策要走到画图那一步：detail_of 的「详见 ①」徽章是框架写的
    # 字，不是作者的 —— 英文图上印中文，判据一个都不会响。
    geometry["text_language"] = contract.get("text_language")
    findings = geometry.setdefault("layout_findings", [])

    # ── 终尺寸可读性 ──────────────────────────────────────────────────────
    # 画布的 pt 数**就是**物理英寸数。这张图被复现到目标宽度时，最小的一段字
    # 有多大？46 轮里没有任何东西问过这件事，而校准集在期刊单栏下是 0.8mm。
    from .figure_contract import MEDIA, fit_scale, medium_of

    medium = medium_of(contract)
    spec = MEDIA[medium]
    canvas = geometry.get("canvas") or [1.0, 1.0]
    canvas_w_pt, canvas_h_pt = (canvas[0] or 1.0), (canvas[1] or 1.0)
    canvas_w_mm = canvas_w_pt * 25.4 / 72.0
    canvas_h_mm = canvas_h_pt * 25.4 / 72.0
    scale = fit_scale(canvas_w_mm, canvas_h_mm, medium)
    # 判的是**节点标签**那一档，不是最细的那一档：副标题、边标注、引出注记是
    # 辅助文字，期刊允许它们更小；读者必须读得了的是「这个盒子叫什么」。
    # 拿最细档去判会让判据在半数图上响，而它响的是一件本来就允许的事。
    label_pt = geometry["fonts"]["node"]
    final_pt = label_pt * scale
    metrics["medium"] = medium
    metrics["final_width_mm"] = round(canvas_w_mm * scale, 1)
    metrics["final_label_pt"] = round(final_pt, 2)
    # 「最小的字」只数**图上真有的那一档**：没有副标题的图不该被副标题的字号
    # 判；而有副标题的图，副标题就是读者要读的字（iter11 的 ×8 / ×16 / ×32）。
    fit = print_fit(geometry, contract)
    metrics["final_smallest_pt"] = fit["final_smallest_pt"]
    metrics["print_fit"] = fit
    # **终尺寸可读性只进 metrics，不进 findings。** 一条判据都不报。
    #
    # 这个结论是被实测逼出来的，两步：
    # ① iter48：由 purpose 推出 slide 之后判据响了两次，agent 声明三版、画布
    #    777pt → 777pt → 777pt 一次没变。它做不到 —— 「窄 42%」对一张 19 节点的
    #    拓扑图 = 砍一半内容或拆两张图，而需求要的是一张图。
    # ② 我当时只改了一半：「作者**自己声明**了 medium 才拿来判他」。
    #    iter50 实测这半步的后果 ——
    #        第1版 medium=slide（agent 自己写的真话）→ 判据开口
    #        第2版 medium=None  ← **它把声明删掉，让判据闭嘴**
    #        第3版 medium=None  ← 和第2版一字不差，白烧一轮
    #    多花 3 轮 / 111k token，买到的是一句真话被删掉。
    #
    # **给一个自愿的声明挂惩罚，就是教它别声明。** 这与 `unexpressed` 的设计
    # 正好相反 —— 那边刻意做成「写了只披露、不罚」，所以模型敢写。
    #
    # 「这些内容放不进这个版心」是关于**需求**的事实，不是图的缺陷；能解它的是
    # 「要不要拆成两张图」，而那不在作者的权限里。事实照进 layout_metrics，
    # referee 和用户都读得到 —— 判据不该逼一个人去做他做不到的事。
    ratio = metrics.get("aspect_ratio") or 1.0
    if ratio > MAX_ASPECT or ratio < 1.0 / MAX_ASPECT:
        findings.append(
            {
                "collector": "OB-LAYOUT",
                "message": (
                    f"the canvas is {ratio:.1f}:1 — too elongated to read at any print "
                    "size. Re-compose: split one wide band into several bands, or show "
                    "one unit in detail and the rest compactly"
                ),
                "aspect_ratio": ratio,
            }
        )
    if metrics["edge_crossings"] > MAX_CROSSINGS_PER_EDGE * max(1, metrics["edge_count"]):
        findings.append(
            {
                "collector": "OB-LAYOUT",
                "message": (
                    f"{metrics['edge_crossings']} edge crossings over "
                    f"{metrics['edge_count']} edges — readers cannot follow which line "
                    "goes where. Three ways out, strongest first (all measured on "
                    "real figures, 2026-09-17): (1) **collapse the fans**: an edge "
                    "endpoint may name a whole group as `group:<id>`, the same lexicon "
                    "bands use. 'every service reports to Prometheus' is ONE edge, not "
                    "ten; 'the gateway calls the business layer' is one edge, not ten. "
                    "Measured 267->30 with every node still drawn — see dense_example. "
                    "(2) **fold the busiest layer into its own block**: one node "
                    "carrying detail_of=<the group in the new block>, the detail block "
                    "expressing its outward links as annotations. Measured 290->133 and "
                    "268->8, at the cost of a second column. (3) nested groups "
                    "(groups[].parent) put related components together — but measured "
                    "268->235 and 109->109: that tidies the rows, it does not thin the "
                    "figure"
                ),
                "edge_crossings": metrics["edge_crossings"],
            }
        )
    if metrics["edges_grazing_nodes"]:
        findings.append(
            {
                "collector": "OB-LAYOUT",
                "message": (
                    f"{metrics['edges_grazing_nodes']} connection(s) run right against a "
                    "component they do not connect to; they read as if they did"
                ),
                "samples": metrics["grazing_samples"],
            }
        )
    # 装饰件跑出宿主 = **渲染被破坏**，不是编排偏好 —— 所以放在最前面。
    detached = metrics.get("detached_decorations") or []
    if detached:
        findings.append(
            {
                "collector": "OB-LAYOUT",
                "message": (
                    f"{len(detached)} decoration(s) are drawn outside the thing they "
                    "belong to (ports away from their component, or spec text outside "
                    "its panel) — this is a rendering defect, not a composition choice"
                ),
                "detached": detached[:6],
            }
        )
    # 端口已经是真正的连接点了（线落在口上），那么「画了 8 个口、只有 4 条线」
    # 就是图上一个读者看得见、合同里却没有任何位置说明的落差。实测 iter12：
    # ② 栏画 4 条上联，注记写「每台服务器 2 张 RoCE 网卡分别上联」（= 8 条），
    # 交换机声明 8 个口 —— 三处各说各的，机械判据一条都没响。
    #
    # 不是错误（画一台 48 口交换机接 4 根线很正常），所以只报不拒；要消掉它，
    # 要么把线补齐，要么写一条 neighbors 断言说清这个节点的度。
    degrees: dict[str, int] = {}
    for edge in contract["edges"]:
        weight = int(edge.get("represents") or 1)
        degrees[edge["from"]] = degrees.get(edge["from"], 0) + weight
        degrees[edge["to"]] = degrees.get(edge["to"], 0) + weight
    stated = {
        assertion.get("node")
        for assertion in contract.get("assertions") or []
        if assertion.get("kind") == "neighbors"
    }
    for node in contract["nodes"]:
        ports = int(node.get("ports") or 0)
        landed = degrees.get(node["id"], 0)
        if ports and landed > ports:
            # 反方向更严重：**一台 2 口交换机插不进 5 根线**，这是被画的东西
            # 本身不可能的事。而且端口吸附只在「口够坐」时才生效，所以此时
            # 那句「声明了端口，连线就落在端口上」也悄悄失效了 —— 图上 5 条线
            # 只有 2 条真落在口上（2026-09-17 扫词表时发现，判据原先只看
            # ports > landed 这一边）。
            findings.append(
                {
                    "collector": "OB-LAYOUT",
                    "message": (
                        f"{node['id']} declares {ports} ports but {landed} links arrive "
                        "at it — the thing being drawn cannot have more cables than "
                        "ports, and the extra lines no longer land on a port at all. "
                        "Raise the port count, or draw fewer links."
                    ),
                    "ports": ports,
                    "edges_landed": landed,
                }
            )
        elif ports > landed and node["id"] not in stated:
            findings.append(
                {
                    "collector": "OB-LAYOUT",
                    "message": (
                        f"{node['id']} draws {ports} ports but only {landed} edges land "
                        "on them — a reader sees unused ports and has to guess whether "
                        "the missing links exist. Either declare the remaining edges, or "
                        f"add a neighbors assertion on {node['id']} saying what its "
                        "degree is meant to be."
                    ),
                    "ports": ports,
                    "edges_landed": landed,
                }
            )
    # detail_of 说了「② 栏这个盒子 = ① 栏那个机箱」。那么两栏关于**这台机器
    # 对外有几条链路**的说法就得对得上 —— 而合同里过去没有位置说这句话
    # （同型根因第 7 次）。iter13 实测：① 栏画两张 NIC，注记写「每台服务器
    # 2 张网卡分别上联」，② 栏每台服务器却只接 1 条线，还把交换机端口从 8 改
    # 成 4 去迁就它 —— 朝错的方向对齐了。参考图是 8 条。
    #
    # 声明「对外几条」的地方就是 annotations：从 NIC 朝外引一句「上联 RoCE
    # 交换机」。数得出来就对账，数不出来就说清「没法核」。
    members_of = {
        group["id"]: _group_member_ids(contract, group["id"])
        for group in contract["groups"]
    }
    outward = {gid: 0 for gid in members_of}
    for annotation in contract.get("annotations") or []:
        for gid, members in members_of.items():
            if annotation.get("anchor") in members:
                outward[gid] += int(annotation.get("represents") or 1)
    for node in contract["nodes"]:
        target = node.get("detail_of")
        if not target or target not in members_of:
            continue
        landed = degrees.get(node["id"], 0)
        declared = outward.get(target, 0)
        if declared == 0:
            findings.append(
                {
                    "collector": "OB-LAYOUT",
                    "message": (
                        f"{node['id']} is drawn as the collapsed form of {target}, and "
                        f"carries {landed} link(s). Nothing in {target} says how many "
                        "links leave it, so the two panels cannot be checked against "
                        "each other — add an annotation on each interface that goes "
                        "outside (anchor=<the NIC/port node>, text=where it goes)."
                    ),
                    "detail_of": target,
                }
            )
        elif declared != landed:
            findings.append(
                {
                    "collector": "OB-LAYOUT",
                    "message": (
                        f"{target} is drawn with {declared} link(s) leaving it, but "
                        f"{node['id']} — the same unit collapsed — carries {landed}. "
                        "The two panels disagree about the same machine."
                    ),
                    "detail_of": target,
                    "declared": declared,
                    "landed": landed,
                }
            )
    # 画了框却没有名字 = 读者看见一个盒子，不知道它圈的是什么。组框是画出来的
    # （占面积、有边界），名字却是可选的 —— 这个缝里掉出来的就是「两个灰框」。
    # 2026-09-17 实测：schema example 自己把嵌套组的 label 留成 ""，agent 六轮
    # 里五轮照抄；唯一写了名字的那轮（iter22）图明显更好读。
    for group in contract["groups"]:
        if not group["label"]:
            findings.append(
                {
                    "collector": "OB-LAYOUT",
                    "message": (
                        f"group {group['id']} is drawn as a frame but carries no label — "
                        "a reader sees a box and cannot tell what it encloses. Name it, "
                        "or fold its members into the parent if the grouping carries no "
                        "meaning of its own."
                    ),
                    "group": group["id"],
                }
            )
    # 一整排都要跟同排里的某一个说话 —— 那一个不该待在这排里。
    #
    # 2026-09-17 分层架构基准实测：六个执行节点横着连同排的「模型服务」，18 个
    # 交叉 + 9 对叠线全出在这儿。试过让这些线绕到排下面走，**实测 18 → 80，更
    # 糟**（绕道正好落进下一层的总线走廊）。绕不出来是因为病根在结构：同排里
    # 横着穿过去，中间隔着谁就压谁。所以这里只说出病根和出路，不去抢救。
    unplaced = [
        f"{edge['from']}->{edge['to']}:{edge['label']}"
        for edge in geometry["edges"]
        if edge.get("label_unplaced")
    ]
    if unplaced:
        findings.append(
            {
                "collector": "OB-LAYOUT",
                "message": (
                    f"{len(unplaced)} edge label(s) had nowhere to go and are printed on "
                    f"top of something else: {unplaced[:5]}. Usually too many edges share "
                    "one stretch of the figure — give them separate rows, or move the "
                    "text into a spec/note block."
                ),
                "labels_unplaced": unplaced[:8],
            }
        )

    # 同一个名字在图例里出现两次（一个色块 + 一条线），读者无从知道它指哪个。
    # 节点的 role 说的是「这是个什么东西」（处理 / 判定点），边的 role 说的是
    # 「这条连接是什么」（数据流 / 回退）—— 两者共用一个名字时，图例上就是同名
    # 两色两义。2026-09-17 流程图基准实测：「不合格分支」既是节点角色又是边角色，
    # 而这份合同当时 0 findings，判据完全没看见。
    node_roles = {node["role"] for node in contract["nodes"] if node.get("role")}
    edge_roles = {edge["role"] for edge in contract["edges"] if edge.get("role")}
    for role in sorted(node_roles & edge_roles):
        findings.append(
            {
                "collector": "OB-LAYOUT",
                "message": (
                    f"{role!r} is used both as a node role and as an edge role, so the "
                    "legend carries that one name twice — once as a swatch, once as a "
                    "line, in two different colours. A node role says what a thing is; "
                    "an edge role says what a connection is. Rename one of them."
                ),
                "ambiguous_role": role,
            }
        )

    # 交叉还在时，**替作者算一算**：把某一行里相邻两个对调，交叉会不会降下来。
    # 这是一条被算出来的建议，不是猜的 —— 我给过一条没量过的建议（「把那个节点
    # 单独放一层」），实测把交叉从 18 弄成 164。**建议也要过度量这一关。**
    # 每试一种对调都要真摆一遍，所以只在**图已经乱了**的时候才找，并且限轮数。
    if (
        metrics.get("edge_crossings", 0) >= REORDER_MIN_CROSSINGS
        and not contract.get("_no_reorder_probe")
    ):
        steps = _cheaper_order(contract, metrics["edge_crossings"])
        if steps:
            after = steps[-1][-1]
            recipe = "; ".join(f"{how} in {w}" for w, how, _sw, _o, _n in steps)
            findings.append(
                {
                    "collector": "OB-LAYOUT",
                    "message": (
                        f"{recipe} — that takes the crossings from "
                        f"{metrics['edge_crossings']} to {after}. The order inside a row "
                        "is yours to choose; this sequence is measured, not guessed"
                    ),
                    "reorder": {
                        # order 永远给（全排列表达不成一对 swap）；正好是两个
                        # 元素对调时**额外**给 swap，老的消费方不必改。
                        "steps": [
                            {
                                "where": w,
                                "how": how,
                                "order": order,
                                "crossings_after": n,
                                **({"swap": swap} if swap else {}),
                            }
                            for w, how, swap, order, n in steps
                        ],
                        "crossings_before": metrics["edge_crossings"],
                        "crossings_after": after,
                    },
                }
            )

    overlaps = metrics.get("collinear_overlaps") or []
    if overlaps:
        findings.append(
            {
                "collector": "OB-LAYOUT",
                "message": (
                    f"{len(overlaps)} pair(s) of edges are drawn on top of each other — "
                    "the reader sees one line where there are two, so one of them is "
                    f"invisible: {overlaps[:4]}. Route them apart, or (if they really do "
                    "share a trunk) connect every source to every target so the framework "
                    "draws one bus."
                ),
                "collinear_overlaps": overlaps[:8],
            }
        )
    if metrics.get("empty_rows", 0) > MAX_EMPTY_ROWS:
        findings.append(
            {
                "collector": "OB-LAYOUT",
                "message": (
                    f"{metrics['empty_rows']} of {metrics.get('slices', 24)} horizontal "
                    "slices of the canvas are empty — whole bands of the figure carry "
                    "nothing. Usually a block is padded far taller than its content, or "
                    "two short blocks should share one blocks[].row"
                ),
                "empty_rows": metrics["empty_rows"],
            }
        )
    if metrics.get("empty_cols", 0) > MAX_EMPTY_COLS:
        findings.append(
            {
                "collector": "OB-LAYOUT",
                "message": (
                    f"{metrics['empty_cols']} of {metrics.get('slices', 24)} vertical "
                    "slices of the canvas are empty — the figure is much wider than its "
                    "content needs"
                ),
                "empty_cols": metrics["empty_cols"],
            }
        )
    spanning = metrics.get("rank_spanning_edges") or []
    if spanning:
        ratio = metrics.get("edge_length_ratio")
        findings.append(
            {
                "collector": "OB-LAYOUT",
                "message": (
                    f"{len(spanning)} connection(s) span more than one rank inside their "
                    "group, so they have to route around the rank in between"
                    + (f" (the longest edge is {ratio}× the mean)" if ratio else "")
                    + ": "
                    + "; ".join(f"{a}->{b}" for a, b, in
                                [(i['edge'][0], i['edge'][1]) for i in spanning[:4]])
                    + ". Put the two ends on adjacent ranks — e.g. split one node's "
                    "downstream into the ranks **above and below** it instead of "
                    "stacking them all on one side"
                ),
                "spanning": spanning[:6],
            }
        )
    # 「这一行其实是 N 个子系统」只在**症状出现时**才报。
    # 校准集实测：参考图把 8 张 GPU 排一行、两个 switch 各管 4 张，靠**位置邻近**
    # 表达分组（sw0 在左、GPU0–3 在左），交叉 1、绕行 2.8× —— 完全合法的设计，
    # 却被这条判据点名要求拆成嵌套子组。结构气味只有在它真的造成了交叉或绕行时
    # 才是缺陷；否则它只是另一种编排。
    # 开关从**全图比值**换成**这一行自己的结构**：各家成员连成一段 = 位置邻近
    # 已经把归属说清楚了（参考图就是这么画的），不是缺陷；交错才是。
    # 旧开关（最长边/平均边长 > 3.0）让「图里别处变好」也能把这条点着 ——
    # 2026-09-17 实测比值 2.79 → 3.17，只因为另一栏的线被拉直了。
    for split in (metrics.get("splittable_ranks") or []):
        if split.get("contiguous", False):
            continue
        classes = split["partition"]
        findings.append(
            {
                "collector": "OB-LAYOUT",
                "message": (
                    f"rank {split['rank_index']} of group {split['group']!r} is really "
                    f"{len(classes)} separate sub-systems sharing one row: "
                    + "; ".join(
                        f"{owner} owns {len(ids)}" for owner, ids in sorted(classes.items())
                    )
                    + ". Declare one nested group per sub-system (groups[].parent) so "
                    "each owner sits with the components it owns, instead of all of them "
                    "fanning across a shared row"
                ),
                "partition": classes,
            }
        )
    for duplicate in metrics["isomorphic_group_sets"]:
        findings.append(
            {
                "collector": "OB-LAYOUT",
                "message": (
                    f"groups {duplicate} are structurally identical — drawing all "
                    f"{len(duplicate)} in full adds no information while multiplying the "
                    "area. Consider showing one in detail and the rest collapsed, or "
                    f"collapsing all of them into one unit marked ×{len(duplicate)}"
                ),
                "isomorphic_groups": duplicate,
            }
        )
    return geometry


def _layout_sequence(contract: dict[str, Any]) -> dict[str, Any]:
    """时序：参与者横排一行，消息按 step 从上往下各占一行。

    没有这套摆法时，同一对参与者之间的七条消息全塌成一条线、标签叠成一团
    —— 而交叉判据说没毛病（2026-09-17 时序图基准实测）。**每条消息各占一行**
    之后，标注天然不会互相压，交叉也天然为零。
    """
    # 时序图不走 _route_edges（生命线自己排），端口没有实测位置 ——
    # 给一个空表，`_port_marks` 就退回均布。**用到就必须定义**：少了它
    # NameError 会被上层吞掉、悄悄掉回回落引擎（同一个错今天犯了三次）。
    _port_at: dict[tuple[str, str], list[float]] = {}

    from .figure_contract import edge_role_colors, role_colors

    nodes = {node["id"]: node for node in contract["nodes"]}
    roles = contract.get("_page_roles") or role_colors(contract)
    edge_colours = contract.get("_page_edge_roles") or edge_role_colors(contract)
    suppress = bool(contract.get("_suppress_chrome"))
    order = [
        ident
        for entry in contract["bands"][0]
        for prefix, _, ident in [entry.partition(":")]
        if prefix == "node"
    ]
    sizes = {nid: _node_size(nodes[nid]) for nid in order}
    head_h = max(h for _w, h in sizes.values())
    row_w = sum(w for w, _h in sizes.values()) + ENTRY_GAP * (len(order) - 1)

    messages = sorted(
        enumerate(contract["edges"]), key=lambda pair: (pair[1]["step"], pair[0])
    )
    legend_entries = [] if suppress else sorted(roles)
    legend_sources = [] if suppress else list(edge_colours)
    legend_h = (
        (LEGEND_GAP + LEGEND_SWATCH + LEGEND_FS)
        if (legend_entries or legend_sources)
        else 0.0
    )
    notes = [] if suppress else list(contract.get("notes") or [])
    notes_h = (NOTE_GAP + len(notes) * NOTE_FS * 1.7) if notes else 0.0
    has_title = bool(contract.get("title")) and not suppress
    title_h = (TITLE_FS * 1.4 + TITLE_GAP) if has_title else 0.0
    margin = INNER_MARGIN if suppress else MARGIN

    label_w = max(
        (_text_width(edge["label"], EDGE_LABEL_FS) for _i, edge in messages), default=0.0
    )
    canvas_w = max(row_w, label_w + 40.0, legend_width(legend_entries, legend_sources),
                   _text_width(contract.get("title") or "", TITLE_FS),
                   # 页脚注记也要放得下 —— 四个引擎里只有一个算了它，另外三个
                   # 比内容宽就画到画布外，而几何判据只看盒子，不会响。
                   max((_text_width(str(n), NOTE_FS) for n in notes), default=0.0),
                   ) + 2 * margin
    body_h = SEQ_HEAD_GAP + SEQ_STEP_GAP * len(messages) + SEQ_TAIL_GAP
    canvas_h = head_h + body_h + 2 * margin + title_h + legend_h + notes_h

    top = canvas_h - margin - title_h
    left = (canvas_w - row_w) / 2.0
    node_rects: dict[str, _Rect] = {}
    cursor = left
    for nid in order:
        width, height = sizes[nid]
        node_rects[nid] = _Rect(cursor, top - height, width, height)
        cursor += width + ENTRY_GAP

    lifeline_bottom = top - head_h - body_h + SEQ_TAIL_GAP
    lifelines = [
        {
            "x": round(node_rects[nid].cx, 2),
            "top": round(node_rects[nid].y, 2),
            "bottom": round(lifeline_bottom, 2),
        }
        for nid in order
    ]

    edges_out: list[dict[str, Any]] = []
    for slot, (_index, edge) in enumerate(messages):
        y = top - head_h - SEQ_HEAD_GAP - SEQ_STEP_GAP * slot
        x0 = node_rects[edge["from"]].cx
        x1 = node_rects[edge["to"]].cx
        if edge["from"] == edge["to"]:
            # 自己发给自己：向右伸出一个小回环，别缩成一个点。
            bump = SEQ_SELF_W
            points = [(x0, y), (x0 + bump, y), (x0 + bump, y - SEQ_SELF_H),
                      (x0, y - SEQ_SELF_H)]
            label_xy = (x0 + bump + 6.0, y - SEQ_SELF_H / 2.0)
        else:
            points = [(x0, y), (x1, y)]
            label_xy = ((x0 + x1) / 2.0, y + 5.0)
        edges_out.append(
            {
                "from": edge["from"],
                "to": edge["to"],
                "points": [[round(px, 2), round(py, 2)] for px, py in points],
                "label": edge["label"],
                "label_xy": [round(label_xy[0], 2), round(label_xy[1], 2)],
                "kind": edge["kind"],
                "role": edge.get("role", ""),
                "color": edge_colours.get(edge.get("role", "")),
                "bidirectional": edge["bidirectional"],
                "bundle": 1,
                "step": edge["step"],
                # 时序图里**方向是第一信息** —— 谁发给谁。没有箭头的横线读不出
                # 方向（2026-09-17 第一版就是这样）。
                "arrow": True,
                "mode": "sequence",
            }
        )

    geometry: dict[str, Any] = {
        "canvas": [round(canvas_w, 2), round(canvas_h, 2)],
        "title": contract.get("title") or "",
        "title_y": round(canvas_h - margin - TITLE_FS, 2) if has_title else None,
        "title_box": _text_box(
            contract.get("title") or "", canvas_w / 2.0,
            canvas_h - margin - TITLE_FS, TITLE_FS,
        ) if has_title else None,
        "nodes": {
            nid: {
                "rect": rect.as_list(),
                "label": nodes[nid]["label"],
                "sublabel": nodes[nid]["sublabel"],
                "color": roles[nodes[nid]["role"]],
                "role": nodes[nid]["role"],
                "shape": nodes[nid].get("shape", "box"),
                "detail_of": nodes[nid].get("detail_of", ""),
                "ports": _port_marks(
                    rect, nodes[nid].get("ports", 0),
                    at=_port_at.get((nid, "top")) or _port_at.get((nid, "bottom")),
                ),
            }
            for nid, rect in node_rects.items()
        },
        "groups": {},
        "edges": edges_out,
        "lifelines": lifelines,
        "annotations": [],
        "panels": [],
        "spec_items": [],
        "block_notes": [],
        "legend": [{"role": role, "color": roles[role]} for role in legend_entries],
        "edge_legend": [
            {"role": role, "color": colour} for role, colour in edge_colours.items()
        ],
        "legend_y": round(margin + notes_h, 2) if legend_entries else None,
        "legend_box": _row_box(
            canvas_w / 2.0, round(margin + notes_h, 2),
            legend_width(legend_entries, legend_sources), LEGEND_SWATCH + LEGEND_FS,
        ) if legend_entries else None,
        "notes": notes,
        "notes_y": round(margin, 2) if notes else None,
        "layout_findings": [],
        "layout_engine": "sequence:participants+time-axis",
        "fonts": {
            "node": NODE_FS, "sub": SUB_FS, "group": GROUP_LABEL_FS, "title": TITLE_FS,
            "legend": LEGEND_FS, "edge": EDGE_LABEL_FS, "note": NOTE_FS,
            "panel": PANEL_TITLE_FS, "spec": SPEC_FS,
        },
    }
    return geometry


def _layout_two_layer(contract: dict[str, Any]) -> dict[str, Any]:
    """两层布局：单元**内部**交给 dot，单元**之间**的摆放由我们 pack。

    「哪个单元放第几行」是页面编排（作者在 bands 里显式声明过，是确定性的），
    「一个子系统内部怎么连」是图布局（分层、交叉最小化，dot 的看家本领）。
    2026-09-16 实测：把两者塞进同一个机制会互相冲掉 —— 先是声明的 2×2 被 dot
    压平成 1×4，钉死层序后 NIC 被甩出簇、两个 PCIe 域被分到画面两端。每修一处
    冲掉另一处，这个循环不收敛。所以分开，各交给擅长的一方。
    """

    from . import dot_layout
    from .figure_contract import is_sequence

    if is_sequence(contract):
        # 时序是另一种摆法：参与者横排、消息按步下行。两层布局那一套（带子 +
        # dot 单元）在这里用不上，也不该硬套。
        return _layout_sequence(contract)

    nodes = {node["id"]: node for node in contract["nodes"]}
    groups = {group["id"]: group for group in contract["groups"]}
    sizes = {nid: _node_size(node) for nid, node in nodes.items()}
    children: dict[str | None, list[str]] = {}
    for group in contract["groups"]:
        children.setdefault(group.get("parent"), []).append(group["id"])

    def descendants(gid: str) -> list[str]:
        out = [gid]
        for child in children.get(gid, []):
            out.extend(descendants(child))
        return out

    # ── ① 每个单元内部（dot）────────────────────────────────────────────
    units: dict[str, dict[str, Any]] = {}
    for gid in children.get(None, []):
        member_groups = [groups[g] for g in descendants(gid)]
        members = [n for g in member_groups for rank in g["ranks"] for n in rank]
        units[gid] = dot_layout.layout_unit(
            contract, sizes, members=members, groups=member_groups
        )

    # ── ② 页面编排（条目横排居中，带子自上而下）──────────────────────────
    band_specs: list[list[tuple[str, str, float, float]]] = []
    for band in contract["bands"]:
        entries: list[tuple[str, str, float, float]] = []
        for entry in band:
            prefix, _, ident = entry.partition(":")
            w, h = units[ident]["size"] if prefix == "group" else sizes[ident]
            if prefix == "node" and ident in _self_loop_nodes(contract):
                # 自环挂在右上角，给它和**它的标注**留出宽度，免得压到右邻居。
                w += _self_loop_width(contract, ident)
            entries.append((prefix, ident, w, h))
        band_specs.append(entries)

    band_widths = [
        sum(w for _, _, w, _ in e) + ENTRY_GAP * (len(e) - 1) for e in band_specs
    ]
    band_heights = [
        max(h for _, _, _, h in e)
        + _band_headroom(contract, {ident for _k, ident, _w, _h in e})
        for e in band_specs
    ]
    content_w = max(band_widths) if band_widths else 0.0

    # bar（通栏长条）撑满内容宽 —— 「共享骨干」这个语义是用**形状**说的：
    # 一条横贯全幅的长条读起来就是总线/织物，一个跟别人一样大的方块不是。
    # bar 撑满的目标宽度：版面模式下是**分栏宽**（由整张版面最宽的 block 决定），
    # 不是这一个 block 自己的内容宽 —— 否则 ② 栏里的「通栏长条」只占中间一小块，
    # 形状表意就失效了（实测）。
    bar_target = max(content_w, float(contract.get("_page_content_width") or 0.0))
    for band_index, entries in enumerate(band_specs):
        widened = []
        for kind, ident, w, h in entries:
            if kind == "node" and nodes[ident].get("shape") == "bar" and len(entries) == 1:
                w = bar_target
            widened.append((kind, ident, w, h))
        band_specs[band_index] = widened
    band_widths = [
        sum(w for _, _, w, _ in e) + ENTRY_GAP * (len(e) - 1) for e in band_specs
    ]
    content_w = (
        (max(band_widths) if band_widths else content_w)
        + 2 * _around_gutter(contract)
        + _side_gutter(contract, "left")
        + _side_gutter(contract, "right")
    )
    gaps = _band_gaps(contract)
    gaps = (gaps + [BAND_GAP] * len(band_specs))[: max(0, len(band_specs) - 1)]
    content_h = sum(band_heights) + sum(gaps)

    from .figure_contract import edge_role_colors

    # 配色是**版面级**的：每个 block 各算各的，颜色按块内首次出现顺序分配，
    # 跨块就串位（实测：「服务器 1」和 CPU 同蓝、RoCE 交换机和 PCIe Switch
    # 同绿）。同一个角色在整张图里必须是同一个颜色 —— 一个问题一个真相源。
    roles = contract.get("_page_roles") or role_colors(contract)
    edge_colours = contract.get("_page_edge_roles") or edge_role_colors(contract)
    suppress = bool(contract.get("_suppress_chrome"))
    legend_entries = [] if suppress else sorted(roles)
    legend_sources = [] if suppress else list(edge_colours)
    notes = [] if suppress else list(contract.get("notes") or [])
    has_title = bool(contract.get("title")) and not suppress
    # 条件必须是「真会画的东西」而不是「手里有的数据」。这里原本写的是
    # `legend_entries or edge_colours` —— 版面模式下栏内图例被抑制
    # （legend_entries 为空），但 edge_colours 是版面级的角色表、永远非空，
    # 于是每个栏都给一条画不出来的图例留了 56px。empty_rows 十轮卡在 11-12
    # 就是它（2026-09-17）。
    legend_h = (
        (LEGEND_GAP + LEGEND_SWATCH + LEGEND_FS)
        if (legend_entries or legend_sources)
        else 0.0
    )
    notes_h = (NOTE_GAP + len(notes) * NOTE_FS * 1.7) if notes else 0.0
    title_h = (TITLE_FS * 1.4 + TITLE_GAP) if has_title else 0.0
    # 画布宽由**内容、图例、标题、图注**共同撑开 —— 少算任何一项，那一项就出界。
    legend_w = legend_width(legend_entries, legend_sources)
    title_w = _text_width(contract.get("title") or "", TITLE_FS)
    notes_w = max((_text_width(note, NOTE_FS) for note in notes), default=0.0)
    margin = INNER_MARGIN if suppress else MARGIN
    canvas_w = max(content_w, legend_w, title_w, notes_w) + 2 * margin
    canvas_h = content_h + 2 * margin + title_h + legend_h + notes_h

    node_rects: dict[str, _Rect] = {}
    group_rects: dict[str, _Rect] = {}
    placed_edges: list[dict[str, Any]] = []

    # 通栏骨干（shape=bar）的相邻带子，元件按骨干的宽度铺开 —— 这正是 bar 的
    # 语义：共享骨干，大家挂在上面。不铺开的话，4 台服务器挤在中间 40%，端口
    # 却铺满整条 bar，每条上联都得横跑一大段，8 条线缠成一团（iter14 看图）。
    bar_bands = {
        index: sum(w for _, _, w, _ in entries) + ENTRY_GAP * (len(entries) - 1)
        for index, entries in enumerate(band_specs)
        if len(entries) == 1
        and entries[0][0] == "node"
        and nodes.get(entries[0][1], {}).get("shape") == "bar"
    }
    spread_to: dict[int, float] = {}
    for index in range(len(band_specs)):
        if index in bar_bands:
            continue
        neighbours = [bar_bands[i] for i in (index - 1, index + 1) if i in bar_bands]
        if not neighbours or len(band_specs[index]) < 2:
            continue
        target = max(neighbours)
        natural = (
            sum(w for _, _, w, _ in band_specs[index])
            + ENTRY_GAP * (len(band_specs[index]) - 1)
        )
        if natural < target * 0.85:
            spread_to[index] = target

    # 侧边一列要占的那块地方已经算进 content_w 了，但**带子是居中摆的** ——
    # 于是留给右侧的空间被平分到两边，侧边节点照样出界（2026-09-17 实测 12pt）。
    # 把偏移补上：留给哪边就往哪边让。
    side_shift = (
        _side_gutter(contract, "left") - _side_gutter(contract, "right")
    ) / 2.0

    cursor_y = canvas_h - margin - title_h
    for band_index, (entries, band_h) in enumerate(zip(band_specs, band_heights)):
        band_w = sum(w for _, _, w, _ in entries) + ENTRY_GAP * (len(entries) - 1)
        cursor_x = (canvas_w - band_w) / 2.0 + side_shift
        centres: list[float] | None = None
        if band_index in spread_to:
            target = spread_to[band_index]
            left = (canvas_w - target) / 2.0 + side_shift
            centres = [
                left + target * (slot + 0.5) / len(entries)
                for slot in range(len(entries))
            ]
        for slot, (kind, ident, w, h) in enumerate(entries):
            if centres is not None:
                cursor_x = centres[slot] - w / 2.0
            y = cursor_y - band_h + (band_h - h) / 2.0
            if kind == "group":
                unit = units[ident]
                dx = cursor_x - unit["origin"][0]
                dy = y - unit["origin"][1]
                for nid, rect in unit["nodes"].items():
                    node_rects[nid] = _Rect(rect[0] + dx, rect[1] + dy, rect[2], rect[3])
                for gid, rect in unit["groups"].items():
                    group_rects[gid] = _Rect(rect[0] + dx, rect[1] + dy, rect[2], rect[3])
                for item in unit["edges"]:
                    edge = item["declared"]
                    points = [(px + dx, py + dy) for px, py in item["points"]]
                    label_xy = (
                        [item["label_xy"][0] + dx, item["label_xy"][1] + dy]
                        if item.get("label_xy")
                        else [points[len(points) // 2][0], points[len(points) // 2][1] + 5]
                    )
                    placed_edges.append(
                        {
                            "from": edge["from"],
                            "to": edge["to"],
                            "points": [[round(px, 2), round(py, 2)] for px, py in points],
                            "label": edge["label"],
                            "label_xy": [round(label_xy[0], 2), round(label_xy[1], 2)],
                            "kind": edge["kind"],
                            "role": edge.get("role", ""),
                            "color": edge_colours.get(edge.get("role", "")),
                            "bidirectional": edge["bidirectional"],
                # 几何里每条都是一股；bundle=N 说明它属于一笔 represents=N 的捆。
                "bundle": int(edge.get("bundle") or 1),
                            "mode": "dot-unit",
                        }
                    )
            else:
                # 自环节点的盒子**连同预留一起变宽** —— 看图时我以为「运行中被
                # 拉成长条」是缺陷，动手改窄，实测交叉 2 → 14：它有 9 条边，
                # 56pt 的盒子根本排不开锚点。宽是对的，只是原因不是自环，而是
                # **度数**。审美判断又一次被度量否掉（2026-09-17）。
                node_rects[ident] = _Rect(cursor_x, y, w, h)
            cursor_x += w + ENTRY_GAP
        cursor_y -= band_h + (gaps[band_index] if band_index < len(gaps) else 0.0)

    # ── ②' 侧边一列：立在整摞层旁边，纵向对齐它连到的那些行 ───────────────
    side_rail: dict[str, float] = {}
    for which in ("left", "right"):
        column = _side_columns(contract)[which]
        if not column or not node_rects:
            continue
        inner_left = min(rect.x for rect in node_rects.values())
        inner_right = max(rect.right for rect in node_rects.values())
        for order, node in enumerate(column):
            width, height = _node_size(node)
            neighbours = [
                node_rects[other]
                for edge in contract["edges"]
                for other in (edge["from"], edge["to"])
                if other in node_rects
                and node["id"] in (edge["from"], edge["to"])
            ]
            centre = (
                sum(rect.cy for rect in neighbours) / len(neighbours)
                if neighbours
                else (min(r.y for r in node_rects.values())
                      + max(r.top for r in node_rects.values())) / 2.0
            )
            offset = order * (height + ENTRY_GAP)
            if which == "left":
                x = inner_left - SIDE_GAP - width
                rail = inner_left - SIDE_RAIL_GAP
            else:
                x = inner_right + SIDE_GAP
                rail = inner_right + SIDE_RAIL_GAP
            node_rects[node["id"]] = _Rect(x, centre - height / 2.0 - offset, width, height)
            side_rail[node["id"]] = rail

    # ── ③ 跨单元的边：单元已经是盒子，用既有的正交布线器连它们 ───────────
    unit_of = {nid: gid for gid, unit in units.items() for nid in unit["nodes"]}
    band_of_node: dict[str, int] = {}
    for index, band in enumerate(contract["bands"]):
        for entry in band:
            prefix, _, ident = entry.partition(":")
            if prefix == "node":
                band_of_node[ident] = index
            else:
                for nid in units[ident]["nodes"]:
                    band_of_node[nid] = index

    drawn = {(e["from"], e["to"]) for e in placed_edges}
    cross = [e for e in contract["edges"] if (e["from"], e["to"]) not in drawn]
    cross_findings: list[dict[str, Any]] = []
    # 定义放在 if 外面：没有跨栏边时，下面画端口标记那一步照样要读它。
    _port_at: dict[tuple[str, str], list[float]] = {}
    if cross:
        cross_edges, cross_findings = _route_edges(
            {**contract, "edges": cross},
            port_marks=_port_at,
            node_rects=node_rects,
            group_rects={gid: group_rects[gid] for gid in units if gid in group_rects},
            band_rects=[],
            group_of=unit_of,
            band_of_node=band_of_node,
            rank_of={},
            side_rail=side_rail,
        )
        for item in cross_edges:
            item["color"] = edge_colours.get(item.get("role", ""))
        placed_edges.extend(cross_edges)

    geometry = {
        "canvas": [round(canvas_w, 2), round(canvas_h, 2)],
        "title": contract.get("title") or "",
        "title_y": round(canvas_h - margin - TITLE_FS, 2) if has_title else None,
        "nodes": {
            nid: {
                "rect": rect.as_list(),
                "label": nodes[nid]["label"],
                "sublabel": nodes[nid]["sublabel"],
                "color": roles[nodes[nid]["role"]],
                "role": nodes[nid]["role"],
                "shape": nodes[nid].get("shape", "box"),
                # 端口小块：让「这是一台 8 口交换机」一眼可见，不用去数连线。
                "ports": _port_marks(
                    rect, nodes[nid].get("ports", 0),
                    at=_port_at.get((nid, "top")) or _port_at.get((nid, "bottom")),
                ),
                "detail_of": nodes[nid].get("detail_of", ""),
            }
            for nid, rect in node_rects.items()
        },
        "groups": {
            gid: {
                "rect": rect.as_list(),
                "label": groups[gid]["label"],
                "label_position": groups[gid]["label_position"],
                "style": groups[gid].get("style", "solid"),
            }
            for gid, rect in group_rects.items()
        },
        "edges": placed_edges,
        "annotations": _annotation_marks(contract, node_rects),
        "legend": [{"role": role, "color": roles[role]} for role in legend_entries],
        # 边也进图例：只有节点进、边不进，读者无从知道两种线差在哪。
        "edge_legend": [
            {"role": role, "color": edge_colours[role]} for role in legend_sources
        ],
        "legend_y": round(margin + notes_h, 2) if legend_entries else None,
        "title_box": _text_box(
            contract.get("title") or "", canvas_w / 2.0,
            canvas_h - margin - TITLE_FS, TITLE_FS,
        ) if has_title else None,
        "legend_box": _row_box(
            canvas_w / 2.0, round(margin + notes_h, 2), legend_w,
            LEGEND_SWATCH + LEGEND_FS,
        ) if legend_entries else None,
        "notes": notes,
        "notes_y": round(margin, 2) if notes else None,
        "layout_findings": list(cross_findings),
        "layout_engine": "dot-units+packed-bands",
        "fonts": {
            "node": NODE_FS,
            "sub": SUB_FS,
            "group": GROUP_LABEL_FS,
            "title": TITLE_FS,
            "legend": LEGEND_FS,
            "edge": EDGE_LABEL_FS,
            "note": NOTE_FS,
        },
    }
    _place_edge_labels(
        geometry["edges"],
        [_Rect(*n["rect"]) for n in geometry["nodes"].values()],
        geometry["canvas"],
    )
    return geometry


def _pack_rows_towards(
    laid: list[dict[str, Any]],
    title_h_of: list[float],
    target: float,
    medium: str | None = None,
) -> list[list[int]]:
    """把块按相邻顺序分组成若干行：先让图在**版心里印得最大**，再看画幅。

    块数少（通常 2-6），直接枚举所有「在哪些位置断行」的方案，取最优；并列相同
    时取行数多的（更接近原来的竖排，改动最小）。确定性，同一份合同永远同一种
    排法。

    2026-09-18 之前只按 4:3 打分、一行最多 2 块：六面板永远 2×3 竖排，170mm
    下被高度压到 0.72。现在第一关键字是 `fit_scale`（封顶 1.0 —— 小图不必撑满
    版心），4:3 只在两种排法都放得下时才起作用。
    """

    from .figure_contract import DEFAULT_MEDIUM, fit_scale

    medium = medium or DEFAULT_MEDIUM
    count = len(laid)
    best: tuple[tuple[float, float, int], list[list[int]]] | None = None
    for mask in range(1 << (count - 1)):
        rows: list[list[int]] = [[0]]
        for index in range(1, count):
            if mask & (1 << (index - 1)):
                rows[-1].append(index)
            else:
                rows.append([index])
        if any(len(row) > PAGE_MAX_PER_ROW for row in rows):
            continue
        widths = [
            sum(laid[i]["size"][0] for i in row)
            + 2 * PANEL_PAD * len(row)
            + PANEL_GAP * (len(row) - 1)
            for row in rows
        ]
        heights = [
            max(laid[i]["size"][1] + 2 * PANEL_PAD + title_h_of[i] for i in row)
            for row in rows
        ]
        page_w = max(widths)
        page_h = sum(heights) + PANEL_GAP * (len(rows) - 1)
        if not page_w or not page_h:
            continue
        # 各行必须宽度相近。行宽不齐时窄行会被拉到页宽，而它的内容还是居中的
        # —— 留白从版面底部搬到了两侧，度量上是空列，看上去是「图飘在中间」。
        # 实测：不加这条，iter18 并排后空列 0→3 并触发一条 finding。
        #
        # **但它只该管「并排换来的」那种不齐。** 竖排时每行只有一个块，块的自然
        # 宽度本来就参差（624/704/600/389），这条会把**基线方案自己**也毙掉 ——
        # 于是 best 是 None，代码掉进兜底的竖排，`PAGE_TARGET_ASPECT` 一次都没
        # 参与过打分。36 轮 36 次竖长条，就是这么来的（2026-09-17 追踪确认：
        # 四块图的五个候选**全部**被拒，包括竖排那个）。
        # 基线（全单块行）永远可参选：它是「什么都不做」，没有替代方案可比。
        if any(len(row) > 1 for row in rows) and min(widths) < page_w / PAGE_ROW_WIDTH_RATIO:
            continue
        # **同一行里的块，高度也得相近。** 只比宽度时，一个又高又瘦的流程图会
        # 跟一段两行的注记配成一行，注记那一栏于是空掉九成 —— 流程图基准实测
        # 整整一栏是空的（2026-09-17）。行高取最大值，矮的那个白占一栏。
        if any(
            min(laid[i]["size"][1] for i in row) < max(laid[i]["size"][1] for i in row)
            / PAGE_ROW_HEIGHT_RATIO
            for row in rows
            if len(row) > 1
        ):
            continue
        # 整页 ≈ 分栏区 + 页边距（标题/图例两种排法都一样，不参与比较）
        fit = min(
            1.0,
            fit_scale(
                (page_w + 2 * MARGIN) * 25.4 / 72.0,
                (page_h + 2 * MARGIN) * 25.4 / 72.0,
                medium,
            ),
        )
        score = abs(math.log((page_w / page_h) / target))
        key = (-round(fit, 4), round(score, 6), -len(rows))
        if best is None or key < best[0]:
            best = (key, rows)
    return best[1] if best else [[index] for index in range(count)]


def _spec_block_size(block: dict[str, Any]) -> tuple[float, float]:
    columns = block["columns"]
    per_col = -(-len(block["items"]) // columns)
    widest = max(
        (_text_width(item, SPEC_FS) for item in block["items"]), default=0.0
    )
    width = columns * (SPEC_BULLET_W + widest) + SPEC_COL_GAP * (columns - 1)
    height = per_col * SPEC_LINE_H
    return width, height


def _note_block_size(block: dict[str, Any]) -> tuple[float, float]:
    return _text_width(block["text"], NOTE_FS), NOTE_FS * 1.8


def _is_bare_note(block: dict[str, Any]) -> bool:
    """「页脚的一句话」：note、没标题、也没被作者显式安排位置。

    `row` 是作者对这一块的**位置声明**（并排到第几行）。写了 row 还把它降级，
    等于偷偷推翻作者说过的话 —— 降级只收那些作者没表过态的。
    """

    return (
        block.get("kind") == "note"
        and not block.get("title")
        and block.get("row") in (None, "")
    )


def _layout_page(contract: dict[str, Any]) -> dict[str, Any]:
    """版面布局：figure 是**带标题的 block 列表**，diagram 只是其中一种。

    一张好的架构图不是一个 graph 的渲染，是一个版面，里面包含一个 graph。
    合同只能表达 graph 时，所有产出都长成「盒子用线连起来、按行排列」——
    形式雷同不是模型没创意，是词表里只有这一种形式（2026-09-17 根因）。

    每个 diagram block 独立布局（复用 `_layout_two_layer`），block 之间带标题
    堆叠；图例升到**版面级**（整张图一套配色，不是每栏各一套）。
    """

    from .figure_contract import edge_role_colors

    # **一句话的注记不配独占一整栏。** 壳是按块收固定费的：分栏边框 + 标题条
    # 一块 55pt，而 iter43/45 两轮真跑里那条注记的内容只有 17pt —— 效率 22%，
    # 而且整页画幅被它拖低 0.06。没标题的 note 块本来就是「页脚的一句话」，
    # 几何里早就有那个位置（`notes` / `notes_y`，无框、不占编号）——
    # 把它画成一个带框的分栏是我们的选择，而且是个坏选择。
    #
    # 只收无标题的：作者给了标题，那是他要的一个小节，框该留着。
    blocks = [
        block
        for block in contract["blocks"]
        if not _is_bare_note(block)
    ]
    demoted = [
        text
        for block in contract["blocks"]
        if _is_bare_note(block)
        for text in ([block.get("text")] if isinstance(block.get("text"), str)
                     else list(block.get("notes") or []))
        if text and str(text).strip()
    ]
    roles = role_colors(contract)
    edge_colours = edge_role_colors(contract)
    legend_entries = sorted(roles)

    # ── 每个 block 先各自量好 ────────────────────────────────────────────
    # 两遍：第一遍量出分栏宽，第二遍让含 bar 的 block 按分栏宽通栏。
    def lay_blocks(page_width: float) -> list[dict[str, Any]]:
        return _lay_blocks(contract, blocks, roles, edge_colours, page_width)

    # 三步：① 量内容 → ② 定页宽（内容/图例/标题谁宽听谁的）→ ③ 分栏齐页边，
    # 含 bar 的 block 按**分栏宽**重排。分栏比页面窄时通栏长条就不通栏了
    # （实测只占 56%），而两张参考图的分栏都是齐页边的。
    laid = lay_blocks(0.0)
    measured_w = max((item["size"][0] for item in laid), default=0.0)
    _legend_w = legend_width(legend_entries, list(edge_colours))
    _title_w = _text_width(contract.get("title") or "", TITLE_FS)
    # 页脚注记的宽度**从来没参与过页宽**：比内容宽就直接画出画布外（谁都没报过，
    # 因为几何判据只看盒子，而页脚是一行文字）。降级过来的注记让这条暴露出来 ——
    # 原来它是一个撑着页宽的 block，降级后页面反而变窄、更竖（flowchart 0.96→0.61）。
    _notes_w = max(
        (_text_width(str(note), NOTE_FS) for note in
         list(contract.get("notes") or []) + demoted),
        default=0.0,
    )
    page_content_w = max(measured_w, _legend_w, _title_w, _notes_w)
    if page_content_w > measured_w or any(
        n.get("shape") == "bar"
        for b in blocks
        if b["kind"] == "diagram"
        for n in b["nodes"]
    ):
        laid = lay_blocks(page_content_w)

    _unused: list[dict[str, Any]] = []
    for block in []:
        if block["kind"] == "diagram":
            sub = {
                **contract,
                "title": "",
                "notes": [],
                "nodes": block["nodes"],
                "groups": block["groups"],
                "bands": block["bands"],
                "edges": block["edges"],
                # 图例在版面级画，block 内不重复；配色也用版面级的同一张表。
                "_suppress_chrome": True,
                "_page_roles": roles,
                "_page_edge_roles": edge_colours,
            }
            geo = _layout_two_layer(sub)
            inner_w = geo["canvas"][0]
            inner_h = geo["canvas"][1]
            laid.append({"block": block, "geo": geo, "size": (inner_w, inner_h)})
        elif block["kind"] == "spec":
            laid.append({"block": block, "geo": None, "size": _spec_block_size(block)})
        else:
            laid.append({"block": block, "geo": None, "size": _note_block_size(block)})

    del _unused

    # 有编号，跨栏引用才指得出去。参考图就是这么编的：① 内部拓扑 / ② 集群拓扑。
    _CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫"
    titled = [index for index, item in enumerate(laid) if item["block"].get("title")]
    # 编号**不当文字排**：①②③ 在 TikZ 的衬线正文字体里没有字形，整个字符
    # 被静默丢掉 —— 合同写的「① 单台服务器内部拓扑」，图上只剩后半截
    # （2026-09-17 实测）。改成框架自己画一个圆圈+阿拉伯数字，任何字体都画
    # 得出。作者自己在标题里写了 ①，这里剥掉、按同一套重画，顺带保证编号
    # 连续（作者编错了也不会两个 ②）。
    # 编号归框架管，作者一概不编 —— 所以**先无条件剥掉**作者写的编号，再决定
    # 要不要编。只有一个分栏时编号毫无意义，作者却仍可能写（流程图基准实测：
    # 单栏图的标题是「①数据处理流水线」）。
    for index in titled:
        title = (laid[index]["block"].get("title") or "").lstrip()
        if title[:1] in _CIRCLED:
            laid[index]["block"] = {**laid[index]["block"], "title": title[1:].lstrip()}
    numbers: dict[int, int] = {}
    if len(titled) > 1:
        for order, index in enumerate(titled):
            numbers[index] = order + 1
    # 组 id → 它所在块的编号，供 detail_of 解析成一句「详见 ①」
    # 目标块没有编号（整页只有一个带标题的块）时，仍然要画出「这个盒子里面还有
    # 东西，画在别处」—— 只是那句话里没有编号可指。0 = 有归属但无编号。
    # 原先这里直接 continue，于是 detail_ref 解析不出来、内嵌边框和徽章一个都不
    # 画，声明等于没发生（2026-09-17 实测）。
    group_block_number: dict[str, int] = {}
    for index, item in enumerate(laid):
        mark = numbers.get(index, 0)
        if item["block"].get("kind") != "diagram":
            continue
        for group in item["block"].get("groups") or []:
            group_block_number[group["id"]] = mark
    for index, item in enumerate(laid):
        geo = item["geo"]
        if not geo:
            continue
        for node in geo["nodes"].values():
            target = node.get("detail_of")
            if target and target in group_block_number:
                node["detail_ref"] = group_block_number[target]

    title_h_of = [
        (PANEL_TITLE_H if item["block"].get("title") else 0.0) for item in laid
    ]
    panel_hs = [
        item["size"][1] + 2 * PANEL_PAD + th for item, th in zip(laid, title_h_of)
    ]
    # 同一个 row 的 block 并排；没给 row 的各占一行。
    rows: list[list[int]] = []
    seen_rows: dict[int, int] = {}
    for index, item in enumerate(laid):
        key = item["block"].get("row")
        if key is None:
            rows.append([index])
        elif key in seen_rows:
            rows[seen_rows[key]].append(index)
        else:
            seen_rows[key] = len(rows)
            rows.append([index])

    # 作者一个 row 都没给时，框架自己挑一种并排方式。**不挑就等于每次都出竖版**
    # —— 十九轮跑下来画幅始终在 0.87-0.89，而参考图是 1.32；一张竖长条塞进
    # 论文或幻灯片都得缩得看不清。这不是「好不好看」的判断，是可数的：
    # 在所有相邻合并方案里，取画幅最接近 4:3 的那个（并列不得比最宽的块宽太多，
    # 否则并排反而制造留白）。作者显式写了 row 就一概不动 —— 他说了算。
    laid = _widen_for_titles(laid)
    if all(item["block"].get("row") is None for item in laid) and len(laid) > 1:
        from .figure_contract import medium_of

        rows = _pack_rows_towards(laid, title_h_of, PAGE_TARGET_ASPECT, medium_of(contract))
    panel_w = max(
        max(
            (
                sum(laid[i]["size"][0] for i in row)
                + 2 * PANEL_PAD * len(row)
                + PANEL_GAP * (len(row) - 1)
                for row in rows
            ),
            default=0.0,
        ),
        page_content_w,
    )
    row_hs = [max(panel_hs[i] for i in row) for row in rows]

    notes = list(contract.get("notes") or []) + demoted
    has_title = bool(contract.get("title"))
    # 条件必须是「真会画的东西」而不是「手里有的数据」。这里原本写的是
    # `legend_entries or edge_colours` —— 版面模式下栏内图例被抑制
    # （legend_entries 为空），但 edge_colours 是版面级的角色表、永远非空，
    # 于是每个栏都给一条画不出来的图例留了 56px。empty_rows 十轮卡在 11-12
    # 就是它（2026-09-17）。
    legend_h = (
        (LEGEND_GAP + LEGEND_SWATCH + LEGEND_FS)
        if (legend_entries or legend_sources)
        else 0.0
    )
    notes_h = (NOTE_GAP + len(notes) * NOTE_FS * 1.7) if notes else 0.0
    fig_title_h = (TITLE_FS * 1.4 + TITLE_GAP) if has_title else 0.0

    canvas_w = max(panel_w, _legend_w, _title_w, _notes_w) + 2 * MARGIN
    canvas_h = (
        sum(row_hs) + PANEL_GAP * (len(rows) - 1)
        + 2 * MARGIN + fig_title_h + legend_h + notes_h
    )

    nodes_out: dict[str, Any] = {}
    groups_out: dict[str, Any] = {}
    edges_out: list[dict[str, Any]] = []
    panels: list[dict[str, Any]] = []
    annotations_out: list[dict[str, Any]] = []
    specs: list[dict[str, Any]] = []
    block_notes: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []

    cursor_y = canvas_h - MARGIN - fig_title_h
    for row, row_h in zip(rows, row_hs):
      # 一行里的分栏按各自的自然宽度分配剩余宽度
      natural = [laid[i]["size"][0] + 2 * PANEL_PAD for i in row]
      slack = panel_w - sum(natural) - PANEL_GAP * (len(row) - 1)
      widths = [w + slack / len(row) for w in natural]
      cursor_x_row = (canvas_w - panel_w) / 2.0
      for slot, index in enumerate(row):
        item, panel_h, title_h = laid[index], row_h, title_h_of[index]
        block = item["block"]
        panel_w_here = widths[slot]
        panel_x = cursor_x_row
        cursor_x_row += panel_w_here + PANEL_GAP
        panel_y = cursor_y - panel_h
        panels.append(
            {
                "rect": [round(panel_x, 2), round(panel_y, 2), round(panel_w_here, 2), round(panel_h, 2)],
                "title": block.get("title", ""),
                "number": numbers.get(index),
                "title_xy": [
                    round(panel_x + PANEL_PAD, 2),
                    round(panel_y + panel_h - PANEL_TITLE_H + 8, 2),
                ],
            }
        )
        inner_w, inner_h = item["size"]
        inner_x = panel_x + (panel_w_here - inner_w) / 2.0
        inner_y = panel_y + PANEL_PAD

        if block["kind"] == "diagram":
            geo = item["geo"]
            dx, dy = inner_x, inner_y
            for nid, node in geo["nodes"].items():
                r = node["rect"]
                # 端口小块是节点的一部分，必须**跟着节点一起平移**。只移 rect
                # 不移 ports，小方块就会留在 block 内坐标上、落到别的分栏的
                # 文字上（2026-09-17 看图发现；所有度量当时全绿 —— 端口不参与
                # 交叉/贴线/包围盒计算，机械判据看不见这种错位）。
                nodes_out[nid] = {
                    **node,
                    "rect": [round(r[0] + dx, 2), round(r[1] + dy, 2), r[2], r[3]],
                    "ports": [
                        [round(px + dx, 2), round(py + dy, 2), pw, ph]
                        for px, py, pw, ph in (node.get("ports") or [])
                    ],
                }
            for gid, group in geo["groups"].items():
                r = group["rect"]
                groups_out[gid] = {**group, "rect": [round(r[0] + dx, 2), round(r[1] + dy, 2), r[2], r[3]]}
            for edge in geo["edges"]:
                edges_out.append(
                    {
                        **edge,
                        "points": [[round(x + dx, 2), round(y + dy, 2)] for x, y in edge["points"]],
                        "label_xy": [
                            round(edge["label_xy"][0] + dx, 2),
                            round(edge["label_xy"][1] + dy, 2),
                        ],
                    }
                )
            for annot in geo.get("annotations") or []:
                annotations_out.append(
                    {
                        **annot,
                        "start": [round(annot["start"][0] + dx, 2), round(annot["start"][1] + dy, 2)],
                        "end": [round(annot["end"][0] + dx, 2), round(annot["end"][1] + dy, 2)],
                        "text_xy": [
                            round(annot["text_xy"][0] + dx, 2),
                            round(annot["text_xy"][1] + dy, 2),
                        ],
                    }
                )
            findings.extend(geo.get("layout_findings") or [])
        elif block["kind"] == "spec":
            columns = block["columns"]
            per_col = -(-len(block["items"]) // columns)
            col_w = (inner_w - SPEC_COL_GAP * (columns - 1)) / columns
            for index, text in enumerate(block["items"]):
                col, row = divmod(index, per_col)
                specs.append(
                    {
                        "text": text,
                        "xy": [
                            round(inner_x + col * (col_w + SPEC_COL_GAP), 2),
                            round(inner_y + inner_h - (row + 1) * SPEC_LINE_H + 6, 2),
                        ],
                    }
                )
        else:
            block_notes.append(
                {
                    "text": block["text"],
                    # 居中要居**它自己那一栏**的中。原先写的是 canvas_w/2（整页
                    # 中心）—— 每个块都通栏时碰巧对，一旦有块并排就把注记甩到
                    # 别人的栏里去了（2026-09-17 流程图基准实测：注记横跨两栏、
                    # 压在左栏底部外面）。
                    "xy": [round(inner_x + inner_w / 2.0, 2), round(inner_y + 4, 2)],
                }
            )
      cursor_y -= row_h + PANEL_GAP

    return {
        "canvas": [round(canvas_w, 2), round(canvas_h, 2)],
        "title": contract.get("title") or "",
        "title_y": round(canvas_h - MARGIN - TITLE_FS, 2) if has_title else None,
        "nodes": nodes_out,
        "groups": groups_out,
        "edges": edges_out,
        "panels": panels,
        "annotations": annotations_out,
        "spec_items": specs,
        "block_notes": block_notes,
        "legend": [{"role": role, "color": roles[role]} for role in legend_entries],
        "edge_legend": [
            {"role": role, "color": colour} for role, colour in edge_colours.items()
        ],
        "legend_y": round(MARGIN + notes_h, 2) if (legend_entries or edge_colours) else None,
        "title_box": _text_box(
            contract.get("title") or "", canvas_w / 2.0,
            canvas_h - MARGIN - TITLE_FS, TITLE_FS,
        ) if has_title else None,
        "legend_box": _row_box(
            canvas_w / 2.0, round(MARGIN + notes_h, 2), _legend_w,
            LEGEND_SWATCH + LEGEND_FS,
        ) if (legend_entries or edge_colours) else None,
        "notes": notes,
        "notes_y": round(MARGIN, 2) if notes else None,
        "layout_findings": findings,
        "layout_engine": "page:dot-units+packed-bands",
        "fonts": {
            "node": NODE_FS, "sub": SUB_FS, "group": GROUP_LABEL_FS,
            "title": TITLE_FS, "legend": LEGEND_FS, "edge": EDGE_LABEL_FS,
            "note": NOTE_FS, "panel": PANEL_TITLE_FS, "spec": SPEC_FS,
        },
    }


def _lay_blocks(
    contract: dict[str, Any],
    blocks: list[dict[str, Any]],
    roles: dict[str, str],
    edge_colours: dict[str, str],
    page_width: float,
) -> list[dict[str, Any]]:
    """量每个 block 的尺寸与几何。`page_width` 是分栏宽（第一遍传 0）。"""

    laid: list[dict[str, Any]] = []
    for block in blocks:
        if block["kind"] == "diagram":
            sub = {
                **contract,
                "title": "",
                "notes": [],
                "nodes": block["nodes"],
                "groups": block["groups"],
                "bands": block["bands"],
                "edges": block["edges"],
                "annotations": block.get("annotations") or [],
                "_suppress_chrome": True,
                "_page_roles": roles,
                "_page_edge_roles": edge_colours,
                # 分栏内可用宽 = 分栏宽 − 分栏内边距 − block 自己的边距
                "_page_content_width": max(0.0, page_width - 2 * PANEL_PAD - 2 * INNER_MARGIN),
            }
            geo = _layout_two_layer(sub)
            laid.append(
                {"block": block, "geo": geo, "size": (geo["canvas"][0], geo["canvas"][1])}
            )
        elif block["kind"] == "spec":
            laid.append({"block": block, "geo": None, "size": _spec_block_size(block)})
        else:
            laid.append({"block": block, "geo": None, "size": _note_block_size(block)})
    return laid


def _port_marks(
    rect: _Rect, count: int, at: list[float] | None = None
) -> list[list[float]]:
    """组件边上的端口小块（沿顶边均布）。

    「≥8 个 400GbE 端口」这件事，画 8 个小方块比写一行字有效得多，也比让读者
    去数连线可靠 —— 连线可能只画了代表性的几条。
    """

    if count <= 0:
        return []
    if at:
        # 线真正落下的位置（布线器给的）。多出来的口按均布补齐 ——
        # 「画了 8 个口只有 4 条线」那条判据靠的就是它们还在。
        centres = list(at)
        while len(centres) < count:
            centres.append(rect.x + rect.w * (len(centres) + 1) / (count + 1))
        return [
            [round(cx - PORT_W / 2.0, 2), round(rect.top - PORT_H / 2.0, 2),
             PORT_W, PORT_H]
            for cx in sorted(centres[:count])
        ]
    marks: list[list[float]] = []
    for index in range(count):
        t = (index + 1) / (count + 1)
        marks.append(
            [
                round(rect.x + rect.w * t - PORT_W / 2.0, 2),
                round(rect.top - PORT_H / 2.0, 2),
                PORT_W,
                PORT_H,
            ]
        )
    return marks


def _annotation_marks(
    contract: dict[str, Any], node_rects: dict[str, _Rect]
) -> list[dict[str, Any]]:
    """出框叙事引出：从组件某一侧引一支箭头 + 一句话。"""

    out: list[dict[str, Any]] = []
    for item in contract.get("annotations") or []:
        rect = node_rects.get(item["anchor"])
        if rect is None:
            continue
        side = item["side"]
        if side == "bottom":
            start, end = (rect.cx, rect.y), (rect.cx, rect.y - ANNOT_LEAD)
            text_xy, align = (rect.cx, end[1] - ANNOT_FS - 2), ("center", "top")
        elif side == "top":
            start, end = (rect.cx, rect.top), (rect.cx, rect.top + ANNOT_LEAD)
            text_xy, align = (rect.cx, end[1] + 4), ("center", "bottom")
        elif side == "left":
            start, end = (rect.x, rect.cy), (rect.x - ANNOT_LEAD, rect.cy)
            text_xy, align = (end[0] - 4, rect.cy), ("right", "center")
        else:
            start, end = (rect.right, rect.cy), (rect.right + ANNOT_LEAD, rect.cy)
            text_xy, align = (end[0] + 4, rect.cy), ("left", "center")
        out.append(
            {
                "anchor": item["anchor"],
                "text": item["text"],
                "start": [round(start[0], 2), round(start[1], 2)],
                "end": [round(end[0], 2), round(end[1], 2)],
                "text_xy": [round(text_xy[0], 2), round(text_xy[1], 2)],
                "align": list(align),
            }
        )
    return out


# ── 印刷尺寸：合同的 medium 是画布的预算（2026-09-18 iter11）───────────────
#
# 画布的 pt 数就是物理英寸数；把它放进版心的缩放 = min(W/w, H/h)。过去这个数
# 只在 layout_metrics 里事后报告，编译器从不按它反推排布；模型于是对着「7.8pt
# 但 p10 5.6pt」反复重声明重渲染（最后一个子 run 里同一张总览渲了 7 次）。
# 现在：① 排布按它选（_pack_rows_towards），② 声明那一刻算出每一档文字的
# 印刷字号，低于下限拒绝并给出算过的改法（tools/figure.py）。


def used_text_tiers(geometry: dict[str, Any]) -> dict[str, float]:
    """图上真有文字的那些档 → 自然尺寸下的字号（pt）。

    只数在场的：没有副标题的图不该被副标题那一档判；有副标题的图，副标题就是
    读者要读的字。chip 标签与引出注记不在 fonts 表里（用模块常量），这里补上。
    """

    fonts = geometry.get("fonts") or {}
    used: dict[str, float] = {}

    def mark(tier: str, size: float | None = None) -> None:
        if tier not in used:
            used[tier] = float(size if size is not None else fonts.get(tier, TYPE_BASE))

    if geometry.get("title"):
        mark("title")
    for node in (geometry.get("nodes") or {}).values():
        if node.get("label"):
            if node.get("shape") == "chip":
                mark("chip", CHIP_FS)
            else:
                mark("node")
        if node.get("sublabel"):
            mark("sub")
    for group in (geometry.get("groups") or {}).values():
        if group.get("label"):
            mark("group")
    for edge in geometry.get("edges") or []:
        if edge.get("label"):
            mark("edge")
    if geometry.get("annotations"):
        mark("annotation", ANNOT_FS)
    if geometry.get("legend") or geometry.get("edge_legend"):
        mark("legend")
    if geometry.get("notes") or geometry.get("block_notes"):
        mark("note")
    if geometry.get("spec_items"):
        mark("spec")
    if any(panel.get("title") for panel in geometry.get("panels") or []):
        mark("panel")
    return used


def print_fit(
    geometry: dict[str, Any],
    contract: dict[str, Any],
    *,
    floor_pt: float | None = None,
) -> dict[str, Any]:
    """这张图印在合同的 medium 里：缩放多少、每档文字还剩几 pt、过不过下限。"""

    from .figure_contract import MEDIA, fit_scale, font_floor_pt, medium_of

    medium = medium_of(contract)
    spec = MEDIA[medium]
    floor = float(floor_pt) if floor_pt is not None else font_floor_pt(medium)
    canvas = geometry.get("canvas") or [1.0, 1.0]
    w_mm = (canvas[0] or 1.0) * 25.4 / 72.0
    h_mm = (canvas[1] or 1.0) * 25.4 / 72.0
    # 不封顶：消费方（写作节点的 \includegraphics[width=\textwidth]、幻灯、
    # 海报）都把图撑到版心宽，窄图会被放大、字随之变大。封顶只在挑排法时用。
    scale = fit_scale(w_mm, h_mm, medium)
    tiers = used_text_tiers(geometry)
    final = {tier: round(size * scale, 2) for tier, size in tiers.items()}
    smallest_tier = min(final, key=final.get) if final else None
    smallest = final[smallest_tier] if smallest_tier else 0.0
    return {
        "medium": medium,
        "print_box_mm": [spec["w"], spec["h"]],
        "canvas_mm": [round(w_mm, 1), round(h_mm, 1)],
        "scale": round(scale, 4),
        "bound_by": "width" if spec["w"] / w_mm <= spec["h"] / h_mm else "height",
        "final_width_mm": round(w_mm * scale, 1),
        "final_height_mm": round(h_mm * scale, 1),
        "floor_pt": floor,
        "final_pt_by_tier": final,
        "smallest_tier": smallest_tier,
        "final_smallest_pt": round(smallest, 2),
        "fits": bool(smallest_tier is None or smallest >= floor - 1e-6),
    }


def _independent_unit_bands(contract: dict[str, Any]) -> list[list[str]] | None:
    """平铺合同里的 bands 是不是**互不相连的单元**（六种拓扑各一组那种）。

    是 → 这些 band 不是层级（CPU 层 / GPU 层），是一张网格里的格子，重新分行
    不改变任何结构事实，可以替作者算「几个一行印出来最大」。不是 → 返回 None，
    band 是作者的层序，一个字都不碰。
    """

    if contract.get("blocks") and len(contract["blocks"]) > 1:
        return None
    bands = contract.get("bands") or []
    if len(bands) < 2:
        return None
    if any(not entry.startswith("group:") for band in bands for entry in band):
        return None
    member_of: dict[str, str] = {}
    for group in contract.get("groups") or []:
        for rank in group.get("ranks") or []:
            for nid in rank:
                member_of[nid] = group["id"]
    for edge in contract.get("edges") or []:
        src, dst = edge.get("from", ""), edge.get("to", "")
        a = src[6:] if src.startswith("group:") else member_of.get(src)
        b = dst[6:] if dst.startswith("group:") else member_of.get(dst)
        if a != b:
            return None  # 组之间有线：这是一张图，不是格子
    return bands


def print_fit_remedies(
    contract: dict[str, Any], fit: dict[str, Any]
) -> list[dict[str, Any]]:
    """印不下时**算过**的改法，不是建议清单：每一条都带它会得到的字号。

    候选：① 独立单元换行数（2/3 一行）；② 拆成两张图；③ 去掉最小那一档的文字；
    ④ 折叠同构组（layout_findings 已点名）。只在拒绝时才跑（每个候选一次布局）。
    """

    import copy as _copy

    from .figure_contract import DEFAULT_MEDIUM, medium_of

    remedies: list[dict[str, Any]] = []
    floor = float(fit.get("floor_pt") or 0.0)
    medium = medium_of(contract) or DEFAULT_MEDIUM

    def measure(variant: dict[str, Any]) -> dict[str, Any] | None:
        try:
            geometry = layout_for_backend(_copy.deepcopy(variant), "tikz")
        except Exception:  # noqa: BLE001 —— 候选算不出来就不推荐它
            return None
        return print_fit(geometry, variant, floor_pt=floor)

    units = _independent_unit_bands(contract)
    if units:
        flat = [entry for band in units for entry in band]
        current = [len(band) for band in units]
        for per_row in sorted({2, 3, 4} - {max(current)}):
            if per_row > len(flat):
                continue
            rebanded = [flat[i:i + per_row] for i in range(0, len(flat), per_row)]
            got = measure({**contract, "bands": rebanded})
            if got is None:
                continue
            remedies.append({
                "change": f"re-band the {len(flat)} independent units {per_row} per row",
                "bands": rebanded,
                "final_smallest_pt": got["final_smallest_pt"],
                "smallest_tier": got["smallest_tier"],
                "fits": got["fits"],
            })
        # 拆成两张：只留前一半的单元（及其节点、边），行数照旧
        half = -(-len(flat) // 2)
        keep_groups = {entry[6:] for entry in flat[:half]}
        keep_nodes = {
            nid
            for group in contract.get("groups") or []
            if group["id"] in keep_groups
            for rank in group.get("ranks") or []
            for nid in rank
        }
        split = {
            **contract,
            "groups": [g for g in contract.get("groups") or [] if g["id"] in keep_groups],
            "nodes": [n for n in contract.get("nodes") or [] if n["id"] in keep_nodes],
            "edges": [
                e for e in contract.get("edges") or []
                if e.get("from") in keep_nodes and e.get("to") in keep_nodes
            ],
            "annotations": [
                a for a in contract.get("annotations") or [] if a.get("anchor") in keep_nodes
            ],
            "assertions": [],
            "bands": [flat[i:i + max(current)] for i in range(0, half, max(current))],
        }
        split.pop("blocks", None)
        got = measure(split)
        if got is not None:
            remedies.append({
                "change": (
                    f"split into two figures of {half} units each — that is the caller's "
                    "call: report it rather than declaring a half"
                ),
                "final_smallest_pt": got["final_smallest_pt"],
                "smallest_tier": got["smallest_tier"],
                "fits": got["fits"],
            })
    tier = fit.get("smallest_tier")
    if tier in ("sub", "edge", "annotation"):
        stripped = _copy.deepcopy(contract)
        if tier == "sub":
            for node in stripped.get("nodes") or []:
                node.pop("sublabel", None)
            for block in stripped.get("blocks") or []:
                for node in block.get("nodes") or []:
                    node.pop("sublabel", None)
            what = "move the sublabels into node labels or a spec block"
        elif tier == "edge":
            for edge in stripped.get("edges") or []:
                edge.pop("label", None)
            for block in stripped.get("blocks") or []:
                for edge in block.get("edges") or []:
                    edge.pop("label", None)
            what = "drop the edge labels (keep role → legend)"
        else:
            stripped["annotations"] = []
            for block in stripped.get("blocks") or []:
                block["annotations"] = []
            what = "drop the callout annotations"
        got = measure(stripped)
        if got is not None:
            remedies.append({
                "change": f"{what}: the smallest tier is then {got['smallest_tier']!r}",
                "final_smallest_pt": got["final_smallest_pt"],
                "smallest_tier": got["smallest_tier"],
                "fits": got["fits"],
            })
    remedies.sort(key=lambda item: -float(item.get("final_smallest_pt") or 0.0))
    return remedies
