# CLI 终端界面

维护者：nidy。CLI 保留为次要交互界面，Desktop 是主要交互产品。这里维护终端呈现和交互适配，执行策略继续复用框架。

## 文件职责

| 文件 | 职责 |
| --- | --- |
| `__main__.py` | `python -m cli` 的启动入口 |
| `chat.py` | REPL、输入事件分流、界面生命周期、进度和流式回调接线 |
| `terminal.py` | 提示符、颜色、横幅、回复、工具进度、思考状态和人工输入提示 |
| `input.py` | readline、中文及多行粘贴、stdin 监听；仅启动终端时开启行编辑 |
| `commands.py` | 斜杠命令和帮助文本，调用框架现有操作 |
| `tests/` | 终端交互和呈现的回归测试，随 UI 一起维护 |

```bash
python -m cli --project my-project --session terminal-session
# 旧入口仍兼容，参数相同：
python chat.py --project my-project --session terminal-session
```

`--session` 为 CLI 使用独立的 Session 工作区。现有 `--autonomy` 和 `--continuous` 参数继续可用，权限及自治语义由框架决定。`hf` 诊断命令与 `run_node.py` 节点工具不属于终端 UI。

## 修改边界

终端布局、提示、输入处理、命令语法、进度展示、文档和对应测试均放在此目录。新增交互复用框架操作；需要新的执行能力时，由框架维护者提供共享接口，再从 CLI 接线。

Session 状态、agent loop、节点派发、自治与权限策略、记忆、KB、暂停消费、停止及恢复逻辑仍在框架。根目录 `chat.py` 暂时保留共享会话实现和旧入口兼容，不归 CLI owner 修改。本次拆分保持原有执行语义，未另写一套 Runtime。

平台 worker 导入共享会话模块时不会启动 CLI、加载终端模块或开启 readline。终端输出仅在 CLI 运行期间接入，退出时恢复。

## 测试

```bash
python -m pytest cli/tests -q
python -m cli --help
python chat.py --help
```

CI 显式运行此目录的测试。框架执行及权限的集成回归留在 `tests/`。nidy 原有 `nodes/analysis/**` 范围保留，新增 `cli/**`；权限配置、共享运行时和其他目录不随之放开。
