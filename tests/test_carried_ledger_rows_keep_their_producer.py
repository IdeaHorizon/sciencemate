"""账本行可以从一本账搬到另一本账：内容一字不改，链按新账本重接，重搬不记两遍。

给平台的会话恢复用（#1186）：接续会话有自己的 worktree、自己的账本，原会话冻结的产物
要连同它们的 save / freeze 行一起过来 —— 新会话的 State 只读自己那本账，只搬正文不搬账，
那份预注册在它眼里就是一个没有身份、没有冻结的文件。
"""
from __future__ import annotations

import hashlib
import json

from core.ledger import workspace_store, write_record


def test_carried_rows_keep_their_producer_and_freeze_and_join_the_new_chain(tmp_path) -> None:
    source_root, target_root = tmp_path / "source", tmp_path / "target"
    record = write_record(
        source_root, artifact_type="pre_registration", name="Q1", content="# Q1\n",
        directory="plan", produced_by_node_type="hypothesis", produced_by_run_id="run-src",
        frozen=True,
    )
    source = workspace_store(source_root)
    target = workspace_store(target_root)
    # 目标账本先有一行别的东西：搬过来的行要接在**它**后面，不是沿用源账本的链。
    write_record(
        target_root, artifact_type="research_state", name="v1", content="# v1\n",
        directory="plan", produced_by_node_type="hypothesis", produced_by_run_id="run-new",
    )
    (target_root / "plan" / "pre_registration__Q1.md").write_text("# Q1\n", encoding="utf-8")

    carried = target.carry(source.rows(), recovered_from_session="s-source")

    assert [row["event"] for row in carried] == ["save", "freeze"]
    head = target.head(record["id"])
    assert head.frozen and head.produced_by_run_id == "run-src", "出处是原产出方，不是搬运者"
    assert target.record(record["id"])["content"] == "# Q1\n"
    lines = (target_root / ".research/ledger/records.jsonl").read_text(encoding="utf-8").splitlines()
    for previous, line in zip(lines, lines[1:]):
        assert json.loads(line)["prev_row_sha256"] == hashlib.sha256(previous.encode("utf-8")).hexdigest()
    assert all(row.get("recovered_from_session") == "s-source" for row in carried)

    assert target.carry(source.rows(), recovered_from_session="s-source") == [], (
        "同一次恢复重放一遍，不许把账记两遍"
    )
    assert len(target.rows()) == len(lines)

