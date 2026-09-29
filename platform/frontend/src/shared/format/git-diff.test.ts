import test from "node:test";
import assert from "node:assert/strict";
import { parseGitDiff } from "./git-diff.ts";

test("unified Git diff is grouped by file with exact old and new line numbers", () => {
  const [file] = parseGitDiff([
    "diff --git a/documents/protocol.md b/documents/protocol.md",
    "--- a/documents/protocol.md",
    "+++ b/documents/protocol.md",
    "@@ -7,2 +7,3 @@",
    " control",
    "-old claim",
    "+new claim",
    "+provenance: imported",
  ].join("\n"));

  assert.equal(file.displayPath, "documents/protocol.md");
  assert.deepEqual(
    file.lines.slice(-4).map((line) => [line.kind, line.oldLine, line.newLine]),
    [
      ["context", 7, 7],
      ["deletion", 8, null],
      ["addition", null, 8],
      ["addition", null, 9],
    ],
  );
});

test("empty patches do not invent files", () => {
  assert.deepEqual(parseGitDiff(""), []);
});
