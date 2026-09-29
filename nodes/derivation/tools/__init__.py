"""derivation 节点专属代码入口（bootstrap 启动时自动 import）。

这里只有一个冻结门（derivation_contract）—— 演绎纪律的机械层。

**验证工具不在这里**：check_step / find_counterexample / dimensional_check /
limit_check 注册在 `shared/tools/library/derivation_check.py`，全节点共享。
这是刻意的分工 —— observation 合并效应量、experiment 核对理论预测、
hypothesis 定阈值时验个量纲，都是"顺手算一下"，不该为此起一个 producing run。

**工具共享，证据所有权独占**：能产 `derivation_log`（evidence_record）的
只有本节点，这条由 artifact_policy 的注册表机械保证（每个 evidence_record
类型必须有且只有一个 producing owner —— 无主的证据类型 = 谁都能凭空造证据）。
"""

from . import derivation_contract  # noqa: F401
