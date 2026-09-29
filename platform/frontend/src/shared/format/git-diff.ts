export type GitDiffLineKind = "header" | "hunk" | "addition" | "deletion" | "context" | "meta";

export interface GitDiffLine {
  kind: GitDiffLineKind;
  content: string;
  oldLine: number | null;
  newLine: number | null;
}

export interface GitDiffFile {
  oldPath: string | null;
  newPath: string | null;
  displayPath: string;
  lines: GitDiffLine[];
}

const HUNK = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;

function cleanPath(value: string) {
  if (value === "/dev/null") return null;
  return value.replace(/^[ab]\//, "");
}

function fileFromHeader(line: string): GitDiffFile {
  const parts = line.split(" ");
  const oldPath = cleanPath(parts[2] ?? "");
  const newPath = cleanPath(parts[3] ?? "");
  return {
    oldPath,
    newPath,
    displayPath: newPath ?? oldPath ?? "unknown file",
    lines: [{ kind: "header", content: line, oldLine: null, newLine: null }],
  };
}

/** Parse the subset of unified Git patches required for a read-only review UI. */
export function parseGitDiff(patch: string): GitDiffFile[] {
  if (!patch.trim()) return [];
  const files: GitDiffFile[] = [];
  let file: GitDiffFile | null = null;
  let oldLine: number | null = null;
  let newLine: number | null = null;

  for (const content of patch.split("\n")) {
    if (content.startsWith("diff --git ")) {
      file = fileFromHeader(content);
      files.push(file);
      oldLine = null;
      newLine = null;
      continue;
    }
    if (!file) continue;

    const hunk = HUNK.exec(content);
    if (hunk) {
      oldLine = Number(hunk[1]);
      newLine = Number(hunk[3]);
      file.lines.push({ kind: "hunk", content, oldLine: null, newLine: null });
      continue;
    }
    if (content.startsWith("--- ")) {
      file.oldPath = cleanPath(content.slice(4).split("\t")[0] ?? "");
      file.lines.push({ kind: "meta", content, oldLine: null, newLine: null });
      continue;
    }
    if (content.startsWith("+++ ")) {
      file.newPath = cleanPath(content.slice(4).split("\t")[0] ?? "");
      file.displayPath = file.newPath ?? file.oldPath ?? file.displayPath;
      file.lines.push({ kind: "meta", content, oldLine: null, newLine: null });
      continue;
    }
    if (content.startsWith("+") && !content.startsWith("+++")) {
      file.lines.push({ kind: "addition", content, oldLine: null, newLine });
      if (newLine !== null) newLine += 1;
      continue;
    }
    if (content.startsWith("-") && !content.startsWith("---")) {
      file.lines.push({ kind: "deletion", content, oldLine, newLine: null });
      if (oldLine !== null) oldLine += 1;
      continue;
    }
    if (content.startsWith(" ")) {
      file.lines.push({ kind: "context", content, oldLine, newLine });
      if (oldLine !== null) oldLine += 1;
      if (newLine !== null) newLine += 1;
      continue;
    }
    file.lines.push({ kind: "meta", content, oldLine: null, newLine: null });
  }
  return files;
}
