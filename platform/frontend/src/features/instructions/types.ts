/** 这个会话**现在**读到的一层指令。 */
export type InstructionLayer = {
  filename: string;
  /** 这一层从哪来：`session_worktree`（git 分支上那一份）/ `harness_home`。 */
  source: string;
  content: string;
  sha256: string | null;
};

/**
 * 会话当下生效的指令 —— 项目层取它自己 worktree 分支上的 `PROJECT.md`
 * （git 就是它的冻结），个人层取这个用户 harness home 里的文件。
 *
 * 从前这里叫 `FrozenInstructions`，三层，取自会话行上的一列 JSON 抄件。
 * 那一列对 041 之前建的会话一律是 NULL，于是这个抽屉对它们永远显示
 * 「没有冻结的指令」，而同一个空值在跑轮那条路上是硬 raise（RFC X3）。
 */
export type SessionInstructions = {
  sessionId: string;
  layers: Record<"project" | "personal", InstructionLayer>;
};

export type InstructionFile = {
  content: string;
  /** 这段文本在磁盘上的位置 —— 用别的编辑器改它也算数，所以要说出来。 */
  path: string;
  sha256: string | null;
  /** 项目层：落下这一版的那次提交。版本就是 git。 */
  commit?: string;
};
