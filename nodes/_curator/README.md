# _curator 节点 ★ Framework 基础设施

**Owner**: wangd

KB 整合 + memory 代谢的心脏。**同事不该改这个节点**；要改 propose 给 wangd review。

## 性质

下划线 `_` 前缀 = framework 基础设施。跟 `_orchestrator` 一样不开放给同事 owner。

## 三种工作模式

1. **Mode 1 (Integration)**：被 orchestrator 调起，整合上游节点 artifact 进 KB
2. **Mode 2 (Dreaming)**：周期性扫 KB 找 research direction / 跨项目模式 / 复审 stale claim
3. **Mode 3 (Per-write)**：每条 KB 写入跑 8 项判断

详见 `harness.yaml` system_prompt 长描述。

## 不开放给同事的原因

- KB 写入是全 framework 数据真值；让 owner 随意改容易破坏 schema 保证
- Curator 的 dreaming / 跨项目 pattern detection 是 framework 一等差异化能力
- 调整 curator 必须连带改 KB schema / propose gate / quality_check —— 牵一发动全身

## 同事如何"曲线影响" curator 行为

- 同事节点写 KB 时 created_by_user_id 自动 stamp → curator dreaming 可识别"哪个 owner 哪种风格"
- 同事可以加节点专属 quality_check → curator 整合时看得到
- propose gate：同事可写 `propose(proposal_type='kb_action', ...)` 让 curator/wangd 在 inbox 审

## 给 wangd（自己）的备忘

curator 维护点：
- skills/ 下 3 个 dreaming skill（kb_integrity_audit / research_direction_exploration / cross_project_pattern_detection）
- harness.yaml 工具白名单含 v3 KB 工具 + `curator_scan`（4 种 scan_type 合一，
  取代了原来的 find_* 切片器）+ `draft_knowledge_card` / `propose`（org 晋升通道）
- fixture：见 fixtures/minimal.yaml（Mode 1 integration 场景）
