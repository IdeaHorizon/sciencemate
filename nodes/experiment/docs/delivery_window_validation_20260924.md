# 当前交付相关性与检查窗口：2026-09-24 检查点

## 基线与共同决定

实现基于 `f82df95d`（运行代码同507c/7d33）。仅 Experiment 内修改；现有 UI、
用户任务和封存证据未动。双方第一轮讨论为信箱060的133、134、135号，
第二轮在固定实现 `05a295551e72a07bed9cd774a8907cb5204e249c` 上独立复核，
142号结论为 accept（仅本批修复）。此文不宣布发布或完整防假成功；后续文档更新不换运行代码。

共同不变量：按当前 A 的条件及相关 B 评价 C，不按历史失败先后追加成功条件。
当前producer/modifier失败、正式required失败、开放/未知作业与权限规则照旧；
无法绑定当前条件的历史失败保留B并披露，不称已恢复或完全无关。
任何检查执行前，正式file、显式对象、当前file方法、必要glob见证进入同一窗口；
检查后复核对象，不能用另一个历史成功producer替换检查过的文件。
目录输入/输出共用成员与叶身份比较，非空容器单纯mtime变化不否定内容；
覆盖/hash不足与确定变化区分。重新实际检查可恢复，不冻结错误方法为新A。

## 改动与未改动

- 核心仅 `tools/finish.py`：移除全局时间门；共同前观察与后核验；不完整观察准确归因。
- governing同步该边界；无新增工具、账本、模型字段或权限豁免。
- 原样导入会话2的0124与0137测试；另加13格正式/自然对象窗口和反向误伤检查。
- 三个旧测试有意迁移：无关联settled failure仍进最终B而不新建条件；CMake/scientific
  无产物失败仍由原正式缺失/失败检查拒绝success，不凭空赋producer关系。
  原success拒绝、真实失败事实、failed三件套与审计/重入仍验证。不是给错误改绿。
- 不改Core/shared、题库判卷、旧实例、旧封存产物或对象省略红例。

## 已跑证据

解释器：工作区既有 `.venv311-060-r3a-20260923/bin/python`（Python3.11）。
pytest均加 `-p no:cacheprovider -q --tb=short`；测试来源在节点tests，未运行模型/UI/Slurm。

| 验证 | 结果 | 解释 |
|---|---|---|
| f82原样0124两文件 | 5 passed / 3 failed，3.15秒 | 候选good→bad误拒；Project/direct输出成员删除假成功均实证 |
| 新13格的基线对照 | 8 passed / 5 failed，4.89秒 | 独立进程首次导入时以内存loader加载f82原finish；其余生产代码未变，磁盘未回退。4格错误接受，1格glob-after错误归因；不是全树基线验收 |
| 第一批扩大定向11文件 | 100 passed / 1 failed，27.31秒 | 唯一红为既有object-omitted，未掩盖；后续又接入0137与契约迁移 |
| 最终本轮 `test_060*.py` | **615 passed / 1 failed / 2 skipped / 5 xfailed**，74.77秒 | 0124与0137全部通过，包括旧对象换回后的恢复不重跑；唯一红仍为object-omitted |
| 固定05a全节点 `nodes/experiment/tests` | **4104 passed / 1 failed / 21 skipped / 6 xfailed**，188.21秒 | 唯一红仍为object-omitted；不是全绿声明 |
| 固定05a根侧 outcome/产出接口11格及refusal登记 | **11 passed / 1 failed**，3.73秒 | 11条接口通过；共享refusal登记仍失败，未越界修改 |
| 会话2固定05a独立 `test_060*.py` | **615 passed / 1 failed / 2 skipped / 5 xfailed**，76.56秒 | 独立检出前后clean，原样反例/恢复和三条旧断言迁移接受；未跑UI/Slurm |
| 86be运行代码＋新增两格反向测试，与原method_contract成对运行 | **13 passed / 1 failed**，4.06秒 | 两条合法撤回通过，唯一红仍为object-omitted；未改生产/旧红例，不把正向约束算成已修复残余 |

根侧11格为 `tests/test_the_task_outcome_is_its_own_axis.py` 全部，以及
`tests/test_experiment_arch_gates.py` 的 `test_forwarded_frozen_artifact_does_not_satisfy_gate`、
`test_agent_reproduced_artifact_satisfies_gate`、`test_forwarded_then_overwritten_counts`。
只证明相应接口，不等于Core完成接线或三件套事务的全部保证。
`tests/test_refusal_sites_are_classified.py` 仍报告七个节点文件超共享登记基线，
为导出线已披露的跨owner事项；未修改根测试或registry。固定05a全部已跟踪节点Python
通过真实Python3.11 py_compile，diff检查通过。

会话2独立证据位于 `harness-framework-060-review-a1-window-ckjtXE/nodes/experiment/.tmp/`
`review-a1-current-window/060-05a29555.xml`，SHA256
`aa553bd1b714bcf01a9af390b593b99fb6ce4d6f7d08d00479b0965920249cb8`。
其范围结论与原命令见信箱142号；不把复核接受外推为整体发布批准。

初次尝试对已注册模块再次exec基线因工具重复注册退出，未进入测试，不计基线证据；
随后改为首次导入loader才取得上表8/5对照。原样0124两文件哈希与125一致；
0137测试文件SHA256为 `4cbbe3748a139e312cb03a91cca84cca5cf3dd7723c11e4fd44695ead30c5a6b`。
最终060套件同时覆盖真实本地执行、CMake、科学参数约束、交付事务和恢复；
完整节点与最小根接口结果已单列；这些仍不替代完整Core验收、真正模型UI或Slurm。

## 重要剩余与下一步

1. **object-omitted仍红**：失败producer留下正确字节后完全省略对象、只做content check，
   现有任意shell没有完整读取依赖，不能机械证明任务对象被绑定；不能靠永久锁旧C修复。
   新增 `test_060_check_only_correction.py` 保住相反路径：纯算术自检第一次误列了失败
   调试文件，第二次撤回它并保留同一真实check，必须可成功；失败记录仍在raw/clean，
   既有limitations不把它写成已恢复。此测试是已正确行为的防回归，不声称坏基线会红。
   会话2的145号确认同一check反向反例，并同意不追加历史C门；即使将来能观察真实读集，
   也不能仅凭“读过失败文件”证明当前任务必须交付该文件。当前未找到能闭合这个语义关系
   的既有事实接口；先保留红例、边界和正式required保护，不用参数路径抽取冒充物理观察。
2. 观察有界且非原子快照；不能宣称捕获所有外部并发改写。窗口前后已知变化不能漏掉。
3. 当前输出hash读取失败可能先走producer匹配失败而标成身份变化，尚未进入共同窗口
   的incomplete分支。这是142号静态指出的既有归因残余，未独立故障注入，不能声称所有hash缺口均已准确区分。
4. Core完成接线、共享登记、正式输入派发丢失、真实Slurm和完整scientific仍分开待验证/交接。
5. 下一步为固定代码两条原自然题及一条输入生成迁移题。独立UI脚本已准备，尚未部署；
   本轮模型凭据复用待用户确认，不读取或输出密钥。
   不往题面塞内部工具/收尾提示，不用driver自报PASS掩盖过程缺口。
