"""项目可以选哪些执行目标，以及选的这个合不合法（RFC_WINDOWS_EXECUTION_TARGETS D1/D2）。

写法只在 harness 里定义一次（``core.execution_target``），这里只问「这台机器上有没有」：
``wsl:<发行版>`` 只在 Windows 上、且发行版真的注册过才收；``ssh:`` 后端还没做，明确拒绝，
免得存进去一个永远起不来的目标。发行版里有没有 bubblewrap 不在这里判（要起一次 WSL），
那条由 worker 的开局说明和第一条命令如实报。
"""
from __future__ import annotations

import sys

from app.services.harness_contract import execution_target_module, wsl_module
from app.services.say import SaidToAPerson


class TargetRejectedError(SaidToAPerson, ValueError):
    """不收这个目标。两种语言都写（``said_in``）；``message`` 留着给测试与日志。"""

    def __init__(self, message: dict[str, str]) -> None:
        super().__init__(message["zh"], en=message["en"])
        self.message = message


def available() -> dict:
    """本机可选的目标：本机 + 这台 Windows 上注册的 WSL 发行版。"""
    targets = [{"value": "local", "kind": "local", "name": ""}]
    if sys.platform == "win32":
        for name in wsl_module().list_distros():
            targets.append({"value": f"wsl:{name}", "kind": "wsl", "name": name})
    return {"host": sys.platform, "targets": targets}


def normalise(raw: object) -> str | None:
    """校验并规范化；本机存 None（与「从没设过」同义）。"""
    module = execution_target_module()
    try:
        target = module.parse(raw)
    except module.ExecutionTargetError as exc:
        raise TargetRejectedError({"zh": f"执行目标写法不对：{exc}", "en": str(exc)}) from exc
    if target.is_local:
        return None
    if target.kind == "ssh":
        raise TargetRejectedError({
            "zh": "远程服务器（ssh）执行目标还没做，暂时只能选本机或 WSL 发行版。",
            "en": "ssh execution targets are not available yet; "
                  "choose this machine or a WSL distro.",
        })
    if target.kind == "wsl":
        if sys.platform != "win32":
            raise TargetRejectedError({
                "zh": "WSL 执行目标只在 Windows 上可用。",
                "en": "WSL execution targets are only available on Windows.",
            })
        distros = wsl_module().list_distros()
        if target.name not in distros:
            listed = "、".join(distros) or "（没有）"
            raise TargetRejectedError({
                "zh": f"这台电脑上没有名为 {target.name} 的 WSL 发行版。已注册的：{listed}",
                "en": f"No WSL distro named {target.name} is registered here. Registered: "
                      f"{', '.join(distros) or '(none)'}",
            })
    return target.spec
