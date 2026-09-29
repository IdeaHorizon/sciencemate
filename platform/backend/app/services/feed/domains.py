"""域词表 —— 平台这一侧只是**问**，不存第二份。

## 为什么不在这里放一份分类表

资讯流的兴趣标签、KB 晋升的域、org 正典的寻址键，是同一个词表。抄一份到
App Server 就是两个会各自演化的真相源，而分叉时两边都不报错：harness 那边
改了词表，平台这边的选择界面里还是旧的，用户永远不知道自己少了个选项。

所以走 `harness_contract` —— 与收件箱格式、worker 地址规则同一条路子。代价
是要把 harness checkout 放进 sys.path（那本来就是 App Server 的既有配置），
收益是词表永远只有 `core/domain_registry.py` 一处定义。

## 为什么只到骨架层，不含 org 本地叶

本地叶存在 `paths.org_root()/domain_leaves.jsonl`，而 `org_root()` 认的是
`HARNESS_FRAMEWORK_ORG_HOME`；platform_runtime 每次跑都用 `_temporary_home`
把它绑到**这一次请求的 home_dir**（`state/users/<uid>/org`）。也就是说本地叶
是按用户分的，App Server 要读它只有两条路：

  1. 在自己进程里临时改 `os.environ` —— 一个异步多请求的进程里改全局环境
     变量，是在给自己造一个只在并发时才现形的竞态；
  2. 经桥问 harness（`op=` 加一个），由**拥有那份状态的进程**回答。

骨架层不需要任何状态（`ARXIV_SPINE` 是纯代码），所以 M1 的兴趣选择只给骨架
分类：它覆盖了绝大多数选择场景，且这条路上一个竞态都没有。本地叶进选择界面
是 2 号做法，留给后续 —— 不是忘了，是没打算用 1 号做法换这个功能。

## 词表拿不到的时候

`harness_contract` 会 fail loud，这里**不把它翻译成"这个域不认识"**。两件
事必须分得开：

  - "这个 slug 不在词表里" —— 一个关于内容的判断；
  - "我现在读不到词表" —— 一个关于平台自己的故障。

把后者说成前者，症状就是资讯流里所有内容的域标签集体消失，而日志里一个字
没有。所以读不到就抛，由调用方决定怎么呈现：采集侧记成源失败（看得见），
接口侧回 503（说得清）。
"""

from __future__ import annotations

import threading
import json
from types import ModuleType

from app.services.harness_contract import HarnessContractUnavailable, domain_registry_module

_lock = threading.Lock()
_registry: ModuleType | None = None
_spine: frozenset[str] | None = None
_cas_catalog: tuple[dict, ...] | None = None
_cas_domains: frozenset[str] | None = None


def domain_registry() -> ModuleType:
    """`core.domain_registry` —— 词表的唯一定义处。

    自己缓存而不是每次走 `harness_contract`：后者会改 `sys.path` 和
    `sys.modules`，而采集是在后台任务里跑的，和请求处理并发。加载一次、
    在锁里做完，之后都只是读一个已经装好的模块对象。
    """
    global _registry
    if _registry is not None:
        return _registry
    with _lock:
        if _registry is None:
            _registry = domain_registry_module()
    return _registry


def registry_available() -> bool:
    """词表现在读不读得到 —— 给健康呈现用，不用来做内容判断。"""
    try:
        domain_registry()
    except HarnessContractUnavailable:
        return False
    return True


def _spine_set() -> frozenset[str]:
    global _spine
    registry = domain_registry()
    if _spine is None:
        _spine = frozenset(registry.spine_categories())
    return _spine


def is_known_domain(slug: str) -> bool:
    """这个 slug 在骨架词表里吗。

    读不到词表会抛 `HarnessContractUnavailable` —— 见模块 docstring：
    "不认识" 和 "读不到" 不是同一件事。
    """
    value = (slug or "").strip()
    if value == FEED_ARXIV_DOMAIN:
        return True
    if value.isdigit() and len(value) == 6:
        return value in cas_domains()
    return value in _spine_set()


def cas_domains() -> frozenset[str]:
    """The actual CAS leaf codes exposed by the checked-in catalog."""
    global _cas_domains
    if _cas_domains is None:
        _cas_domains = frozenset(
            str(option.get("domain") or "")
            for group in catalog()
            if str(group.get("archive") or "").startswith("cas:")
            for option in group.get("categories", ())
            if str(option.get("domain") or "")
        )
    return _cas_domains


#: 资讯流面向的是中文使用者，域名用中文显示。
#:
#: **不新建一张中文表** —— 中英两套都在 `core/domain_registry.py` 里，与
#: slug 同处一处。抄一份到平台就是第三份会各自演化的词表，而分叉时不报错
#: （只是界面上中英混排）。
FEED_LABEL_LANG = "zh"
FEED_ARXIV_DOMAIN = "arxiv"


def label(slug: str, *, lang: str = FEED_LABEL_LANG) -> str:
    value = (slug or "").strip()
    if value == FEED_ARXIV_DOMAIN:
        return "arXiv 预印本" if str(lang).lower().startswith("zh") else "arXiv Preprints"
    if value.isdigit() and len(value) == 6:
        for group in catalog(lang=lang):
            for option in group.get("categories", ()):
                if option.get("domain") == value:
                    return str(option.get("label") or value)
    return str(domain_registry().domain_label(value, lang=lang))


def ancestors(slug: str) -> tuple[str, ...]:
    """上行链（骨架分类 → archive），最细在前。

    送达匹配靠它：订了 `cond-mat` 的人应该收到 `cond-mat.mtrl-sci` 的内容，
    反过来不成立。
    """
    value = (slug or "").strip()
    if value.isdigit() and len(value) == 6:
        return (value, value[:4])
    return tuple(domain_registry().ancestors(value))


def catalog(*, lang: str = FEED_LABEL_LANG) -> tuple[dict, ...]:
    """给资讯兴趣选择使用的目录：arXiv 独立保留，主体使用 CAS 一二级学科。"""
    global _cas_catalog
    if _cas_catalog is None:
        from importlib.resources import files
        path = files("app").joinpath("data/discipline_catalog.json")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, list) or not data:
                raise ValueError("discipline catalog is empty")
            _cas_catalog = tuple(data)
        except (OSError, ValueError) as exc:
            raise HarnessContractUnavailable(
                "The packaged discipline catalog is unavailable"
            ) from exc
    if str(lang).lower().startswith("en"):
        # CAS 原始目录只有中文标签；英文界面不能把中文混进来。没有官方
        # 英文对照表时用稳定的 CAS 编码作为可核验回退，而不是伪造翻译。
        cas = tuple(
            {
                **group,
                "label": f"CAS {str(group.get('archive', '')).removeprefix('cas:')}",
                "categories": tuple(
                    {**category, "label": f"CAS {category.get('domain', '')}"}
                    for category in group.get("categories", ())
                ),
            }
            for group in _cas_catalog
        )
    else:
        cas = _cas_catalog
    legacy = (
        {
            "archive": FEED_ARXIV_DOMAIN,
            "label": label(FEED_ARXIV_DOMAIN, lang=lang),
            "categories": (
                {
                    "domain": FEED_ARXIV_DOMAIN,
                    "label": label(FEED_ARXIV_DOMAIN, lang=lang),
                    "kind": "archive",
                },
            ),
        },
    )
    return cas + legacy


def reset_cache() -> None:
    """丢掉缓存 —— 只给测试用（同一进程里换 harness 根）。"""
    global _registry, _spine, _cas_catalog, _cas_domains
    with _lock:
        _registry = None
        _spine = None
        _cas_catalog = None
        _cas_domains = None
