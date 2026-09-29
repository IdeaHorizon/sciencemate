"""配置读回视图不许悄悄漏字段。

## 现场（2026-08-11）

`autonomous_authorized_risk_classes`（无人值守预授权哪些高危类别）——
`PATCH` 写进去了、库里确实存着，但 `GET /projects/{id}` 永远返回 `None`。
因为 `ProjectConfigResponse` 是一份**手写字段名单**，新增配置项不加进去就
静默看不见。

**授权范围看不见，比不能设置更糟**：设不了会立刻发现；看不见会一直以为
自己没授权。安全相关的设置尤其不能这样。

## 判据：扫模型，不写名单

这正是"护栏要扫盘，不要写名单"的同款 —— 只不过这次漏过的不是危险操作，
是**配置项**。用模型的列做真相源，漏了直接红，不靠人记得。
"""
from __future__ import annotations

from app.models.project import ProjectConfig
from app.schemas.project import ProjectConfigResponse

#: 有意不暴露的列（主键 / 外键 / 审计时间戳这类，不是配置项）。
_NOT_CONFIGURATION = {"id", "project_id", "project"}


def test_project_config_response_covers_the_model() -> None:
    model_columns = {c.name for c in ProjectConfig.__table__.columns} - _NOT_CONFIGURATION
    exposed = set(ProjectConfigResponse.model_fields)
    missing = sorted(model_columns - exposed)
    assert not missing, (
        "这些配置项存得进去、读不回来（GET 里静默消失）：\n  "
        + "\n  ".join(missing)
        + "\n加配置项时 ProjectConfigResponse 必须一起加。"
    )


def test_the_authorization_scope_is_readable() -> None:
    """单独钉这一条：它是安全相关的，看不见的代价最大。"""
    assert "autonomous_authorized_risk_classes" in ProjectConfigResponse.model_fields
