# harness-framework

AI4S 研究平台的开发版脚手架。同事在这里设计自己负责的研究节点（literature /
hypothesis / analysis / writing / …）；架构师在这里维护框架核心 + 主 harness。

设计原则：每个节点是一个独立 agent loop；orchestrator 管协调；artifact 是显式
契约；memory + KB 跨节点持久化；前端 / 数据库 / 调度器都不在这里 —— 这是研究节点
本身的开发场。

---

## 文件结构

```
harness-framework/
├── core/                       # 框架核心（团队不改）
│   ├── harness.py                  NodeHarness 数据类（硬契约）
│   ├── loader.py                   YAML → NodeHarness
│   ├── bootstrap.py                启动时一次性 import 所有工具 / skill
│   ├── llm.py                      OpenAI-compatible LLM client（默认 GLM via Maas，可切 7 provider）
│   ├── state.py                    run 级 + project 级 state 存储
│   ├── tool_registry.py            工具注册 + 调度
│   ├── skill_registry.py           skill 注册 + 渲染
│   ├── context_engine.py           构建 system + user prompt
│   ├── agent_loop.py               LLM ↔ tools 循环（带 hook + summarizer 钩子）
│   ├── executor.py                 端到端单节点执行 + 子调度
│   ├── summarizer.py               每节点 context 压缩（llm / truncate / drop_tool_results）
│   ├── loop_hooks.py               Hook 系统（4 钩点）
│   ├── loop_hooks_builtin.py       memory_delta / kb_delta / scratchpad / reflection
│   └── mcp_client.py               可选 MCP server 集成
│
├── nodes/                      # 每个节点一个目录
│   ├── _orchestrator/              主 harness（用户对话入口；架构师维护）
│   │   ├── harness.yaml
│   │   ├── tools/                  （可选）orchestrator 专属工具
│   │   ├── skills/                 （可选）
│   │   └── fixtures/               （可选）
│   ├── literature/                 参考 #1 —— 写完整，建议通读
│   │   ├── harness.yaml
│   │   └── fixtures/minimal.yaml
│   ├── analysis/                   参考 #2 —— 结构差异较大的另一种设计
│   │   ├── harness.yaml
│   │   └── fixtures/minimal.yaml
│   ├── hypothesis/                 stub（owner 从零写起）
│   ├── data/                       stub
│   ├── experiment/                 stub
│   ├── postprocess/                stub
│   ├── writing/                    stub
│   └── review/                     stub
│
├── shared/                     # 跨节点共享层
│   ├── tools/                      默认自动注册的工具池
│   │   ├── builtin.py                  ~16 个核心工具（artifact/memory/文件/shell/budget/...）
│   │   ├── papers.py                   semantic_scholar + arxiv
│   │   ├── run_node.py                 run_node + run_nodes_parallel + read_external + query_project_status + forward_artifact
│   │   ├── mcp_loader.py
│   │   └── library/                    opt-in（freeze_artifact / KB / evidence / latex / python_exec）
│   ├── skills/                     跨节点 skill 池
│   │   └── examples.py                 3 个示例 skill
│   └── lib/                        helper 库（按需扩）
│
├── tests/                      # smoke test（每批次一组）
├── docs/                       # 设计文档（先读 docs/architecture.md）
├── templates/                  # harness / tool / skill 起手模板
├── cli/                        # 次要终端 UI（nidy 维护）
├── chat.py                     # 共享会话实现 + 旧终端启动入口兼容
├── run_node.py                 # 单节点 runner
├── pyproject.toml
└── mcp_servers.yaml.example
```

---

## 安装

```bash
cd harness-framework
cp .env.example .env
# 编辑 .env 填三个字段：LLM_API_KEY + LLM_BASE_URL + LLM_MODEL
# .env.example 里列了 7 个 provider 的样例配置（团队 Maas / DeepSeek /
# OpenAI / OpenRouter / 本地 ollama / vLLM / Anthropic via proxy）任选一组。
# 详见 docs/quick-start.md § 3。

pip install -e .

# v0.3.2+: 预下载 KB embedding model（~120MB，一次性，之后离线可用）
bash scripts/install_embedding_model.sh
# 国内网络：脚本默认走 HF mirror（hf-mirror.com）
# 海外用户：export HF_ENDPOINT=https://huggingface.co 再跑
# 离线 GPU server：在有网机跑完后拷 ~/.cache/huggingface/ 到目标机，设 HF_HOME
# 不想要 semantic dedup（CI / 老 KB 设备）：export HARNESS_DISABLE_SEMANTIC_DEDUP=1
```

---

## 跑起来

```bash
# 次要终端 UI（主要交互产品为 Desktop）：
python -m cli --project my-project-2026
# 目录职责与维护边界见 cli/README.md；python chat.py 继续兼容。

# 无人值守持续运行：自动包含 auto-approve + bypass-permissions
python chat.py --project my-project-2026 --continuous

#  > 输入文字 → orchestrator 处理。
#  > /status   → 查项目状态
#  > /btw <text> → 软注入（下一轮 system message）
#  > /reset   → 清对话（保留 memory + KB）
#  > /continuous on|off → 切持续运行；/stop 随时停止
#  > /exit    → 退出（自动保存）

# 单节点跑（开发自己节点时用）：
python run_node.py --harness literature \
    --fixture nodes/literature/fixtures/minimal.yaml \
    --project-id my-project-2026

# 检查输出
ls output/                                       # 每次 run 一个目录
cat output/orchestrator__my-project-2026/conversation.json | jq .
cat output/<sub_run>/transcript.jsonl | jq .
ls output/<sub_run>/artifacts/

# 项目级 memory / KB（持久化）：
ls ~/.harness-framework/projects/my-project-2026/
```

`--continuous` 是项目级持续状态机。普通文字回复、provider 空回复、可恢复错误和
后台节点完成都不会让项目静默掉回 REPL；框架会继续唤醒 orchestrator。只有明确
完成、只剩真人/外部状态才能解除的阻塞，或用户主动 `/stop` / `/continuous off`
才停止自动续轮。

---

## 团队成员要做什么

**先读 2 篇必读**：

1. **`docs/architecture.md`** —— 整体框架（节点契约 / 状态层 / agent loop / hook 系统 / 框架契约 vs 自留地）
2. **`docs/owner-guide.md`** —— 从零到交付的 worked example（含 LAMMPS 节点完整 walkthrough）

**然后**：

1. 选自己节点目录（如 `nodes/analysis/`）
2. 通读 2 个参考 harness：`nodes/literature/harness.yaml` + `nodes/analysis/harness.yaml`
3. 抄 templates：

   | 想做什么 | 抄哪个 |
   |---|---|
   | 改 harness | `templates/harness.yaml.template` |
   | 加新工具 | `templates/tool.py.template` |
   | 加新 skill | `templates/SKILL.md.template`（folder + .md 形态） |
   | 写节点专属 hook | `templates/hooks.py.template` |
   | Override summarizer | `templates/summarizer.py.template` |
   | 写测试 fixture | `templates/fixture.yaml.template` |
   | 项目 / user 偏好 | `templates/PROFILE.md.template` / `PROJECT.md.template` |

4. 本地测：`python run_node.py --harness {your_node} --fixture nodes/{your_node}/fixtures/minimal.yaml`
5. 反复迭代
6. 提交：**只 push `nodes/{your_node}/` 下的改动**

**框架契约（不要改）**：`core/` 全部、`shared/tools/builtin.py`、`shared/tools/library/*`、`shared/skills/*`（非你节点本地的）。
**自留地**：`nodes/{your_node}/` 全部 + 你节点本地的 skills / tools / hooks / summarizer。

---

## 从哪里开始读

**文档**：

🌐 **HTML 站点（推荐入口）**：`docs-html/index.html`，或团队站
`https://desktop-9el2944.taile9f15e.ts.net:8443/`。
用 `python scripts/build_docs_html.py` 重建（需要 `pip install -e .[docs]`）。
生成器带三道闸：过期页面扫盘删除、站内死链失败、文档点名的工具必须真的存在。

📖 原始 markdown 在 `docs/`，按站上的顺序读：

| 组 | 文件 |
|---|---|
| **开始** | `quick-start.md` |
| **设计总纲** | `design-principles.md`（★ 五条判据，先读这个）· `architecture.md` |
| **研究循环** | `research-loop.md` · `evidence-modalities.md` · `node-lifecycle.md` |
| **承诺与证据** | `commitment-and-evidence.md` · `artifact-model.md` |
| **知识与记忆** | `kb-system.md` · `memory-system.md` |
| **人在回路** | `human-in-the-loop.md` |
| **运行时** | `runtime.md` · `context-and-hooks.md` |
| **Owner 手册** | `owner-guide.md` · `node-iteration-guide.md` · `dev-workflow.md` |
| **决策档案** | `RFC_*.md` / `PROPOSAL_*.md` / 事故账本 —— **设计逻辑住在这里** |

节点 / 工具 / skill / hook 的清单**不写在 markdown 里** ——
它们由生成器从 `harness.yaml` 与注册表现算，见站上的「目录 · 自动生成」。

**代码**：
1. `core/harness.py`（80 行）—— NodeHarness 契约
2. `nodes/literature/harness.yaml` —— 完整 producing 节点范例
3. `nodes/_curator/harness.yaml` —— KB 整合节点范例
4. `nodes/_orchestrator/harness.yaml` —— 主 harness 范例
5. `core/agent_loop.py` —— LLM ↔ tools + hook + summarizer + pause/resume
6. `core/executor.py` —— sub-agent 调用 + auto-forward
7. `core/state.py` —— Artifact / KB / Memory 的家
8. `core/pause.py` —— pause/resume 机制

---

## 跑测试

```bash
python -m pytest tests/ -q       # 182 个 unit + integration test，不联网，不调真实 LLM
```
