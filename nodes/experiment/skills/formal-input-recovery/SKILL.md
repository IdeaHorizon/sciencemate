---
name: formal-input-recovery
description: |
  通过 Data 服务，或在 Data 终态失败后由冻结契约明确授权的 Experiment 兜底，恢复缺失的预注册正式输入。所需数据集、
  预处理结构、网格、输入包或前处理产物缺失时使用。
applies_when:
  - 任何绑定 frozen prereg 的 scientific run 缺少预注册正式输入或前处理产物
  - 数据集来源、schema、单位或消费验证不完整
tools_used:
  - validate_data_request_spec
  - dispatch_data_request
  - reconcile_data_dispatch
  - verify_dataset_consumption
  - authorize_experiment_fallback
  - verify_experiment_fallback_inputs
  - record_data_delivery_outcome
expected_outcome: 运行实际消费了经验证的正式输入，或产生精确可审计的 blocker
status: validated
---

# 正式输入恢复

1. 识别精确缺失的预注册输入、所需 schema/单位、来源、下游消费者和验收检查。
   Experiment 不得为了继续运行而自行生成貌似相同的正式输入。
2. 构造并验证数据请求规格，调度 Data 节点，并验证返回数据是否真正被消费：
   artifact ID、文件、可读性、schema、单位和预期参数关联；记录交付结果。
   frozen pre-registration 约束输入时选 `formal_input_preparation`；无冻结约束
   （operation、diagnostic、toolchain build）时选 `preprocessing_service_request`，
   后者必须声明 `requesting_stage`、`purpose`、`target_software`、`required_assets`，
   `scientific_parameters` 写 `not_applicable`，且不带科学权威；绑定了冻结 prereg 时
   这个 kind 会被拒 —— 科学运行只能走 `formal_input_preparation`（spec 需带
   `source_prereg_artifact_id`、`scientific_parameters: frozen_prereg_only`，以及
   file_exists/schema/units/manifest_lineage 均为 true 的 `acceptance`）。随后调用
   `dispatch_data_request(spec_id=..., user_note=...)`；它复用既有 Data child 路径并
   持久化 spec、payload 与 child receipt。Data 是同步服务，必须等待数据返回并验收，
   不能把这项工作退回调用方。若 Data 因权限 pause，保留同一请求；恢复后先调用
   `reconcile_data_dispatch()`，由它从直属 child 的完整终态 artifact 清单机械回填回执：
   唯一 dataset 继续验收，唯一 blocker 才可记录；不得重派或手选历史报告。
3. Data 服务不完整、受阻或需要权限时，普通返回只能把 `dispatch_data_request` 返回的
   `child_run_id` 与 `all_child_artifacts` 中的 blocker id 传给 `record_data_delivery_outcome`；
   若该 child pause 后恢复，必须先调用 `reconcile_data_dispatch`，只接受它从同一直属
   child 唯一终态 blocker 返回的 id，再记录 outcome。任意历史报告或裸 `run_node` 结果均
   不能授权兜底。只有冻结 prereg 的
   `input_delivery_policy.mode=experiment_data_fallback` 且
   `experiment_fallback_permitted=true` 时，Experiment 才可接手；否则在 experiment_log 中保留
   精确缺失输入证据并 report_blocker。不得把运行改标为成功，也不得静默替换或合成数据。
4. 获授权的兜底按同一冻结 formal spec 的来源、资产和 scientific_params 获取或转换输入，
   调用 `authorize_experiment_fallback` 后生成带 manifest、逐文件 hash 与 Data blocker 链的
   `experiment_fallback_inputs`，并调用 `verify_experiment_fallback_inputs`。它可作为本 run 的
   正式输入，但不是 scope deviation，也不授权改变数据源、模型、方法或参数，更不能单独
   形成科学 closure。旧 `integration_e2e_fallback` 只保留给历史 secondary 非分析兼容。

## 这条约束**不**覆盖的（属运行化，直接做，不要问、不要报 blocker）

以下不是"制造正式输入"，把它们误判成越界会白白卡住整个 run：

- 运行资源调整：MPI ranks、OMP 线程、walltime、queue、nodelist、ulimit、workdir
- 按 prereg 把**已有**输入复制到 workdir
- 续跑类的常规改名/搬运，例如 VASP 的 `CONTCAR` → `POSCAR`

判据：你是在**生成一份本来不存在的科学输入**（越界），还是在**摆放/参数化一份
已经存在的输入**（运行化，可做）。

另外注意与 verdict 分开：这条针对**输入缺失**；输入齐全但数据判不动
（confound / 缺 metric / N 太小）走 `verdict: inconclusive`，不是这条路。
本 run 是否越界由 hook 机械对账（`preprocessing_boundary_audit` 事件），不靠自述。

该恢复流程通过负责服务创建或获取输入；它不修改冻结 pre-registration，也不授权
科学 closure。
