# 通用诊断模式库 (Diagnose Patterns)

**设计原则**: 数据与代码分离，模式不绑定特定工具，通用分类适用于所有 HPC/AI 应用。

## 文件结构

```
diagnose_patterns/
├── README.md                    # 本文档
└── diagnose_patterns.yaml       # 所有错误模式（56 个）
```

## 使用方式

### Python API

```python
from tools.diagnose import DiagnoseEngine, analyze_log

# 方式 1: 自动加载所有 YAML 模式
engine = DiagnoseEngine.from_yaml_patterns()
report = engine.analyze_log("/path/to/build.log")

# 方式 2: 便捷函数（推荐）
from tools.diagnose import analyze_log

# 分析日志（自动使用所有 56 个模式）
report = analyze_log("build.log")
```

### 模式格式

```yaml
schema_version: "1.0"
description: "通用 HPC/AI 应用错误诊断模式"

patterns:
  - id: "unique_pattern_id"
    regex: '正则表达式'
    category: "compilation"      # compilation/linking/runtime/configuration/environment
    severity: "error"            # critical/error/warning/info
    confidence: 0.9              # 0.0-1.0
    description: "人类可读描述"
    generic_fix: "通用修复建议"
    auto_fixable: false          # 是否可自动修复
    context_hint: "额外上下文提示"
    suggested_fix:               # 自动修复建议（如 auto_fixable=true）
      type: "compiler_flag"      # compiler_flag/linker_flag/source_edit/environment/shell_command
      flag: "-fallow-argument-mismatch"
```

## 模式分类

| 分类 | 数量 | 描述 | 示例 |
|------|------|------|------|
| `compilation` | 12 | 编译阶段 | 编译错误、语法错误、类型不匹配 |
| `linking` | 8 | 链接阶段 | 未定义引用、库未找到、符号冲突 |
| `runtime` | 16 | 程序执行阶段 | 段错误、浮点异常、CFL违反 |
| `configuration` | 9 | 配置/输入验证 | namelist错误、XML解析、参数范围 |
| `environment` | 10 | 环境准备 | 路径未设置、权限不足、模块缺失 |
| **合计** | **55** | | |

## 模式设计原则

### 1. 通用性优先

❌ **错误**: 绑定特定工具
```yaml
# 不要这样
pattern: "CESM.*glc_constants.F90.*stdout"
```

✅ **正确**: 通用模式
```yaml
# 这样更好
pattern: "symbol.*not exported|stdout.*not declared"
description: "符号未导出或未声明"
```

### 2. 可修复性分级

| 级别 | 说明 | 行动 |
|------|------|------|
| `auto_fixable: true` | 可安全自动修复 | 添加编译器标志、改环境变量 |
| `auto_fixable: false` | 需要人工决策 | 提供上下文，由人判断 |

### 3. 添加新模式

在 `diagnose_patterns.yaml` 的对应分类下添加：

```yaml
  - id: "my_new_pattern"
    regex: '错误正则'
    category: "compilation"
    severity: "error"
    confidence: 0.85
    description: "描述"
    generic_fix: "修复建议"
    auto_fixable: false
```

## 来源

- **CESM2.2** 全耦合构建（16+ 项修复）
- **LAMMPS GPU** 编译（KOKKOS → 原生 GPU 包迁移）
- **WRF** 运行时诊断
- **通用 HPC 最佳实践**

所有模式都已去工具化，适用于任何使用类似技术栈的 HPC/AI 应用。
