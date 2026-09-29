"""秘密不许进模型上下文（issue #279，qinp 产品 E2E 实测）。

现场：data 节点为了解析 `${PODSYS_SAFE_SOURCE}`，直接
`read_file(path="/proc/self/environ")` —— 那是**整个进程的环境块**，里面有
LLM_API_KEY、会话令牌等一切与本次输入无关的秘密。工具结果原样进了模型消息，
也就同时进了 transcript / checkpoint / provider 日志。客户授权的范围只是那**一个**
变量对应的路径。

两道防线，缺一不可：

  1. 路径黑名单（`is_sensitive_path`）—— 明确拒绝 procfs 环境入口、凭据文件。
     快，但只挡得住"直接读文件"这一条路。
  2. **值脱敏**（`redact`）—— 工具结果进消息之前统一扫一遍，把环境里那些像
     秘密的值替换掉。这条是根治：`run_bash("cat /proc/self/environ")`、
     `execute_python` 里 `os.environ`、grep 到 .env ……不管从哪条路捞出来，
     出口只有一个（core/tool_registry.execute），在那里洗干净。

只挡路径不脱敏 = 换个工具就绕过去了；只脱敏不挡路径 = 秘密仍被读进进程、
只是没打印出来。所以两条都要。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

# ── 1. 敏感路径 ────────────────────────────────────────────────────────────
#
# procfs 的 environ/cmdline 是"整个进程的秘密"的直通车；其余是业界公认的凭据
# 落盘位置。匹配用 **normalize 后的完整路径**，不看相对写法（`../` 早在
# _resolve_path 里解析掉了）。
_SENSITIVE_PATH_RE = re.compile(
    r"""(?x)
    ^/proc/(self|\d+|thread-self)/(environ|cmdline)$    # 进程环境 / 命令行
  | /\.ssh/(id_[^/]*|identity|.*\.pem)$                 # SSH 私钥
  | /\.aws/credentials$
  | /\.config/gcloud/.*credential.*$
  | /\.docker/config\.json$
  | /\.netrc$
  | /\.git-credentials$
  | /\.npmrc$
  | /\.pypirc$
    """,
)
# .env / .env.local / .env.production …（目录名叫 .environment 之类的不算）
_ENV_FILE_RE = re.compile(r"(^|/)\.env(\.[^/]+)?$")


def is_sensitive_path(path: str | os.PathLike[str]) -> str | None:
    """敏感 → 返回**给人看的拒绝理由**；不敏感 → None。

    理由文案里绝不能带上文件内容（那正是要防的东西）。
    """
    # Credentials retain their identity across Windows/POSIX separators, case
    # aliases and NTFS alternate data streams. Do not reinterpret a Unix path
    # with the host's Path class before classifying it.
    p = str(path).replace("\\", "/").casefold()
    parent, separator, leaf = p.rpartition("/")
    p = parent + separator + leaf.split(":", 1)[0]
    if _SENSITIVE_PATH_RE.search(p):
        return (f"拒绝读取 {p}：这是进程环境/凭据入口，不是研究数据。"
                "需要某个环境变量指向的路径时，让 orchestrator 在 node_inputs 里"
                "传解析后的路径，而不是把整个环境块读进来。")
    if _ENV_FILE_RE.search(p):
        return (f"拒绝读取 {p}：.env 文件存放凭据。"
                "需要其中某个配置项时请让人工显式提供该项的值或路径。")
    return None


# ── 2. 值脱敏 ──────────────────────────────────────────────────────────────

# 变量名命中这些词 = 按秘密处理。宁可多脱敏一个无关变量（顶多让模型看到
# «REDACTED»），也不能漏掉一个真凭据。
_SECRET_NAME_RE = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|SESSION|COOKIE|SALT|"
    r"PRIVATE|SIGNATURE)",
    re.IGNORECASE,
)
# 太短的值没法当秘密，且极易误伤正文（"true"/"prod"/端口号之类）。
_MIN_SECRET_LEN = 12


def is_secret_name(name: str) -> bool:
    """环境变量名像不像凭据。给隔离后端脱敏子进程环境用 —— 同一条正则，不另抄。"""
    return bool(_SECRET_NAME_RE.search(name))

_MASK = "«REDACTED:{name}»"


def secret_values() -> dict[str, str]:
    """当前进程环境里"像秘密"的 值 → 变量名。

    每次现算，不缓存：`.env` 可能在 run 中途被重新加载，缓存住的旧集合会漏掉
    新出现的凭据 —— 这里宁可多花几十微秒。
    """
    out: dict[str, str] = {}
    for name, value in os.environ.items():
        if not value or len(value) < _MIN_SECRET_LEN:
            continue
        if _SECRET_NAME_RE.search(name):
            out[value] = name
    return out


def redact(obj: Any, *, secrets: dict[str, str] | None = None) -> Any:
    """深走 dict/list/str，把秘密值换成 `«REDACTED:VAR»`。

    保形：结构、键名、非字符串值一律不动 —— 下游有一堆按 `status` / `content`
    取值的消费方，脱敏不能改变它们看到的形状。
    """
    if secrets is None:
        secrets = secret_values()
    if not secrets:
        return obj
    return _walk(obj, secrets)


def _walk(obj: Any, secrets: dict[str, str]) -> Any:
    if isinstance(obj, str):
        out = obj
        for value, name in secrets.items():
            if value in out:
                out = out.replace(value, _MASK.format(name=name))
        return out
    if isinstance(obj, dict):
        return {k: _walk(v, secrets) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        walked = [_walk(v, secrets) for v in obj]
        return type(obj)(walked) if isinstance(obj, tuple) else walked
    return obj


def contains_secret(obj: Any) -> list[str]:
    """obj 里出现了哪些秘密变量的值（返回**变量名**，绝不返回值本身）。

    给审计事件用：既要留痕"这里曾有秘密"，又不能把秘密写进 transcript。
    """
    secrets = secret_values()
    if not secrets:
        return []
    hits: set[str] = set()
    _scan(obj, secrets, hits)
    return sorted(hits)


def _scan(obj: Any, secrets: dict[str, str], hits: set[str]) -> None:
    if isinstance(obj, str):
        for value, name in secrets.items():
            if value in obj:
                hits.add(name)
    elif isinstance(obj, dict):
        for v in obj.values():
            _scan(v, secrets, hits)
    elif isinstance(obj, list | tuple):
        for v in obj:
            _scan(v, secrets, hits)
