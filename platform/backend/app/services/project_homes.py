"""每个项目住在哪。

## 一个项目只有一个家

本机，或者某一个组织 —— 出生那一刻挑，之后只有"搬家"一个动作，没有"同步"
（`RFC_ORGANISATION_IS_A_PROJECT_HOME_20260922` A2）。两处各存一份就是两个真相源，
这个仓库为此付过足够多的学费（`feedback_one_truth_source_per_question`）。

## 不在登记里 = 住本机

登记只记"住在别处的那些"。本机项目本来就在本机的库里，再抄一份"它住本机"等于
给同一个事实找第二个出处：那份抄件一旦和库分叉（删了项目忘了删登记、或反过来），
没有任何一层会发现。

住在组织里的项目**不进本机的库** —— 它的记录、工作区、会话都在那台服务器上。
本机只需要知道"这个 id 要去问谁"，所以登记里一个项目只有一行：id → 连接 id。
"""
from __future__ import annotations

import json
from pathlib import Path

from app.config import settings


def the_same_project(project_id: str) -> str:
    """一个项目 id 的规范写法。

    项目 id 是 UUID，而 UUID 有两种拼法：`07e872a6-4787-…`（API 交出来的）和
    `07e872a647874…`（SQLAlchemy 存进 SQLite 的）。登记按一种记、查的时候来了另一种
    —— 那一查落空，请求就静静地落到本机，本机诚实地答"没这个项目"。**两种拼法就是
    两把钥匙**，而配不上的那次不报错，只是那个项目"不见了"。

    所以进出这张表的每一个 id 都先过这里。不像 UUID 的（将来换一种 id）原样留着。
    """
    cleaned = project_id.strip().lower()
    bare = cleaned.replace("-", "")
    if len(bare) == 32 and all(c in "0123456789abcdef" for c in bare):
        return bare
    return cleaned


def _store() -> Path:
    return Path(settings.platform_data_root).expanduser() / "project-homes.json"


def _read() -> dict[str, str]:
    path = _store()
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    homes = payload.get("homes") if isinstance(payload, dict) else None
    if not isinstance(homes, dict):
        return {}
    return {the_same_project(str(k)): str(v) for k, v in homes.items() if k and v}


def _write(homes: dict[str, str]) -> None:
    path = _store()
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(".writing")
    scratch.write_text(
        json.dumps({"homes": homes}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    scratch.replace(path)


def where_this_project_lives(project_id: str) -> str:
    """哪条连接 —— 空串 = 本机。"""
    return _read().get(the_same_project(project_id), "")


def this_one_lives_at(project_id: str, connection_id: str) -> None:
    homes = _read()
    homes[the_same_project(project_id)] = connection_id
    _write(homes)


def these_live_at(connection_id: str, project_ids: list[str]) -> None:
    """那台服务器刚提到了它上面的这些项目 —— 记下来（已记着的照旧）。

    **只加不删**（2026-09-24 改）。从前它跟着「这一份清单」走：清单里不再有的，本机也不再认。
    可「住在哪」是项目的事实，不是哪份清单的事实：侧栏的清单只有「我的项目」，组织页的清单是
    「组里看得见的」—— 按侧栏那份删，就把组织页刚记下的别人的项目删掉了，「看看」随即落到本机、
    本机答「没这个项目」。一条过期的记录（那边删了项目）只会把请求送去那台服务器，它照实答 404，
    和本机答的一样；真要忘掉一个组织的全部，是退出它（`forget_everything_at`）。
    """
    homes = _read()
    for project_id in project_ids:
        homes[the_same_project(project_id)] = connection_id
    _write(homes)


def forget_everything_at(connection_id: str) -> None:
    """退出了这个组织 —— 它名下的项目不再由这台桌面代问。"""
    _write({k: v for k, v in _read().items() if v != connection_id})


def the_job_ledger_of(project_id: str) -> Path:
    """一个项目的作业登记表在哪 —— 一处：项目的家（`config.the_projects_home`）。

    从前一个项目几个人各开会话就有几份（每人 home 底下一份），得一个个找出来拼；
    谁跑的也是按「账本在谁的 home 里」推的。现在项目层一个项目一份
    （`docs/RFC_PROJECT_HOME_20260924.md`），谁跑的写在记录上（`by_user_id`）。
    """
    from app.config import the_projects_home

    return the_projects_home() / str(project_id)
