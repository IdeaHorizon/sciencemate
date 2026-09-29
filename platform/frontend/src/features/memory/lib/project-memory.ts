/**
 * Project Memory 的真相源是 Git 仓库根的 MEMORY.md（v2.1 收敛）。
 *
 * 此前 UI 只读 DB 的 memory_entries，而 harness 真正写的是别处 —— Project v2
 * 下甚至写在用户家目录（~/.harness-framework/…），Git 根那份 MEMORY.md 没有
 * 任何人写、是个装饰品。收敛之后 curator 写 Git，DB 降级为检索投影；UI 必须
 * 跟着看 Git 那份，否则展示的是另一份数据。
 */

export interface MemorySection {
  readonly heading: string;
  readonly entries: readonly string[];
}

const HEADING = /^##\s+(.+?)\s*$/;
const ENTRY = /^\s*-\s+(.+?)\s*$/;

/** 按 Markdown 章节切 MEMORY.md —— 章节就是记忆的分类（Decisions / Findings …）。 */
export function parseProjectMemory(markdown: string): MemorySection[] {
  const sections: MemorySection[] = [];
  let heading: string | null = null;
  let entries: string[] = [];

  const flush = () => {
    if (heading !== null && entries.length > 0) {
      sections.push({ heading, entries });
    }
    heading = null;
    entries = [];
  };

  for (const line of (markdown ?? "").split("\n")) {
    const headingMatch = HEADING.exec(line);
    if (headingMatch) {
      flush();
      heading = headingMatch[1];
      continue;
    }
    if (heading === null) continue;
    const entryMatch = ENTRY.exec(line);
    if (entryMatch) entries.push(entryMatch[1]);
  }
  flush();
  return sections;
}

/** 条目开头的稳定 ID（[D-001] / [F-002] …），没有则返回 null。 */
export function entryId(entry: string): string | null {
  const match = /^\[([A-Z]-\d+)\]/.exec(entry.trim());
  return match ? match[1] : null;
}
