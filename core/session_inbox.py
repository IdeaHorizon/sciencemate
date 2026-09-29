"""会话邮箱：人对正在干活的 agent 说的**话**（RFC 异步运行时 D12 + P1）。

## 这里只装话，不装信号（2026-08-24）

从前停止也走这个队列（`KIND_STOP`）。那是个类别错误，代价是自主档的停止
按钮**完全失效**：队列必须有人取，而消费者的寿命绑在"某一种操作"上
（`turn` / `run_unattended` 各建一个，`answer` 一个都没有）。于是一条
`answer` 驱动的续跑一跑二十分钟，期间用户点 19 次停止 —— 19 条
`inbox_item_received`，**0 条 consumed**（node20 会话 a6f156e4 实测）。

分界线是**这东西需不需要被理解**：
  · 话   —— 语义开放，要带着完整语境跑一轮真实的调度器轮次才能回答，
            因此必须由"正在跑的那一轮"在安全点上取走。队列是对的。
  · 停止 —— 一个布尔。没有安全点可言，它要立刻对**当时正在跑的任何东西**
            生效。给它排队 = 给它造一个"谁来取"的问题，而那个问题的答案
            是一份会变长的操作名单（[[护栏要扫盘不要写名单]]）。

所以停止不再进这个模块：它在 socket 分发点同步施加（`PlatformSession
.stop_now`），到达即生效，没有队列、没有消费者、也就没有"漏掉哪条路"。


## 它取代了什么

从前这是一个**文件收件箱**：App Server 把话写进工作区里的一个目录，worker
在跑轮期间每秒轮询。那是补偿机制 —— 补的是"协议不能多路复用"这件事，原模块
的文档里写得很清楚：「要走 RPC 就得把协议改成多路复用，那是个大改动」。

多路复用做完了（P1-1），所以补偿退休：话经 socket 直达 worker 进程，这个模块
只剩**队列语义**，不再管投递。

## 每条消息必有终局

从不更新的字段不是事实 —— 对邮箱条目同样适用。一条进来的消息只有三种下场，
而且三种都会在会话事实流里留下一行：

    received  → consumed      被消费了（正常）
              → superseded    被后来的东西取代（stop 会作废在它之前排队的话）
              → quarantined   连续 N 次消费即崩 —— 毒丸，隔离

没有第四种，也没有"就那么躺着"。

## 毒丸熔断必须在**消费路径**上

PR#553 的教训：熔断器挂在账本上，而失败那条路绕过了账本记账，于是熔断器
"存在但不在场"。所以这里的计数由 `consume()` / `failed()` 自己维护，不依赖
任何外部记账。

## 为什么队列在进程内

写的人和读的人现在是同一个进程（socket 收下来就在 worker 里）。持久化由**会话
事实流**承担：入队/消费/隔离都是 transcript 里的行，与对话共用同一份日志
（抄自 Claude Code 的 queue-operation，附录 S2）。消费指针与效果因此天然
同一持久化域 —— 不需要第二个存储，也就不会有第二个真相源。
"""

from __future__ import annotations

import itertools
import time
import uuid
from dataclasses import dataclass, field

#: 人对 agent 说的话，语义开放，由待命轮带着完整语境回答。
#:
#: 只剩这一种。`KIND_STOP` 已删除 —— 见模块开头「这里只装话，不装信号」。
#: 保留这个常量而不是把 `kind` 字段一并删掉：条目的终局记账（received /
#: consumed / superseded / quarantined）会把它写进会话事实流，历史行里也有
#: 它；留一个恒定值比让每条记录少一个字段更容易读。
KIND_MESSAGE = "message"

KINDS = (KIND_MESSAGE,)

#: 单条的字数上限。人手打的话不会很长；给个界防止误粘一整个文件。
MAX_TEXT_CHARS = 8_000

#: 同一条消息连续崩几次算毒丸。取 2：一次可能是偶发（上游抖动），
#: 两次就该怀疑是这条消息本身 —— 再多试只是把同一次崩溃重复制造。
POISON_THRESHOLD = 2

_ids = itertools.count(1)


@dataclass(frozen=True)
class Item:
    """一条已经收下的消息。"""

    kind: str
    text: str
    author: str = "user"
    #: 这句话对应的会话消息 id（App Server 先落 user 消息行再投递）。
    #: 回执和答复靠它锚回"用户问的那一条" —— 没有它，答复只能挂在 run 的活动
    #: 窗口里，渲染在提问上面（2026-08-18 实测）。
    message_id: str = ""
    #: 进程内唯一。毒丸计数、终局记账都按它。
    item_id: str = field(default_factory=lambda: f"in-{next(_ids)}-{uuid.uuid4().hex[:6]}")
    at: float = field(default_factory=time.time)

    def truncated(self) -> "Item":
        if len(self.text) <= MAX_TEXT_CHARS:
            return self
        return Item(
            kind=self.kind,
            text=self.text[:MAX_TEXT_CHARS] + "\n…（超出长度上限，已截断）",
            author=self.author,
            message_id=self.message_id,
            item_id=self.item_id,
            at=self.at,
        )


def make(kind: str, text: str, *, author: str = "user", message_id: str = "") -> Item:
    """造一条。空文本与未知类别当场拒绝 —— 收下一条不可能被消费的东西，
    就是在给毒丸熔断制造工作。"""
    if kind not in KINDS:
        raise ValueError(f"unknown inbox kind: {kind!r}（合法取值：{KINDS}）")
    body = (text or "").strip()
    if not body:
        raise ValueError("inbox text must not be empty")
    return Item(kind=kind, text=body, author=author or "user", message_id=message_id).truncated()


class Inbox:
    """一个会话的邮箱。进程内，单写者（socket 分发循环）单读者（跑轮的循环）。

    它**不做 IO**：入队/消费/隔离要落到会话事实流，由调用方写 —— 那是它自己
    的日志，本模块不该知道 transcript 长什么样。
    """

    def __init__(self, *, poison_threshold: int = POISON_THRESHOLD) -> None:
        self._pending: list[Item] = []
        self._failures: dict[str, int] = {}
        self._quarantined: list[Item] = []
        self._threshold = max(1, int(poison_threshold))

    def __len__(self) -> int:
        return len(self._pending)

    def put(self, item: Item) -> None:
        """收下一条。

        这里曾经还负责"`stop` 作废排在它前面的话"。作废本身是对的语义（那些
        话是说给"这一轮"听的，而这一轮马上就没了），但它不该由**入队**这个
        动作来做 —— 那要求停止先变成一条队列条目。停止移出队列之后，作废由
        施加停止的那一处调 `drain_pending()` 完成：同一件事，发生在它真正
        发生的地方。
        """
        self._pending.append(item)

    def take(self) -> Item | None:
        """取下一条待消费的。取走**不**立刻删 —— 消费成功了才算数
        （`consumed`），崩了要能数第二次（毒丸计数）。"""
        return self._pending[0] if self._pending else None

    def consumed(self, item: Item) -> None:
        self._drop(item)

    def failed(self, item: Item) -> bool:
        """一次消费崩了。返回 True 表示它已被隔离（毒丸）。

        熔断就在这条路径上 —— 不经过任何外部账本，因此没有"绕过账本 =
        绕过熔断"的缝（PR#553 的教训）。
        """
        count = self._failures.get(item.item_id, 0) + 1
        self._failures[item.item_id] = count
        if count >= self._threshold:
            self._drop(item)
            self._quarantined.append(item)
            return True
        return False

    def quarantined(self) -> list[Item]:
        return list(self._quarantined)

    def drain_pending(self) -> list[Item]:
        """把还没消费的全部取走 —— 它们的终局由调用方定。

        两个调用点：轮结束时收尾，以及**施加停止时**（那些话是说给这一轮
        听的，这一轮没了它们也就没了对象）。毒丸计数一并清掉：条目已经离开
        队列，再留着计数就是一个没有主人的账。
        """
        out, self._pending = self._pending, []
        for gone in out:
            self._failures.pop(gone.item_id, None)
        return out

    def _drop(self, item: Item) -> None:
        self._pending = [q for q in self._pending if q.item_id != item.item_id]
        self._failures.pop(item.item_id, None)
