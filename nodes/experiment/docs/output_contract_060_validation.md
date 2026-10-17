# 060 自然任务交付共同契约修复

## 范围与基线

- 基线：`493a0f1b95afcfebf1ae15f0353bcf790a65ee5f`。
- 独立实现分支：`codex/060-output-contract`，无 upstream；不更改运行旧 UI 的源树。
- 仅修改 Experiment 节点；Core、共享协议和根目录测试/登记表只读。
- 本文件报告代码修复与回归，不把单元/真进程测试冒称为模型/UI 通过。

## 共同规则

1. **A 不被方法改写。** 保存收到的完整任务及来源，正式 required 独立生效。
   `requirement` 是忠实解释，不用逐字包含认证；它也不是任意自然语言语义已获证明。
   父节点的 `experiment_spec` 不冒称原始用户逐字输入。
2. **B 使用实际边界。** 路径先用 Core 的工具坐标；shell 路径仍按实际 cwd。
   业务观察范围来自本次实际 writable roots 与业务角色交集，不反向扩大写权限。
   框架状态、私有 scratch 不算业务产物；预算、扫描错误与已知重叠归属如实披露。
3. **C 同时核验对象与方法。** `deliverables` 及明确检查对象参与同一求值；换
   file/check 不会让同一对象的未知/失败来源消失。正式文件要求仍独立核验。
   已有只读基线/依赖参考保留其输入身份，不强迫重新生成；无文件自检不造文件。
4. **收尾仍只有 finish。** 同一评价进入已有三件套事务；保存/冻结工具的既有
   content_contract 和启动说明提前指向 finish，不增加第二条交付入口。

没有恢复 scope 门、旧路线、登记工具或新的模型内部 ID。

## 反例与验证

在尚未修改生产代码的 493a 基线上：

- 真实文件生产/检查矩阵：3 红 / 4 绿。失败或未知来源的对象由 file 改成 check-only
  会误成功；正常产物、已有输入审计和无文件自检作为正对照。
- 实际 Project + 真 Bash/Python：5 红 / 3 绿。合法父/兄弟目录输出漏观察，以及
  150 创建 + 100 修改的合计身份预算未披露；真实越界写拒绝仍成立。
- 同一观察根的两个真实并发进程：修复前 1 红，不能把两个结束快照都称确定 producer。
- before 扫描开始前意图必须持久化：加载基线函数的独立进程 2 红，修后 2 绿。
  基线函数只在测试进程加载，没有临时覆盖生产磁盘。

新增回归位于 `test_060_output_contract_counterexamples.py`、
`test_060_output_capture_boundary.py`、`test_060_output_path_contract.py`、
`test_060_output_contract_guidance.py`。包含合法纠正不重跑载荷、正式 required 不能豁免、
同字节改写不洗白失败、Project/非 Project 坐标、只读外部 cwd、symlink 边界、
私有 scratch 排除、扫描缺失、并发归属、初始工具说明及 clean 视图披露。

旧测试维护分三类，不能混称“测试本来写错了”：

- 路径夹具从 State.root 隐藏坐标迁到 Core 工具锚点或实际绝对路径，原语义断言保留。
- 两项经反例确认的旧断言更新：逐字引句认证、重叠快照冒称确定 producer。
- 旧模型面测试禁止所有三件套 content_contract，现改为允许唯一入口说明但禁止
  旧协议/手填 schema。理由是活体已发生先手工保存、撞门后才知道 finish。

验证命令（在本分支根目录，使用既有 Python 环境）：

```bash
PYTHONPATH=. python -m pytest -q nodes/experiment/tests --tb=short
PYTHONPATH=. python -m pytest -q \
  tests/test_model_commands_reach_the_os_through_one_throat.py \
  tests/test_refusal_sites_are_classified.py \
  tests/test_instructed_tools_are_in_whitelist.py --tb=short
git diff --check
```

根门禁本轮为 **17 passed / 2 failed**，失败内容与基线披露一致：共享 refusal registry
未登记的四个文件计数，以及八个已退役工具的静态旧引用。one-throat 通过。本轮没有
修改根测试或共享登记文件，也不宣称仓库可无条件合并。

最终节点全量：**4000 passed / 21 skipped / 6 xfailed**，174.44 秒。
skip/xfail 如实保留，不当作已验证；`git diff --check` 通过。
最终全量前已修正入口说明测试，未通过改业务成功/失败断言掩盖缺陷。

## 能力边界与后续活体验证

- 任意 shell check 没有完整文件读取依赖观测，不能机械证明它充分检查了所指对象。
- 输入参考的身份观测只证明核验期间稳定，不证明自 run 开始一直未变；这需要原始
  hash、上游身份或真实对照检查。
- before/after 仍是有界观察；已记账的同 run 重叠会披露 unknown，不宣称已检测全部
  跨 run 或外部写者。没有平台级隔离/写入追踪能力时不能冒称有。
- 超预算对象没有被默认为已知 producer，也没有通过增大常数隐藏缺口。
- 既有 Core completion 交接缺口、真正 Slurm/GPU 路径不由这次本地测试证明解决。
- 下一轮以干净固定提交、原 CMake/tilepack 题面在独立 UI/API 验证，分别报告业务、
  节点交付/来源与流程摩擦。旧现场保留，不以本次重跑倒写旧 verdict。

本机持久日志：`nodes/experiment/.tmp/output-contract-validation/`（不入 git）。
