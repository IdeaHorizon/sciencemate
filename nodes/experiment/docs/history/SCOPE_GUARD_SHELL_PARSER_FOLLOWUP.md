> 📦 已归档（2026-08-30）：本文要求的 parser 化已全部完成——`timeout_escalation.py` 使用 `analyze_bash` 的 `backgrounded`/`detached_launch` AST 判定，路径提取走 `static_path_events`，对抗性语料测试（`test_bash_semantics_corpus.py`、`test_bash_path_events.py`）已固化本文的不可协商边界。

# Follow-up: shell parser limits in scope_guard and background detection

## Context

The toolchain environment-probe incident was fixed by routing fixed software
availability checks through `probe_toolchain`, not by changing shell parsing.
That removes `command -v` loops, redirections, and scheduler words used as
plain text from this probe workflow.

## Scope of a future change

Investigate a correctly bounded shell AST or another parser-backed approach for
`safe_bash._scope_guard_bash` path extraction and
`timeout_escalation.looks_backgrounded`. This is explicitly not a claim that
the current string-based guards parse arbitrary shell correctly.

## Non-negotiable regression boundaries

Any future parser work must preserve rejection of real unmanaged launches:

- `nohup` and `setsid` launches;
- a real trailing shell background operator `&` and `disown`;
- bare `srun`, `sbatch`, `qsub`, and equivalent scheduler submission commands;
- actual writes outside declared experiment path roles.

The future work must not relax scope_guard, exempt scheduler names with a
regex, or authorize paths based on malformed redirection text. Add adversarial
parser fixtures before changing either guard.
