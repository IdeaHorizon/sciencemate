"""这份安装是哪种发行：personal / pro。

## 为什么是"安装"的属性，不是载荷或档位的属性

两种发行（EXEC_PLAN_TWO_EDITIONS §1）只差两个烧进包里的值：`edition` 与自更新源。
它们**不能**住在任何会被自更新替换的东西里 —— 0.4.6 起更新会换掉载荷、壳和后端
本体，住在那里面就意味着装一次更新可能把一份专业版变回个人版、或把个人版指向一个
它没权限的私有仓库（拉不到更新且界面上什么都不显示）。所以它们住在打包器写进
bundle 的 `edition.json` 里，锚在跑着我们那个解释器旁边：`sys.prefix` 是
`Resources/python`，文件在 `Resources/edition.json` —— 与 `launcher._bundled_binary`
同一个锚，不从 `__file__` 往上数目录。

它也不是档位。档位（personal / org）说的是这台服务器**装配成了什么形态**；发行说
的是这份**安装**是哪一种 —— 一份专业版桌面包，既能以个人档跑在本机，也能连到一台
以组织档跑着的服务器。

## 读不到就是 personal

源码 checkout 里没有这个文件（venv 的 `sys.prefix.parent` 是 backend/）；0.5.0 之前
装出去的个人版包也没有。缺省是 personal：少画一个入口的方向是安全的，反过来会在
一份个人版上画出「连接组织服务器」，而它的更新源还指错。文件在但坏了同样按
personal，并把原因交给装配层去写日志 —— 不在这里吞掉。

## 谁可以读

只有 `app/assembly.py`（接线）。业务代码看能力，不看发行 —— 同档位那条规矩
（RFC_RESEARCH_BUDDY R2），`tests/test_the_edition_only_lives_in_the_assembly.py`
机械地守着。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

EDITIONS = ("personal", "pro")
FILENAME = "edition.json"


def edition_file() -> Path:
    """打包器把它写在随包解释器旁边：`<sys.prefix>/../edition.json`。"""
    return Path(sys.prefix).parent / FILENAME


@dataclass(frozen=True)
class Edition:
    name: str
    #: 打包器烧进来的自更新源（仓库网址或 manifest.json 的 URL）。空 = 用代码里的出厂值。
    update_source: str = ""
    #: 文件在但读不懂时的原因；正常为空。
    problem: str = ""


def read_edition(path: Path | None = None) -> Edition:
    """读 `edition.json`。任何读不到、读不懂的情况都是 personal —— fail-closed。"""
    target = path or edition_file()
    if not target.is_file():
        return Edition("personal")
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Edition("personal", problem=f"{target}: {exc.__class__.__name__}: {exc}")
    if not isinstance(raw, dict) or raw.get("edition") not in EDITIONS:
        return Edition("personal", problem=f"{target}: edition 不是 {'/'.join(EDITIONS)} 之一")
    source = raw.get("update_source")
    return Edition(str(raw["edition"]), str(source).strip() if isinstance(source, str) else "")


def write_edition(directory: Path, name: str, update_source: str = "") -> Path:
    """打包器用：把发行写进 bundle。读它的是上面的 `read_edition`，格式只在这一个文件里定。"""
    if name not in EDITIONS:
        raise ValueError(f"edition 必须是 {'/'.join(EDITIONS)} 之一，不是 {name!r}")
    if name == "pro" and not update_source.strip():
        raise ValueError("专业版必须烧进自更新源：它的更新不在公开仓库")
    target = directory / FILENAME
    target.write_text(
        json.dumps({"edition": name, "update_source": update_source.strip()},
                   ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target
