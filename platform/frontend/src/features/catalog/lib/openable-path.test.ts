import test from "node:test";
import assert from "node:assert/strict";

import { openablePathOf } from "./openable-path.ts";

/** 目录里一条真实形状的记录（2026-09-06 本机 Ising 课题，字段照 core/catalog.to_dict）。 */
const PREREG = {
  artifactId: "pre_registration__Ising_Tc_MC_prereg",
  kind: "pre_registration",
  name: "Ising_Tc_MC_prereg",
  ownerNode: "hypothesis",
  version: 2,
  frozen: true,
  permanent: true,
  tier: "deliverable",
  isDeliverable: true,
  recordPath: "plan/artifacts/pre_registration__Ising_Tc_MC_prereg.json",
  files: [] as string[],
  createdAt: "2026-09-06T19:36:27.444710+00:00",
  frozenAt: "2026-09-06T19:37:19.379661+00:00",
};

test("论文开它的 PDF —— 伴随文件排在前面", () => {
  const manuscript = {
    ...PREREG,
    kind: "manuscript",
    recordPath: "paper/artifacts/manuscript__Ising_Tc.json",
    files: ["paper/latex_build/Ising_Tc_clean/main_clean.pdf", "paper/latex_build/Ising_Tc/main.tex"],
  };
  assert.equal(openablePathOf(manuscript), "paper/latex_build/Ising_Tc_clean/main_clean.pdf");
});

test("预注册没有伴随文件，开它自己的信封 —— 而不是灰着", () => {
  assert.equal(openablePathOf(PREREG), "plan/artifacts/pre_registration__Ising_Tc_MC_prereg.json");
});

test("连记录路径都没有的条目才是真打不开", () => {
  assert.equal(openablePathOf({ files: [], recordPath: "" }), null);
});
