"""「这一轮交付过没有」问 git，不问表（RFC X1 第一步）。

## 为什么

`project_repository` 的 manifest 里白纸黑字写着 `authority: git` —— `project_revisions`
是**投影**。判据建在投影上，分叉时不报错：要么悄悄重复交付（同一份结果发两次），
要么永不交付（表里那行在、git 里没有）。

`publish_linear` 早就是这么判的（`Change-Set-ID:` trailer + `git log --grep`）。
这一步把交付这条路搬到同一把尺子上，也是删掉那五张表之前必须先做的事：**先把
挂在表上的判决搬走，再删表**。顺序反了就是又一次"改造做一半"。
"""
from __future__ import annotations

from app.services.deliverable_publishing import DELIVERY_TRAILER, _delivery_marker
from app.services.project_repository import GitProjectRepository as ProjectRepository


def test_the_published_commit_carries_a_machine_readable_trailer() -> None:
    """人读第一行，机器读 trailer。同一条 message 同时是给人的和给判据的。"""
    message = _delivery_marker("run-42")
    first, _, trailers = message.partition("\n\n")
    assert first == "Continuous checkpoint for Run run-42", "第一行仍然是给人看的"
    assert trailers.strip() == f"{DELIVERY_TRAILER}: run-42"


def test_the_trailer_is_matched_whole_line_not_by_substring() -> None:
    """`run-4` 不该匹配到 `run-42` 的提交。

    `--grep` 的模式必须锚在行首行尾。这不是洁癖：run id 之间互为前缀是常态，
    而"匹配到了别人的交付"在表现上是"这一轮永远发不出去"。
    """
    import inspect

    source = inspect.getsource(ProjectRepository.commit_with_trailer)
    assert 'f"^{trailer}: {value}$"' in source, source


def test_it_asks_the_default_branch() -> None:
    """判据问的是 main 上有没有，不是某个会话分支上有没有。

    会话分支上出现过一次交付提交、后来没合进 main —— 那不叫交付过。
    """
    import inspect

    source = inspect.getsource(ProjectRepository.commit_with_trailer)
    assert "DEFAULT_BRANCH" in source
