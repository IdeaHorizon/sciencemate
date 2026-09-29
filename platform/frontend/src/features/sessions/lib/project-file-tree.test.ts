import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(
  new URL("../components/ProjectFileTree.tsx", import.meta.url), "utf8",
);

/**
 * 平台记账不该淹掉研究产出。
 *
 * 实测（2026-08-12，真实项目 467e24b5）：73 个文件里 **23 个**是 `.history/`
 * （每次改写存一份带时间戳的副本）和 `.frozen.jsonl`，和
 * `survey_report__*.json` / `pre_registration__*.json` 平铺在一起。
 */
test("记账判决由后端给，界面不拿路径形状猜", () => {
  /**
   * 原来这里自己判"路径里有没有点开头的段"。那条规则只错一个地方，而那个
   * 地方恰好是用户 2026-09-09 找不到的那篇论文：调度器的工作区是
   * `.research/orchestration`，它**产出的每一样东西**因此整片被折进「平台
   * 记账」。目录名带点是它所在位置的事，不是它是什么的事。
   *
   * "哪些目录是节点工作区"在后端已经有真相源，界面再判一次就是第二份会分叉
   * 的判据。判据本体的测试在后端
   * `tests/test_the_file_tree_hides_nothing.py`。
   */
  assert.match(source, /entry\.bookkeeping/, "应该读后端给的判决");
  assert.doesNotMatch(source, /startsWith\("\."\)/, "界面不许自己按路径形状再判一遍");
  assert.doesNotMatch(source, /"\.history"|'\.history'/, "更不许写名单");
});

test("记账是分区不是隐藏 —— 看不见的东西没法被审计", () => {
  assert.match(source, /bookkeeping\.length > 0/, "记账要照样列出来");
  assert.match(source, /平台记账/, "要有明确的分区标题");
});

test("计数说的是整个工作区，不是这次拿到了多少", () => {
  // 老版显示的是"这次请求返回了多少行"，而请求被切在 5000 —— 于是一个 9171
  // 个文件的项目，界面上写着"5000 Project files"，看起来像事实。
  assert.match(source, /totalFiles/);
  assert.doesNotMatch(
    source,
    /\{(outputs|entries)\.length\} Project files/,
    "计数不许来自这一层拿到的行数",
  );
});

test("到顶了必须说出来 —— 静默截断就是那个 bug 的本体", () => {
  /**
   * 2026-09-09：9171 个文件的项目里 `paper/` 整个消失，而响应和界面都没有
   * 任何一处说"少给了"。用户和 agent 都只能猜是不是界面折叠了。
   */
  assert.match(source, /listing\.truncated/, "要读后端给的截断标记");
  assert.match(source, /listing\.totalEntries/, "要说出这一层真实有多少项");
});

test("树是展开一层取一层，不是一次拿整棵", () => {
  // 一次拿整棵就必然要在某处切一刀，而那一刀砍掉的永远是字母序靠后的目录。
  assert.match(source, /useSessionProjectTree\(projectId, sessionId, path/);
  assert.match(source, /kind === "directory"/);
});
