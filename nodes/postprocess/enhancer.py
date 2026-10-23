"""图像增强（enhancement）—— 把 SVG 轨已铸造的布局 PNG 经图像生成模型美化。

## 出处与署名（attribution）

本模块的 prompt 工艺与响应提取逻辑移植自 **AutoFigure**（MIT License）：
`autofigure/enhancer.py`（三模式增强 prompt + `convert_code_to_text2image_prompt`
的五节视觉规格转换 + OpenAI 兼容图像响应的多分支提取）。
上游仓库：https://github.com/ResearAI/AutoFigure ；按该仓库
`CITATION_AND_ATTRIBUTION.md` 的请求署名：

    Zhu et al., "AutoFigure: Generating and Refining Publication-Ready
    Scientific Illustrations", ICLR 2026.
    https://openreview.net/forum?id=5N3z9JQJKq

这是一次**移植**，不是重写：三种模式的 prompt 骨架与五节规格转换的措辞
随源保留（英文原文），只在下列接缝处做了有理由的改动（逐条见下）。

## 与 AutoFigure 原版的差异（为什么这么改）

1. **保留文字机械注入（推演发现 #5 / 改进 #11）**。原版 code2prompt 的
   SECTION 3（要保留的文字）由转译 LLM 从代码里读出来 —— 模型会漏、会改写
   （"400Gbps" 变成 "400 Gbps" 这类变形在沙盘推演里是被点名的风险）。这里
   SECTION 3 不再由 LLM 产生：转换 prompt 明确要求转译模型**不要**枚举文字，
   权威的保留文字列表由调用方（来自合同/DOM 的机械抽取）以 `keep_text`
   传入，由本模块**机械拼接**进最终增强 prompt 末尾的
   "PRESERVED TEXT — AUTHORITATIVE" 块，并附硬指令（逐字保留、不得增删改、
   冲突时以此列表为准）。即使转译 LLM 的 SECTION 3 漂移，最终 prompt 里
   仍有权威列表兜底。none/code 两个模式也注入同一列表（它们原本只有
   "preserve external text" 的原则性措辞，没有清单）。
2. **占位框描述适配 SVG 轨约定**。原版的占位框是"灰色矩形 + `[icon]:` 前缀
   文字"（mxGraph/draw.io 约定）；postprocess SVG 轨的占位框是**角色色块 +
   居中短标签**，源码里以 `data-placeholder` 属性标记（词表唯一真相源在
   `svg_vocab.py`，本模块 import 它写进 prompt，漂移即改一处）。图像模型
   看的是像素不是 DOM，所以 raster 侧的描述相应泛化为"彩色占位块"。
3. **不搬原版的失败兜底**。原版图像提取失败时"把原图复制为输出但返回 None"
   （enhancer.py 上游 L938-947）—— 一份美化产物长得和原图一模一样，下游
   只有记得查返回值才知道美化没发生。这里提取失败 = `status: "error"`，
   如实分类进 attempts，绝不静默复制原图。enhancement 是锦上添花，失败
   不拖累本体（成本闸在调用方，§4.5：每 request_id ≤2 次，原图照常交付）。
4. **不搬多变体循环与 web 路由**。一次 `enhance()` 只生成**一张**变体
   （调用方逐张调，AutoFigure 的多变体本来就是调用方循环）。
   不搬 base64 参考图注入 —— 参考图机制在 postprocess 走"预转译文字规格"
   （§六-F14，冻结进 skill 正文），不进 enhancer。
5. **图像模型调用协议按原版的 chat/completions 形态**（bianxie/openrouter
   的 OpenAI 兼容图像生成就是 chat 协议 + base64 图进 messages），用框架
   已有依赖 `httpx` 直连；响应提取保留原版的全部四个分支
   （message.images / markdown data URL / content list / data.b64_json），
   其中 `data[].b64_json` 分支同时覆盖了 DALL-E 风格 images API 的响应形状。
   **本模块不 import PIL**（图像字节直接写盘，PNG 有效性用标准库读 8 字节
   magic 校验 —— 部署环境的 PIL 可能是坏的，这不是 enhancer 该修的）。
6. **无 verdict 语义**。返回里只有成功/失败/未配置的事实，没有任何
   status/quality/verdict 判决字段；美不美由下游（VLM 证人 + 人）判。

## 模型身份两级解析（enhancement_identity）

第 1 级 `core.model_roles.resolve("image_generation")`：平台将来在
`shared/model_roles.yaml` 目录加 `image_generation` 角色并绑定后**自动生效**，
本模块零改动（角色不在目录 / 未绑定 → 落第 2 级，不视为错误）。
第 2 级 `enhancer_config.yaml`（同目录，非密钥：provider/base_url/model/
默认参数）+ 环境变量密钥 `POSTPROCESS_ENHANCEMENT_API_KEY`。

**密钥在模块 import 时从 os.environ 弹出存模块级变量**：postprocess 有
execute_python（subprocess 继承 env），密钥躺在 env 里就会进沙箱子进程
（core/model_roles.py 模块头记载的正是这类"透传名单与擦除名单各自演化"
的事故）。弹出的代价是 .env 晚于 import 加载时读不到 —— 部署侧（worker
注入 env 在进程启动早期）不受影响，这是设计已接受的取舍（§4.5）。

两级都缺席 → `enhancement_identity()` 返回 None → `enhance()` 诚实返回
`{"status": "not_configured"}`，enhancement 自动关闭，**不是异常**。

## code2prompt 的文本 LLM 从哪来

转译那一步需要一次文本 LLM 调用。框架内文本 LLM 的正道是
`core.llm.LLMClient()`（无参 = model_roles 的 `reasoning` 角色；重试、
超时、取消咽喉都在那一层，节点不自建客户端）。`State` 不携带 llm 实例，
`enhance()` 签名里的 `state` 与节点其他工具同形保留（记账由调用方按返回
的 usage 落，对齐 vlm_witness 的"usage 进 attempts、调用方落账"先例）。
拿不到 reasoning 绑定时 code2prompt **降级为 code 模式**并在返回里如实记
`mode_fallback` —— 有 SVG 源码在场时，code 模式仍有完整布局信息可用。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import yaml

from .svg_vocab import ATTR_PLACEHOLDER

if TYPE_CHECKING:
    from core.state import State

log = logging.getLogger(__name__)

#: 增强图像模型的密钥 env 名。import 即弹出（理由见模块 docstring 的密钥节）。
_ENHANCEMENT_API_KEY_ENV = "POSTPROCESS_ENHANCEMENT_API_KEY"
_ENHANCEMENT_API_KEY: str = os.environ.pop(_ENHANCEMENT_API_KEY_ENV, "").strip()

_CONFIG_PATH = Path(__file__).resolve().parent / "enhancer_config.yaml"

#: 平台模型角色目录里图像生成角色的名字（平台加了就自动生效，见模块 docstring）。
_IMAGE_GENERATION_ROLE = "image_generation"

MODES: tuple[str, ...] = ("none", "code", "code2prompt")
DEFAULT_MODE = "code2prompt"

#: PNG 文件 magic（标准库校验，不依赖 PIL —— 部署环境的 PIL 可能是坏的）。
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

#: 默认风格（AutoFigure 原版同款缺省）。
_DEFAULT_ART_STYLE = "Modern scientific illustration"

#: code 模式里嵌进 prompt 的 SVG 源码上限（AutoFigure 原版就是 5000 字符）。
_CODE_EXCERPT_CHARS = 5000

#: code2prompt 转译调用的默认输出预算（enhancer_config.yaml defaults 可覆盖）。
_CONVERSION_MAX_TOKENS = 8192

#: 提取失败时落盘的响应调试 JSON 的体积上限（响应里可能躺着巨大的 base64
#: blob，全量落盘既贵又没信息量 —— 截断保留形状足够归因）。
_DEBUG_RESPONSE_MAX_CHARS = 200_000


# ── 身份解析 ─────────────────────────────────────────────────────────────


def _load_config() -> tuple[dict[str, Any], str | None]:
    """读 enhancer_config.yaml。返回 (配置 dict, 问题描述)。

    缺席是正常局面（enhancement 未启用），不算问题；读到了但解析不了是
    **配置坏了**，要吵（log.warning）—— 坏配置比没配置更该被看见
    （core/model_roles.py 交付通道的同款哲学）。
    """
    try:
        raw = _CONFIG_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    except OSError as exc:
        log.warning("enhancer_config.yaml 读不到：%s", exc)
        return {}, f"config_unreadable: {exc}"
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        log.warning("enhancer_config.yaml 不是合法 YAML：%s", exc)
        return {}, f"config_invalid_yaml: {exc}"
    if value is None:
        return {}, None
    if not isinstance(value, dict):
        log.warning("enhancer_config.yaml 顶层必须是 mapping，得到 %s", type(value).__name__)
        return {}, f"config_not_mapping: {type(value).__name__}"
    return value, None


def enhancement_identity() -> dict | None:
    """两级解析图像生成模型身份；未配置返回 None。
    第 1 级：core.model_roles.resolve("image_generation")（平台未来加角色自动生效）；
    第 2 级：enhancer_config.yaml（非密钥：provider/base_url/model/默认参数）
    + 环境变量 POSTPROCESS_ENHANCEMENT_API_KEY。
    env 密钥在模块 import 时 os.environ.pop 出来存模块级变量 —— 防沙箱子进程继承泄漏。
    返回形状：{"provider": str, "base_url": str, "model": str, "api_key": str, "source": "model_role"|"config_file"}"""
    # ── 第 1 级：平台模型角色 ─────────────────────────────────────────────
    # 角色尚未进目录（spec() 返回 None）或目录本身不可读（ModelRoleError）
    # 都按"第 1 级缺席"落第 2 级：前者是**预期中的未来扩展位**，后者已按
    # model_roles 的哲学吵过（那边目录坏了会抛带原因的错，这里降级不掩盖 —
    # log 里留痕）。
    try:
        from core import model_roles
    except Exception as exc:  # 不在框架运行时内（如独立脚本）—— 第 1 级天然缺席
        log.warning("core.model_roles 不可导入，第 1 级身份解析跳过：%s", exc)
    else:
        try:
            role_known = model_roles.spec(_IMAGE_GENERATION_ROLE) is not None
        except model_roles.ModelRoleError as exc:
            log.warning("模型角色目录不可读，第 1 级身份解析按缺席处理：%s", exc)
            role_known = False
        if role_known:
            binding = model_roles.resolve(_IMAGE_GENERATION_ROLE)
            if binding is not None:
                return {
                    "provider": binding.provider or "openai_compatible",
                    "base_url": binding.base_url,
                    "model": binding.model,
                    "api_key": binding.api_key,
                    "source": "model_role",
                }

    # ── 第 2 级：节点配置文件 + env 密钥 ──────────────────────────────────
    config, problem = _load_config()
    base_url = str(config.get("base_url") or "").strip()
    model = str(config.get("model") or "").strip()
    if not (base_url and model):
        if problem:
            log.warning("enhancer 第 2 级身份配置有问题但不可用：%s", problem)
        return None
    # api_key 不在"是否配置"的判据里：自建端点（vLLM 等）默认不鉴权，"没有 key"
    # 是它们的正常形态（LLMClient 2026-09-15 起的同款立场）。端点要不要 key
    # 由端点自己回答 —— 它回 401，那条错会如实分类进 attempts。
    return {
        "provider": str(config.get("provider") or "").strip() or "openai_compatible",
        "base_url": base_url,
        "model": model,
        "api_key": _ENHANCEMENT_API_KEY,
        "source": "config_file",
    }


def _not_configured_reason() -> str:
    """说出**是哪一种**缺席（对齐 vlm_witness "缺席也要说出是哪一种缺席"的先例）。"""
    parts = [
        f"image_generation 角色未绑定（shared/model_roles.yaml 目录无此角色或平台未指派）",
        f"且 {_CONFIG_PATH.name} 未提供可用的 base_url/model",
    ]
    _config, problem = _load_config()
    if problem:
        parts.append(f"（配置文件状态：{problem}）")
    if not _ENHANCEMENT_API_KEY:
        parts.append(f"；env 密钥 {_ENHANCEMENT_API_KEY_ENV} 也未设置")
    parts.append("。enhancement 已自动关闭，原图照常交付 —— 这是配置局面，不是失败。")
    return "".join(parts)


def _public_identity(identity: dict[str, str]) -> dict[str, str]:
    """随返回/产物记账的身份形态。永远不含 api_key/base_url 之外的敏感面
    （key 本身绝不落账 —— 对齐 RoleBinding.to_public_dict 的边界）。"""
    return {
        "provider": identity["provider"],
        "model": identity["model"],
        "source": identity["source"],
    }


# ── 保留文字机械注入（移植改动 #1 的落点）─────────────────────────────────


def _preserved_text_block(keep_text: list[str] | None) -> str:
    """把调用方机械抽取的保留文字拼成最终增强 prompt 末尾的权威块。

    这是 code2prompt SECTION 3 的**权威实现**：转换 prompt 已明确告知转译
    模型不要枚举文字（它的 SECTION 3 只剩一句指向本列表的声明），真正的
    清单在这里由代码逐条拼进 —— 不经过任何模型，所以没有"读漏/改写"的
    故障面。推演发现 #5 的事故形状是图像模型把 "400Gbps" 画成别的字样；
    位图文字无法机核，prompt 侧的硬指令是唯一直接的防线。
    """
    items = [str(text) for text in (keep_text or []) if str(text).strip()]
    if not items:
        return ""
    lines = "\n".join(f"- {json.dumps(text, ensure_ascii=False)}" for text in items)
    return f"""
**PRESERVED TEXT — AUTHORITATIVE MACHINE-SUPPLIED LIST (HIGHEST PRIORITY):**
The following {len(items)} text strings are the COMPLETE and AUTHORITATIVE set of labels/annotations that must appear in the final illustration. They are supplied mechanically by the figure pipeline — NOT inferred from the image or the code.

{lines}

Hard requirements:
- Render EVERY string above EXACTLY as written, character-for-character — preserve case, digits, units, punctuation, sub/superscripts and spacing (for example, "400Gbps" must stay exactly "400Gbps").
- Do NOT add, remove, rephrase, translate, split, merge, paraphrase, or restyle any of these strings.
- Do NOT introduce any text beyond this list (no captions, watermarks, signatures, or invented labels).
- If any other instruction or specification conflicts with this list, THIS LIST PREVAILS.
"""


# ── 增强 prompt（AutoFigure 三模式骨架，随源保留英文原文；占位框描述按
#    postprocess SVG 轨约定适配，见模块 docstring 差异 #2）────────────────


def _build_enhancement_prompt(
    style: str,
    enhancement_input: str,
    input_type: str,
    keep_text: list[str] | None = None,
) -> str:
    """按模式拼增强 prompt（移植 AutoFigure `_build_enhancement_prompt`）。

    三种模式共享同一骨架：占位框→图标 + 外部文字精确保留 + art_style 密集
    注入。`keep_text` 非空时，末尾追加权威保留文字块（见
    `_preserved_text_block`），对三种模式一视同仁。
    """
    selected_style = style if style else _DEFAULT_ART_STYLE

    if input_type == "none":
        prompt = f"""**BACKGROUND & PURPOSE:**
You are a world-class scientific illustrator specializing in academic research visualizations. Your task is to transform a plain layout diagram into a professional, publication-ready scientific illustration for academic journals and educational materials.

**ARTISTIC STYLE APPLICATION:**
The entire illustration MUST be rendered in the following artistic style: "{selected_style}"
- Apply this style consistently across all visual elements
- Use colors, textures, and effects that align with "{selected_style}"
- Maintain visual coherence throughout the composition
- Ensure the style enhances readability and professional appearance

**STEP-BY-STEP TRANSFORMATION PROCESS:**

**Step 1: Layout Structure Analysis**
Carefully examine the provided layout diagram (originally authored as SVG markup) and identify:
- Boxes and containers (preserve exact positions and proportions)
- Connecting arrows and flow lines (maintain direction and visual hierarchy)
- Text labels positioned outside placeholder areas
- Colored placeholder boxes - these are icon slots: solid colored rectangular regions, each carrying a short centered label naming the concept it stands for

**Step 2: Understanding Placeholder Structure**
This diagram was rendered from hand-authored SVG. Colored placeholder boxes are icon slots:
- **Identification**: Solid colored rectangular areas containing a short label text
- **Purpose**: These placeholders reserve space and name the concept - they must be replaced with actual icons that visually represent their labels

**Step 3: Text Element Processing**
- **External Text (PRESERVE EXACTLY)**: All text positioned outside placeholder boxes represents final labels, titles, and annotations. These MUST appear in the finished illustration with PERFECT accuracy - do not modify, rephrase, or omit any external text
- **Internal Placeholder Text (CONVERT TO ICONS)**: The short label inside a placeholder box names the concept an icon should depict. Transform each placeholder into an appropriate visual icon, completely removing the placeholder's label text

**Step 4: Visual Element Enhancement**
Transform layout elements following "{selected_style}" specifications:
- **Boxes**: Convert plain shapes into styled containers with appropriate colors, borders, and visual effects matching "{selected_style}"
- **Arrows**: Create elegant directional indicators with consistent styling, appropriate thickness, and visual appeal
- **Connections**: Maintain all spatial relationships while enhancing visual flow

**Step 5: Icon Creation Process**
For each placeholder box:
- Read its short label carefully - this tells you what icon to create
- Design a clear, professional icon that visually represents the named concept
- Apply "{selected_style}" aesthetic to the icon design
- Ensure icons are appropriately sized and positioned within their designated placeholder areas
- Use colors and design elements consistent with the overall style
- The icon should be self-explanatory and convey the concept without needing text

**Step 6: Typography and Text Rendering**
- Render all external text labels with crystal-clear readability
- Use typography that complements "{selected_style}"
- Maintain original text positioning and hierarchy
- Apply consistent font weights and sizes appropriate for the artistic style

**CRITICAL QUALITY REQUIREMENTS:**
- High-resolution output suitable for academic publication (minimum 1200x800 pixels)
- Professional color harmony matching "{selected_style}"
- Perfect structural fidelity to the original layout
- Every placeholder box MUST be replaced with an appropriate icon representing its label
- All icons must be contextually appropriate and visually clear
- Zero placeholder label text inside the icon slots in the final output
- All external labels must be preserved with perfect accuracy
- Consistent visual style across all elements

**OUTPUT SPECIFICATIONS:**
Create a stunning scientific illustration that perfectly balances the "{selected_style}" aesthetic with academic professionalism, ensuring every visual element serves both artistic and informational purposes."""

    elif input_type == "code":
        prompt = f"""**BACKGROUND & PURPOSE:**
You are a world-class scientific illustrator specializing in academic research visualizations. Your task is to transform a plain layout diagram into a professional, publication-ready scientific illustration for academic journals and educational materials.

**ARTISTIC STYLE APPLICATION:**
The entire illustration MUST be rendered in the following artistic style: "{selected_style}"
- Apply this style consistently across all visual elements
- Use colors, textures, and effects that align with "{selected_style}"
- Maintain visual coherence throughout the composition
- Ensure the style enhances readability and professional appearance

**TECHNICAL REFERENCE (Code Structure):**
Use this SVG code to understand the precise layout structure, element relationships, and spatial positioning. Elements carrying a `{ATTR_PLACEHOLDER}` attribute are icon slots; the text associated with each such element names or describes the icon to create:
```svg
{enhancement_input[:_CODE_EXCERPT_CHARS] if enhancement_input else '(no code provided)'}
```

**STEP-BY-STEP TRANSFORMATION PROCESS:**

**Step 1: Code Structure Analysis**
Parse the provided code to identify:
- Exact coordinates and dimensions of all elements
- Text positioning and content hierarchy
- Shape relationships and visual flow patterns
- Color specifications and styling attributes
- Icon slots marked with `{ATTR_PLACEHOLDER}` attributes

**Step 2: Layout Preservation**
Maintain absolute fidelity to:
- Element positions and proportions from code coordinates
- Text placement and alignment specifications
- Arrow paths and connection points
- Overall composition balance and spacing

**Step 3: Text Element Processing**
- **External Text (PRESERVE)**: All text positioned outside placeholder areas represents final labels that must appear with perfect accuracy
- **Internal Placeholder Text (CONVERT)**: Text inside placeholder boxes names the icon to create - transform these placeholders into appropriate visual icons

**Step 4: Style-Conscious Enhancement**
Apply "{selected_style}" while preserving code structural integrity:
- **Color Palette**: Select colors that embody "{selected_style}" aesthetic
- **Visual Effects**: Add appropriate shadows, gradients, or textures consistent with the style
- **Typography**: Choose fonts and text styling that complement "{selected_style}"
- **Icon Design**: Create icons that match both the functional requirements and artistic direction

**Step 5: Professional Icon Creation**
For each placeholder identified in the code:
- Extract the icon name/description associated with the `{ATTR_PLACEHOLDER}` element
- Design contextually appropriate icons in "{selected_style}"
- Ensure icons integrate seamlessly with the overall composition
- Maintain clear visual communication of each concept

**CRITICAL QUALITY REQUIREMENTS:**
- High-resolution output suitable for academic publication
- Perfect adherence to code layout specifications
- Professional color harmony matching "{selected_style}"
- All icons contextually appropriate and visually clear
- Zero placeholder label text inside the icon slots in final output
- Consistent "{selected_style}" aesthetic throughout

**OUTPUT SPECIFICATIONS:**
Create a stunning scientific illustration that perfectly balances the "{selected_style}" aesthetic with the precise structure defined in the code, ensuring every visual element serves both artistic and informational purposes."""

    else:  # code2prompt (text2image_prompt)
        prompt = f"""**BACKGROUND & PURPOSE:**
You are a world-class scientific illustrator specializing in academic research visualizations. Your task is to transform a plain layout diagram into a professional, publication-ready scientific illustration for academic journals and educational materials.

**ARTISTIC STYLE APPLICATION:**
The entire illustration MUST be rendered in the following artistic style: "{selected_style}"
- Apply this style consistently across all visual elements
- Use colors, textures, and effects that align with "{selected_style}"
- Maintain visual coherence throughout the composition
- Ensure the style enhances readability and professional appearance

**COMPREHENSIVE VISUAL SPECIFICATIONS:**
Follow these detailed requirements for creating the enhanced illustration:

```
{enhancement_input if enhancement_input else '(no specifications provided)'}
```

**STEP-BY-STEP EXECUTION PROCESS:**

**Step 1: Specification Analysis**
Carefully review the detailed visual specifications above to understand:
- Overall scene composition and flow patterns
- Specific placeholder-to-icon conversion requirements
- Text elements that must be preserved exactly
- Style-specific implementation guidelines

**Step 2: Layout Structure Implementation**
Based on the specifications:
- Maintain exact spatial relationships as described
- Preserve all text positioning and hierarchy requirements
- Follow the specified color palette and visual characteristics
- Implement the described connection patterns and flow directions

**Step 3: Style-Conscious Icon Development**
For each icon conversion specified:
- Create detailed icons that match both the functional description and "{selected_style}"
- Ensure visual consistency across all icon elements
- Apply appropriate sizing and positioning as specified
- Use colors and effects that harmonize with the overall artistic direction

**Step 4: Typography and Text Rendering**
- Render preserved text elements with crystal-clear readability
- Apply typography styling that complements "{selected_style}"
- Maintain specified text positioning and visual hierarchy
- Ensure perfect accuracy of all preserved text content

**Step 5: Final Style Integration**
Unify all elements under the "{selected_style}" aesthetic:
- Apply consistent visual effects (shadows, gradients, textures)
- Ensure harmonious color relationships throughout
- Balance artistic appeal with professional functionality
- Optimize for academic publication standards

**CRITICAL QUALITY REQUIREMENTS:**
- High-resolution output suitable for academic publication
- Perfect adherence to detailed visual specifications
- Professional implementation of "{selected_style}"
- All icons contextually appropriate and visually stunning
- Zero placeholder label text inside the icon slots in final output
- Seamless integration of all visual elements

**OUTPUT SPECIFICATIONS:**
Create a masterful scientific illustration that flawlessly implements both the detailed visual specifications and the "{selected_style}" artistic direction, resulting in a publication-ready visualization that exceeds professional standards."""

    preserved = _preserved_text_block(keep_text)
    if preserved:
        # 权威保留文字块放在全篇最后：recency 最强的位置，且对三种模式只有
        # 这一个注入点（机械化原则 —— 多处注入 = 多处漂移面）。对 code2prompt
        # 而言它就是 SECTION 3 的权威实现（转换规格里的 SECTION 3 只剩一句
        # "以随规格供给的权威列表为准"的声明）。
        prompt += preserved
    prompt += "\nTransform the provided layout into this enhanced visualization now."
    return prompt


# ── code2prompt：SVG 代码 → 五节视觉规格（移植 AutoFigure
#    `convert_code_to_text2image_prompt`；SECTION 3 改为"由管线机械供给"）──


def _conversion_prompt(svg_text: str, art_style: str) -> str:
    """转译 prompt（随源保留英文骨架；code_format 固定为 svg）。

    与原版的三处有意差异：
      1. 分析要点改为认 `data-placeholder` 等 DOM 标记（postprocess SVG 轨
         的机械化约定，词表见 svg_vocab.py），不再是 mxGraph 灰色矩形；
      2. SECTION 3 明确要求**不要**从代码枚举文字 —— 权威保留文字列表由
         管线在最终增强 prompt 里机械拼接（移植改动 #1）；
      3. content_type 固定为 scientific（postprocess 的 genre 差异在 skill
         层，不进 enhancer 接口）。
    """
    selected_style = art_style if art_style else _DEFAULT_ART_STYLE
    return f"""You are an expert visual design analyst specializing in converting technical code into detailed text-to-image prompts. Your task is to analyze the provided SVG code that defines vector graphics and create a comprehensive prompt that will guide AI image generation to produce a professional, visually stunning scientific illustration.

**PRIMARY OBJECTIVE:**
Create a text-to-image prompt that will transform the programmatic layout defined in the SVG code into a beautiful visual illustration while maintaining perfect layout structure and applying the specified artistic style: "{selected_style}".

**ARTISTIC STYLE INTEGRATION:**
The final illustration MUST strictly follow this artistic style: "{selected_style}"
- All visual elements, colors, effects, and overall aesthetic must align with this style
- Icons and visual components should be designed to match this artistic direction
- Color palette, typography, and visual effects should complement this style
- The overall composition should embody the essence of "{selected_style}"

**SVG CODE ANALYSIS FOCUS:**
- Parse the SVG structure including groups, paths, shapes, and text elements
- Identify icon slots by their `{ATTR_PLACEHOLDER}` attribute; the text associated with each such element (its own text content or a nearby label) names or describes the icon that should replace it
- Identify node shapes (`data-role` / `data-node` attributes) and connectors (`data-edge-from` / `data-edge-to` attributes) when present
- Extract positioning, sizing, and styling information
- Understand the visual hierarchy, flow direction, and connections
- Convert vector graphics into visual descriptions

**CRITICAL ANALYSIS STEPS:**

1. **Code Structure Analysis:**
   - Parse the SVG code to understand the intended visual layout
   - Identify all visual elements defined in the code
   - Extract positioning, sizing, and styling information
   - Understand the overall composition and flow

2. **Visual Element Identification:**
   - Locate all shapes, text elements, and containers
   - Identify placeholder areas that need icon conversion
   - Document spatial relationships between elements
   - Note any special styling or effects

3. **Style-Specific Visual Enhancement Requirements:**
   - Apply "{selected_style}" consistently throughout the design
   - Define color schemes that match the specified artistic style
   - Specify visual hierarchy and emphasis appropriate for the style
   - Describe background treatment that complements the artistic direction

**OUTPUT FORMAT REQUIREMENTS:**

Your response must include these EXACT sections:

**SECTION 1: OVERALL SCENE DESCRIPTION**
"A professional scientific illustration based on SVG code structure. The illustration should be rendered in the '{selected_style}' style with [style-appropriate color palette and visual characteristics]. The layout follows [describe flow pattern from code analysis]. The overall aesthetic perfectly embodies the '{selected_style}' artistic direction."

**SECTION 2: VISUAL ELEMENTS FROM CODE**
For each visual element found in the code, provide:
"Element [description from code]: Render as [VERY SPECIFIC visual description that incorporates '{selected_style}' style elements]. The element should be [size and position from code], styled in '{selected_style}' with [specific style characteristics: colors, effects, textures, etc.]. It represents [concept] and should visually communicate [specific meaning] while perfectly matching the '{selected_style}' aesthetic."

**SECTION 3: TEXT ELEMENTS TO PRESERVE**
Do NOT enumerate, quote, or paraphrase any text content from the code in this section. The complete and authoritative list of text strings that must be preserved is supplied separately by the pipeline together with the final enhancement prompt. For this section write exactly one sentence: "The authoritative preserved-text list is supplied alongside this specification; every string in it must appear verbatim, with no additions, removals, or alterations."

**SECTION 4: ARTISTIC STYLE IMPLEMENTATION**
"The entire illustration must be rendered in the '{selected_style}' style. Specific implementation requirements:
- Color Palette: [define colors that match the artistic style]
- Visual Effects: [specify effects appropriate for the style: shadows, gradients, textures, etc.]
- Typography: [describe text styling that complements the artistic direction]
- Overall Aesthetic: [detailed description of how the '{selected_style}' should be applied]
- Visual Characteristics: [specific visual elements that define this artistic style]"

**SECTION 5: LAYOUT AND CONNECTIONS**
"Maintain these exact spatial relationships from the SVG code: [describe arrangement]. Connect elements with [connection specifications styled to match '{selected_style}']. Ensure [spacing and alignment from code]. All elements should be rendered in '{selected_style}' style."

**CRITICAL SUCCESS FACTORS:**
- Every programmatic element MUST be converted to a specific, implementable visual description that matches '{selected_style}'
- Layout structure from the SVG code MUST be maintained exactly
- The '{selected_style}' style MUST be consistently applied throughout all visual elements
- Style specifications MUST be detailed enough for consistent application
- The prompt MUST be actionable for AI image generation in the specified artistic style

**INPUT SVG CODE:**
```svg
{svg_text}
```

**ARTISTIC STYLE TO APPLY:** "{selected_style}"

Now analyze this SVG code and create the comprehensive text-to-image prompt following the exact format above. Focus especially on converting every programmatic element into a specific, detailed visual description that perfectly matches the "{selected_style}" artistic style while maintaining visual clarity and professional quality."""


async def convert_code_to_text2image_prompt(
    svg_text: str,
    art_style: str = "",
    *,
    timeout_s: float = 120.0,
) -> dict[str, Any]:
    """SVG 代码 → 五节视觉规格，经框架 reasoning LLM（core.llm.LLMClient）。

    返回 {"ok": True, "text": 规格, "usage": {...}, "elapsed_s": ...}
    或     {"ok": False, "reason": 分类原因, "attempt": {...}}。

    失败**不抛**（取消信号除外 —— RunCancelled/CancelledError 是
    BaseException，自然穿透）：拿不到文本 LLM 或转译失败时，调用方把
    code2prompt 降级为 code 模式并如实记 mode_fallback，enhancement 不因此
    整体失败（有 SVG 源码在场，code 模式仍有完整布局信息）。
    """
    from core import model_roles

    attempt: dict[str, Any] = {"stage": "code2prompt_conversion"}
    started = time.monotonic()

    try:
        binding = model_roles.resolve(model_roles.REASONING_ROLE)
    except model_roles.ModelRoleError as exc:
        attempt.update(ok=False, error_class="role_catalog_error",
                       detail=str(exc)[:300])
        return {"ok": False, "reason": f"reasoning_role_catalog_error: {exc}",
                "attempt": attempt}
    if binding is None:
        attempt.update(ok=False, error_class="role_not_configured",
                       detail="model role 'reasoning' is not bound")
        return {"ok": False, "reason": "reasoning_role_unavailable",
                "attempt": attempt}

    from core.llm import LLMClient, LLMMessage

    config, _problem = _load_config()
    defaults = config.get("defaults") if isinstance(config.get("defaults"), dict) else {}
    max_tokens = int(defaults.get("conversion_max_tokens") or _CONVERSION_MAX_TOKENS)

    client = LLMClient(timeout=timeout_s)
    try:
        response = await client.chat(
            [LLMMessage(role="user", content=_conversion_prompt(svg_text, art_style))],
            max_tokens=max_tokens,
            temperature=0.7,
        )
    except model_roles.ModelRoleUnavailable as exc:
        attempt.update(ok=False, error_class="role_not_configured",
                       detail=str(exc)[:300],
                       elapsed_s=round(time.monotonic() - started, 2))
        return {"ok": False, "reason": "reasoning_role_unavailable",
                "attempt": attempt}
    except Exception as exc:
        # 网络抖动/超时/5xx 等：LLMClient 已按它的重试预算扛过一轮，到这里
        # 就是真失败。如实分类，交给调用方降级，不外抛。
        attempt.update(ok=False, error_class="conversion_call_failed",
                       detail=f"{type(exc).__name__}: {str(exc)[:300]}",
                       elapsed_s=round(time.monotonic() - started, 2))
        return {"ok": False,
                "reason": f"conversion_failed: {type(exc).__name__}: {str(exc)[:200]}",
                "attempt": attempt}

    text = (response.content or "").strip()
    attempt["usage"] = response.usage or {}
    attempt["elapsed_s"] = round(time.monotonic() - started, 2)
    if not text:
        attempt.update(ok=False, error_class="conversion_empty_response")
        return {"ok": False, "reason": "conversion_empty_response",
                "attempt": attempt}
    attempt["ok"] = True
    return {"ok": True, "text": text, "usage": response.usage or {},
            "elapsed_s": attempt["elapsed_s"], "attempt": attempt}


# ── 图像生成调用与响应提取 ────────────────────────────────────────────────


def _is_png(data: bytes) -> bool:
    """标准库验 PNG 头 8 字节 magic。不 import PIL —— 部署环境的 PIL 可能
    是坏的（验字节也不需要图像库，magic 判据比"PIL 能打开"更便宜更确定）。"""
    return data[:8] == _PNG_MAGIC


def _extract_image_bytes(result: dict[str, Any]) -> bytes | None:
    """从 OpenAI 兼容图像响应里提取图像字节（移植 AutoFigure 的四个分支）。

    分支顺序与原版一致：
      1. message.images[].image_url.url（OpenRouter 形态的 data URL）
      2. message.content 里的 markdown data URL（bianxie 形态）
      3. message.content 列表里的 image_url / data 字段
      4. 顶层 data[].b64_json（DALL-E 风格 images API 形态）
    原版还有一条"提取不到就复制原图"的兜底 —— **故意不搬**（见模块
    docstring 差异 #3）：提取失败要如实地是失败。
    """
    choices = result.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}

        images = message.get("images")
        if isinstance(images, list):
            for img in images:
                if not isinstance(img, dict):
                    continue
                url = str((img.get("image_url") or {}).get("url") or "")
                if url.startswith("data:image/") and "," in url:
                    try:
                        return base64.b64decode(url.split(",", 1)[1])
                    except Exception:
                        continue

        content = message.get("content")
        if isinstance(content, str) and "data:image/" in content:
            match = _DATA_URL_RE.search(content)
            if match:
                try:
                    return base64.b64decode(re.sub(r"\s+", "", match.group(2)))
                except Exception:
                    pass
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "image_url":
                    url = str((part.get("image_url") or {}).get("url") or "")
                    if url.startswith("data:image/") and "," in url:
                        try:
                            return base64.b64decode(url.split(",", 1)[1])
                        except Exception:
                            continue
                data = part.get("data")
                if isinstance(data, str) and data:
                    try:
                        return base64.b64decode(data)
                    except Exception:
                        continue

    data_list = result.get("data")
    if isinstance(data_list, list):
        for item in data_list:
            if isinstance(item, dict) and isinstance(item.get("b64_json"), str):
                try:
                    return base64.b64decode(item["b64_json"])
                except Exception:
                    continue
    return None


#: markdown 形态的 data URL（AutoFigure `_save_base64_image_from_markdown` 同款，
#: 两组结构一致：group(1)=格式，group(2)=base64 载荷）。与原版的一处差异：
#: 载荷字符类容忍 \s —— provider 的 markdown 输出会把长 base64 折行，严格
#: 失配会把"其实有图"截成前缀字节（截断的 PNG 头 8 字节仍是合法 magic，
#: 那才是最坏的静默损坏）。解码前剥掉全部空白。
_DATA_URL_RE = re.compile(
    r"data:image/(png|jpeg|jpg|webp);base64,([A-Za-z0-9+/=\s]+)"
)


def _write_debug_response(out_dir: Path, stem: str, result: Any) -> str | None:
    """提取失败时把 provider 原始响应落盘供归因（AutoFigure 原版同款动作，
    加体积上限：响应里可能躺着巨大的 base64 blob）。失败不抛 —— 调试信息
    的落盘失败不能盖掉主错误分类。
    """
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps(result, ensure_ascii=False, default=str)
        if len(text) > _DEBUG_RESPONSE_MAX_CHARS:
            text = text[:_DEBUG_RESPONSE_MAX_CHARS] + "…[truncated]"
        path = out_dir / f"{stem}_enhanced_response_debug.json"
        path.write_text(text, encoding="utf-8")
        return str(path)
    except OSError as exc:
        log.warning("增强响应调试信息落盘失败：%s", exc)
        return None


async def _generate_one_image(
    *,
    identity: dict[str, str],
    png_path: Path,
    prompt: str,
    timeout_s: float,
    out_dir: Path,
) -> dict[str, Any]:
    """一次图像生成调用。返回 {"ok", "image_bytes", "usage", "attempt"}
    或 {"ok": False, "error", "attempt"}。失败分类进 attempt，不外抛。

    错误分类（任务要求的三类 + 两个细分）：
      - timeout            墙钟超时（asyncio.wait_for / httpx timeout）
      - transport_error    连接层错误（DNS/握手/断流，httpx.HTTPError 非超时）
      - http_error         provider 回了 HTTP >= 400（状态码 + 截断正文入帐；
                           **不记请求头** —— Authorization 不进日志，对齐
                           core/llm.py parse_provider_error 的边界）
      - bad_response_json  HTTP 200 但响应不是合法 JSON
      - no_image_in_response  合法 JSON 但四个提取分支都拿不到图
      - not_png            提取到的字节不是 PNG（magic 校验失败）
    """
    attempt: dict[str, Any] = {
        "stage": "image_generation",
        "provider": identity["provider"],
        "model": identity["model"],
    }
    started = time.monotonic()

    url = identity["base_url"].rstrip("/")
    if not url.endswith("/chat/completions"):
        url += "/chat/completions"

    data_url = "data:image/png;base64," + base64.b64encode(
        png_path.read_bytes()).decode("ascii")
    payload: dict[str, Any] = {
        "model": identity["model"],
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }
    # OpenRouter 必须显式声明图像输出模态（AutoFigure 原版同款分支）；其余
    # provider 的怪癖经 enhancer_config.yaml 的 payload_extra 原样并入。
    if identity["provider"].strip().lower() == "openrouter":
        payload["modalities"] = ["image", "text"]
    config, _problem = _load_config()
    extra = config.get("payload_extra")
    if isinstance(extra, dict):
        payload.update(extra)

    headers = {"Content-Type": "application/json"}
    if identity["api_key"]:
        # 没有 key 就**不发** Authorization 头（LLMClient._request_headers 同款
        # 理由：空 Bearer 在部分网关上被当成坏 key 而 401，缺席就让它缺席）。
        headers["Authorization"] = f"Bearer {identity['api_key']}"

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout_s)) as client:
            # 墙钟闸在 httpx 自己的超时之外再罩一层：timeout_s 是"这次调用
            # 总共多久"的承诺（成本闸 §4.5：单次 ≤120s），不是某个 IO 阶段的。
            response = await asyncio.wait_for(
                client.post(url, headers=headers, json=payload),
                timeout=timeout_s,
            )
    except (asyncio.TimeoutError, httpx.TimeoutException) as exc:
        attempt.update(ok=False, error_class="timeout",
                       detail=f"{type(exc).__name__} after {timeout_s:.0f}s wall clock",
                       elapsed_s=round(time.monotonic() - started, 2))
        return {"ok": False, "error": f"timeout after {timeout_s:.0f}s",
                "attempt": attempt}
    except httpx.HTTPError as exc:
        attempt.update(ok=False, error_class="transport_error",
                       detail=f"{type(exc).__name__}: {str(exc)[:300]}",
                       elapsed_s=round(time.monotonic() - started, 2))
        return {"ok": False,
                "error": f"transport_error: {type(exc).__name__}: {str(exc)[:200]}",
                "attempt": attempt}

    attempt["elapsed_s"] = round(time.monotonic() - started, 2)

    if response.status_code >= 400:
        body = response.text[:500]
        attempt.update(ok=False, error_class="http_error",
                       http_status=response.status_code, detail=body)
        return {"ok": False,
                "error": f"http_error: HTTP {response.status_code}: {body[:200]}",
                "attempt": attempt}

    try:
        result = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        attempt.update(ok=False, error_class="bad_response_json",
                       detail=str(exc)[:300])
        return {"ok": False, "error": f"bad_response_json: {str(exc)[:200]}",
                "attempt": attempt}

    attempt["usage"] = result.get("usage") or {}
    image_bytes = _extract_image_bytes(result)
    if image_bytes is None:
        debug_path = _write_debug_response(out_dir, png_path.stem, result)
        attempt.update(ok=False, error_class="no_image_in_response",
                       detail="四个提取分支都拿不到图像字节",
                       **({"debug_response": debug_path} if debug_path else {}))
        return {"ok": False,
                "error": "no_image_in_response: provider 回了合法 JSON 但响应里没有图像",
                "attempt": attempt}

    if not _is_png(image_bytes):
        # 提取到了**东西**但不是 PNG —— 照样是失败，不揣测着改写格式
        # （写一个叫 .png 的 JPEG 是给下游埋雷）。
        attempt.update(ok=False, error_class="not_png",
                       detail=f"magic bytes: {image_bytes[:8].hex()}")
        return {"ok": False,
                "error": f"not_png: 响应里的图像不是 PNG（magic={image_bytes[:4].hex()}…）",
                "attempt": attempt}

    attempt["ok"] = True
    return {"ok": True, "image_bytes": image_bytes,
            "usage": result.get("usage") or {}, "attempt": attempt}


def _next_variant_path(out_dir: Path, stem: str) -> Path:
    """{stem}_enhanced_{i}.png，i 取第一个不存在的序号 —— 调用方逐张调，
    序号由目录现状机械推出，不覆盖已有变体（重写别人的产物 = 销毁证据）。"""
    index = 1
    while (out_dir / f"{stem}_enhanced_{index}.png").exists():
        index += 1
    return out_dir / f"{stem}_enhanced_{index}.png"


# ── 对外主入口 ────────────────────────────────────────────────────────────


async def enhance(state, *, png_path: Path, svg_text: str | None, mode: str,
                  art_style: str | None, out_dir: Path, keep_text: list[str],
                  timeout_s: float = 120.0) -> dict:
    """一次增强：读已铸记录的 PNG + 冻结 SVG，逐张生成变体。
    mode ∈ {"none","code","code2prompt"}（默认 code2prompt）。
    返回：
      成功 {"status":"success","variants":[{"path":str,"bytes":int}],"usage":{...},"identity":{provider,model,source}}
      未配置 {"status":"not_configured","reason":...}
      失败 {"status":"error","error":...,"attempts":[...]}
    """
    started = time.monotonic()
    attempts: list[dict[str, Any]] = []

    # mode 词表核查在身份解析之前：调用方传错词是调用方的事，跟配没配置无关，
    # 不该被报成 not_configured。
    requested_mode = (mode or DEFAULT_MODE).strip()
    if requested_mode not in MODES:
        return {
            "status": "error",
            "error": f"unknown_mode: {requested_mode!r}（合法取值：{', '.join(MODES)}）",
            "attempts": [],
        }

    identity = enhancement_identity()
    if identity is None:
        return {"status": "not_configured", "reason": _not_configured_reason()}

    png_path = Path(png_path)
    if not png_path.exists():
        return {
            "status": "error",
            "error": f"input_png_missing: {png_path}",
            "attempts": [],
            "identity": _public_identity(identity),
        }

    # ── code2prompt：先转译成五节视觉规格；拿不到文本 LLM / 转译失败 ──────
    # ── 降级为 code 模式（有 SVG 在场时它仍携带完整布局信息），如实记账 ──
    effective_mode = requested_mode
    mode_fallback: dict[str, str] | None = None
    enhancement_input = ""
    if requested_mode == "code2prompt":
        if not (svg_text and svg_text.strip()):
            # 没有 SVG 可转译：code 模式也没料，直接落到 none（AutoFigure
            # 的 agent 层在 SVG 读失败时就是这么降的）。
            effective_mode = "none"
            mode_fallback = {"from": "code2prompt", "to": "none",
                             "reason": "no_svg_text"}
        else:
            conversion = await convert_code_to_text2image_prompt(
                svg_text, art_style or "", timeout_s=timeout_s)
            attempts.append(conversion["attempt"])
            if conversion["ok"]:
                enhancement_input = conversion["text"]
            else:
                effective_mode = "code"
                mode_fallback = {"from": "code2prompt", "to": "code",
                                 "reason": conversion["reason"]}
    if effective_mode == "code":
        enhancement_input = svg_text or ""

    prompt = _build_enhancement_prompt(
        art_style or "", enhancement_input, effective_mode, keep_text)

    generation = await _generate_one_image(
        identity=identity, png_path=png_path, prompt=prompt,
        timeout_s=timeout_s, out_dir=Path(out_dir))
    attempts.append(generation["attempt"])

    base: dict[str, Any] = {
        "mode": effective_mode,
        "identity": _public_identity(identity),
        "elapsed_s": round(time.monotonic() - started, 2),
    }
    if mode_fallback:
        base["mode_fallback"] = mode_fallback

    if not generation["ok"]:
        return {
            "status": "error",
            "error": generation["error"],
            "attempts": attempts,
            **base,
        }

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = _next_variant_path(out_dir, png_path.stem)
    image_bytes = generation["image_bytes"]
    target.write_bytes(image_bytes)

    usage: dict[str, Any] = {}
    conversion_usage = next(
        (a.get("usage") for a in attempts
         if a.get("stage") == "code2prompt_conversion" and a.get("usage")),
        None,
    )
    if conversion_usage:
        usage["code2prompt_conversion"] = conversion_usage
    # 图像 provider 不给 usage 是常态 —— 拿不到就如实缺席，不编零。
    if generation.get("usage"):
        usage["image_generation"] = generation["usage"]

    return {
        "status": "success",
        "variants": [{"path": str(target), "bytes": len(image_bytes)}],
        "usage": usage,
        **base,
    }
