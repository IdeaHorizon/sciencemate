import test from "node:test";
import assert from "node:assert/strict";
import { publishedRevisionId, visibleArtifactMetadata } from "./artifact-presentation.ts";

test("published revision is shown only from the authoritative extra_data field", () => {
  assert.equal(publishedRevisionId({ extra_data: { published_revision_id: "revision-7" } }), "revision-7");
  assert.equal(publishedRevisionId({ extra_data: { published_revision_id: 7 } }), null);
  assert.equal(publishedRevisionId({ extra_data: null }), null);
});

test("document metadata excludes stored content and duplicate frozen attribution", () => {
  assert.deepEqual(
    visibleArtifactMetadata({
      extra_data: {
        _content: "large body",
        frozen_at: "2026-08-04",
        frozen_by: "user-a",
        published_revision_id: "revision-7",
        method: "survey",
      },
    }),
    [["published_revision_id", "revision-7"], ["method", "survey"]],
  );
});
