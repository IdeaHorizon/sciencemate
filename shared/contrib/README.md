# shared/contrib —— owner 共享贡献区

这是所有 node owner 都有 push 权限的公共目录（`.scope_map.yaml` 的 `_anyone`
键开放了 `shared/contrib/**`）。用途：**跨节点通用的 helper / 工具**——你写了个
对别的节点也有用的东西，又不该埋在自己 `nodes/<node>/` 里，放这里。

## 规则

1. **欢迎跨节点 helper**。纯函数库、通用工具（`register_tool` 形式）、共享
   数据处理逻辑都可以。节点强耦合的逻辑留在自己节点目录。

2. **PR 必须经 framework owner (wangd) review**。scope guard 放行
   `shared/contrib/**` 只解决"能不能 push 分支"，不代表免审——本目录的代码
   会被所有节点 import，质量门槛跟 `shared/tools/` 一致。

3. **工具名不得与 builtin / shared 工具重名**。重名策略由 registry 落地
   （`core/tool_registry.py` 的 `register_tool`：后注册覆盖先注册），意味着
   一个重名的 contrib 工具会**静默 shadow 内置工具**。所以硬规则是：
   注册前先 `grep -rn "name=\"<你的工具名>\"" shared/tools/` 确认无冲突；
   建议加前缀（如 `contrib_` 或按用途命名）避免撞名。review 时也会查这条。

4. **禁止 import `nodes/*`**。依赖方向只能是 `nodes/* → shared/contrib`，
   反过来会把某个 owner 的自留地变成全平台的隐式依赖（owner 重构自己节点
   就把别人 build 弄挂）。允许 import：`core/*`、`shared/lib/*`、标准库、
   项目声明的第三方依赖。

## 接线方式

本目录不被 `core.bootstrap` 自动扫描。要让工具真正注册进 registry，需要在
PR 里同时请 framework owner 在 `shared/tools/__init__.py` 加一行 import
（这是 owner review 的一部分，也是重名检查的最后闸门）。
