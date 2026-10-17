---
name: ai-workloads
description: |
  AI 训练、评估、checkpoint 恢复与推理的常用专业检查；诊断指标异常、恢复不一致、
  NaN/Inf/OOM 或性能比较时按需使用，适用于 CPU 和加速器上的工程及科研任务。
applies_when:
  - 执行或诊断模型训练、微调、评估、checkpoint 恢复或推理
tools_used:
  - read_file
  - safe_run_bash
  - safe_execute_python
  - safe_write_file
expected_outcome: 完成任务适用的训练或推理工作，以实际输出、指标或恢复检查及运行记录支持结果
status: proposed
---

# AI 工作负载

按本次目标选取下列方法；它们是检查线索，不是每个任务都要执行的清单或新增准入条件。
从已有代码、日志、配置和模型产物开始，复用可信证据。只要求分析、推理或导出时不追加训练。
工程任务也可使用；不要求 scientific 分类、GPU、分布式环境或额外安装框架。

## 数据与评估

- 明确实际模型/权重、数据划分、预处理与代码版本。检查标签含义、输入形状和 dtype；
  通用指标名不能替代任务中的计算定义。仅在缺少关键前提会改变结论时补查。
- 检查训练与评估之间的样本、实体或时间泄漏；需要拟合的预处理通常只在训练划分上拟合，
  复用其状态处理验证/测试集，交叉验证时在各训练折内拟合。明确约定的其他协议按其执行。
- PyTorch 常规评估使用 `model.eval()`；`torch.no_grad()` / `torch.inference_mode()`
  不会自动切换 Dropout/BatchNorm 行为。需要梯度或随机推理的任务按目的选择模式。
- 核对指标的有效样本/token 数、mask、权重和 reduction。batch 大小不等时不能直接平均
  batch 均值；按该指标定义合并分子与分母或充分统计量。分布式时还核对跨 rank 聚合及
  sampler 补齐产生的重复样本。AUC 等非可加指标不能仅平均局部分数。
- 小样本或少量 step 可验证流程，但不能单凭 loss 下降声称泛化改善；报告实际评估集与
  适用基线。若任务只验运行或恢复，不追加无关的模型质量门槛。

## 训练与数值故障

- 记录已完成的实际更新数；区分 batch、梯度累积 micro-step、optimizer update 和 epoch。
  使用 GradScaler 时，溢出可能跳过更新，不能把每次循环或每次调用都计作成功更新。
- loss 异常先定位首次非有限值或异常范围：输入/标签、forward/loss、梯度、更新后参数。
  保存原日志和相关配置，再选择最便宜的区分性检查；不以替换 NaN 或忽略坏 batch 隐藏故障。
- OOM 先确认发生阶段、实际 batch/序列长/dtype、峰值和可用资源。调整 batch、累积、精度
  或重算时说明对有效 batch、调度和数值的影响；不能把减小任务规模后的成功冒充原配置成功。

## checkpoint 与恢复

- 区分仅加载权重的 warm start、继续训练和复现同一训练轨迹；先确定用户需要哪一种。
  不因有 checkpoint 文件就声称可完整恢复，也不把允许的 warm start 判成恢复失败。
- 任务需要诊断、迁移或证明续训恢复时，再按需加载
  `load_skill(name='ai-workloads', asset='references/checkpoint-resume.md')`。纯推理或已明确接受的
  weights-only warm start 不要求执行其中的续训轨迹核验。
- 推理验收与续训状态验收按用途分别检查；推理通过不证明可续训，optimizer/scheduler/RNG
  等训练状态只在相应恢复目标适用时检查，不作为纯推理通过的前提。
- 对实际算法核对所需状态：模型参数/缓冲区、优化器（如动量）、已用 scheduler/scaler、
  已完成更新数，以及需要连续性的 RNG、数据顺序/sampler/读取位置。未使用的组件不要求保存；
  只重设初始 seed 不能代替恢复中断时 RNG 状态。缺失状态时说明能恢复到什么程度。
- 保存点和加载顺序遵循所用框架版本，尤其是梯度累积与调度时序；优先在完整更新边界保存。
  核对加载的确切文件、模型兼容性、设备映射和下一更新位置，不只看文件名中的 epoch。
- 需要证明恢复时，实际从保存点继续有界步数，检查步数、学习率和状态确实延续；要求轨迹
  一致时，再与不中断的短对照比较状态/指标并说明容差。CPU/GPU 或版本变化不保证逐位一致。
- 保留原 checkpoint，验证新文件可读及关键状态后再标成可用。加载前核对来源；不对不可信
  pickle 执行不受限反序列化。PyTorch 优先使用受限 `weights_only=True`，它不是任意文件安全
  的保证；加载失败不自动改成 `weights_only=False` 或把未知类加入白名单。

## 推理与性能

- 核对输入预处理、tokenizer/标签映射、权重、输出形状及任务适用的参考结果；量化、编译或
  精度变化后重新检查输出误差或质量，不能只凭加载成功、HTTP 成功或更快的时间声称正确。
- 比较性能时固定可比的输入、batch/序列长、精度、设备、线程数和并发；报告计时范围是否
  含预处理、传输与后处理。分别记录首次加载/编译延迟和 warmup 后稳态，保留重复测量及波动。
- CUDA 等异步设备使用相应同步或 device events，避免只测主机提交时间；PyTorch 可用
  `torch.utils.benchmark.Timer`，但需显式选择代表实际场景的线程设置。吞吐的单位和延迟的
  统计对象要明确；批均耗时不能直接冒充单请求尾延迟。

结果沿已有执行与交付机制保存；区分已测事实、推断和未验证部分。工具/平台能力不足时
交代具体缺口，CPU 小任务的结果不外推为真实多 GPU 或多节点验证。

## 按问题查官方依据

已有依据足够时直接工作；API/版本语义不确定时按需查对应页面，不要求逐份阅读：

- [数据泄漏与预处理](https://scikit-learn.org/stable/common_pitfalls.html#data-leakage)
- [保存、恢复与 warm start](https://docs.pytorch.org/tutorials/beginner/saving_loading_models.html)
- [评估模式与 inference mode](https://docs.pytorch.org/docs/stable/generated/torch.autograd.grad_mode.inference_mode.html)
- [AMP 更新与恢复](https://docs.pytorch.org/tutorials/recipes/recipes/amp_recipe.html)
- [复现范围](https://docs.pytorch.org/docs/stable/notes/randomness.html)、[加载安全边界](https://docs.pytorch.org/docs/stable/notes/serialization.html#torch-load-with-weights-only-true)
- [性能计时](https://docs.pytorch.org/tutorials/recipes/recipes/benchmark.html)
