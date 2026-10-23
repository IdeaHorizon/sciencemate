"""模型角色目录 —— 平台侧读取器。

目录的**权威在 harness**（`$HARNESS_ROOT/shared/model_roles.yaml`），因为角色
是由消费方定义的：需要"一个能审图的模型"的是 postprocess 节点，不是平台。
平台读同一个文件，不另存一份 —— 一个问题有几份抄件，就有几个会各自演化的
答案（此前 provider 词表就是这么在 Python 和 TypeScript 各活了一份，靠一条
测试钉着）。

前端也不再自己枚举：它从 API 拿这份目录渲染。所以从 yaml 到设置页，全程
只有一份。

## 失败方向

读不到（HARNESS_ROOT 没配 / 文件缺失 / YAML 坏了）→ **报错，不返回空列表**。
空列表长得像"这套部署没有任何角色"，会让人在设置页里找一个根本没渲染出来
的开关；错误则会指着 HARNESS_ROOT 说话。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from app.config import settings
from app.services.say import SaidToAPerson

if TYPE_CHECKING:
    # 只要类型：app.models.model_backend 在导入期就读这个模块，而 user_interface
    # 又导入 app.models.user —— 运行期导入会绕成一个圈。
    from app.services.user_interface import Language

CATALOG_RELATIVE_PATH = "shared/model_roles.yaml"
REASONING_ROLE = "reasoning"


class ModelRoleCatalogError(SaidToAPerson, RuntimeError):
    """目录读不到 —— 部署问题，不是用户没配置。『设置 → 模型』原话显示（503），两种语言都写。"""


@dataclass(frozen=True)
class RoleSpec:
    id: str
    title: str
    description: str = ""
    modality: str = "text"
    required: bool = False
    #: 写给**消费方节点**的后果与出路（"你什么都不用做"）。
    absence_note: str = ""
    #: 同一件事写给**人**：设置页上"这个槽空着我会少什么"。两句话的读者相反
    #: —— 一个是被拒绝的节点，一个是唯一能把槽填上的人 —— 所以是两个字段。
    absence_impact: str = ""
    #: 上面三句说给人听的话的英文。设置页跟着界面语言挑；absence_note 是给节点的，
    #: 不翻。
    title_en: str = ""
    description_en: str = ""
    absence_impact_en: str = ""

    @property
    def needs_vision(self) -> bool:
        return self.modality == "vision"

    def to_public_dict(self, lang: Language) -> dict:
        """设置页读的那份投影 —— 说给人听的三句按界面语言给。

        `lang` 不给默认值：从前这里只有中文，英文界面的「模型角色」一节整段是
        「主推理模型」「图像审查模型（VLM）」。默认值会把「调用方忘了传」安静地
        变回那个样子。英文缺了才退回中文（目录有测试逐个角色核对，不该发生）。
        """
        english = lang == "en"
        return {
            "id": self.id,
            "title": (self.title_en if english else "") or self.title,
            "description": (self.description_en if english else "") or self.description,
            "modality": self.modality,
            "required": self.required,
            "absence_note": self.absence_note,
            "absence_impact": (self.absence_impact_en if english else "") or self.absence_impact,
        }


def catalog_path() -> Path:
    root = (settings.harness_root or "").strip()
    if not root:
        raise ModelRoleCatalogError(
            "HARNESS_ROOT 未配置 —— 读不到模型角色目录，"
            "『设置 → 模型』的角色指派无法渲染。",
            en="HARNESS_ROOT is not set — the model role catalog cannot be read, so the role "
               "assignments under Settings → Models cannot be shown.",
        )
    return Path(root).expanduser() / CATALOG_RELATIVE_PATH


@lru_cache(maxsize=8)
def _load(path_str: str, mtime_ns: int) -> tuple[RoleSpec, ...]:
    """按 (路径, mtime) 缓存：改了 yaml 不用重启后端，改前的解析结果不复用。"""
    try:
        raw = yaml.safe_load(Path(path_str).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ModelRoleCatalogError(f"模型角色目录读不到：{path_str} —— {exc}",
                                    en=f"The model role catalog cannot be read: {path_str} — {exc}") from exc
    except yaml.YAMLError as exc:
        raise ModelRoleCatalogError(f"模型角色目录不是合法 YAML：{path_str} —— {exc}",
                                    en=f"The model role catalog is not valid YAML: {path_str} — {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("roles"), list):
        raise ModelRoleCatalogError(f"模型角色目录缺 roles 列表：{path_str}",
                                    en=f"The model role catalog has no roles list: {path_str}")
    specs: list[RoleSpec] = []
    for item in raw["roles"]:
        if not isinstance(item, dict) or not str(item.get("id") or "").strip():
            raise ModelRoleCatalogError(f"模型角色目录里有一条缺 id：{item!r}",
                                        en=f"An entry in the model role catalog has no id: {item!r}")
        specs.append(
            RoleSpec(
                id=str(item["id"]).strip(),
                title=str(item.get("title") or item["id"]).strip(),
                description=" ".join(str(item.get("description") or "").split()),
                modality=str(item.get("modality") or "text").strip(),
                required=bool(item.get("required")),
                absence_note=" ".join(str(item.get("absence_note") or "").split()),
                absence_impact=" ".join(str(item.get("absence_impact") or "").split()),
                title_en=str(item.get("title_en") or "").strip(),
                description_en=" ".join(str(item.get("description_en") or "").split()),
                absence_impact_en=" ".join(str(item.get("absence_impact_en") or "").split()),
            )
        )
    return tuple(specs)


def catalog() -> tuple[RoleSpec, ...]:
    path = catalog_path()
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError as exc:
        raise ModelRoleCatalogError(
            f"模型角色目录读不到：{path} —— {exc}（HARNESS_ROOT 指对了吗？）",
            en=f"The model role catalog cannot be read: {path} — {exc} (is HARNESS_ROOT pointing at "
               "the right place?)",
        ) from exc
    return _load(str(path), mtime_ns)


def role_ids() -> frozenset[str]:
    return frozenset(item.id for item in catalog())


def spec(role_id: str) -> RoleSpec | None:
    return next((item for item in catalog() if item.id == role_id), None)
