# 贡献指南

感谢你愿意参与这个项目。这份指南说明如何提交改动，以及提交前需要满足什么。

---

## 快速开始

```bash
git clone https://github.com/Nephets1010/tagmind.git
cd tagmind
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install    # 可选
make test
```

---

## 分支策略

| 分支 | 用途 | 保护 |
|---|---|---|
| `main` | 稳定版本，每次合并都通过全部 CI | 受保护 |
| `develop` | 集成分支，日常合并目标 | 受保护 |
| `feature/<描述>` | 新功能 | — |
| `fix/<描述>` | 缺陷修复 | — |
| `docs/<描述>` | 仅文档 | — |

- 从 `develop` 切出功能分支
- PR 目标为 `develop`
- `main` 由 `develop` 合并而来，合并前需打版本标签

---

## 提交信息

遵循 [Conventional Commits](https://www.conventionalcommits.org/)：

```
<type>(<scope>): <subject>

<body>

<footer>
```

**type**

| type | 用于 |
|---|---|
| `feat` | 新功能 |
| `fix` | 缺陷修复 |
| `refactor` | 重构（不改变外部行为） |
| `perf` | 性能改进 |
| `test` | 测试 |
| `docs` | 文档 |
| `build` | 构建系统 / 依赖 |
| `ci` | CI 配置 |
| `chore` | 其他杂项 |

**scope** 对应模块：`api` `graph` `risk` `research` `ingestion` `domain` `scheduler` `config`

示例：

```
feat(risk): 支持按风险等级过滤预警

新增 min_severity 查询参数，允许只返回 major 及以上的预警。
对应的 API 文档已同步更新。

Closes #42
```

---

## 代码规范

### 硬性要求

| 项 | 要求 |
|---|---|
| 类型标注 | 所有公开函数必须有参数与返回类型标注 |
| 行宽 | 100 字符（ruff 配置） |
| 导入顺序 | ruff isort 规则（`ruff check --fix` 自动修） |
| 格式 | `ruff format` |
| 文档字符串 | 所有模块、所有公开类与函数 |
| 中文注释 | 解释「为什么」，不复述「做了什么」 |

### 架构约束

**这是本项目最重要的约定。** 依赖只能向内指：

```
api · scheduler · observability · cli
              ↓
        risk · research
              ↓
           graph
              ↓
          ingestion
              ↓
           domain（零 I/O）
```

提交前请自检：

- [ ] 新增的 import 没有跨越层边界？
- [ ] 业务逻辑没有写进 `api/routers/`？
- [ ] `domain/` 里没有引入任何 I/O 或框架依赖？
- [ ] Cypher 没有在 `ingestion/` 或 `risk/` 里直接书写？（应走 `graph/queries.py` 或 repository）
- [ ] 新增的标签/关系类型在白名单里？（会自动跟随领域枚举，但要确认）

### 安全约束

- **绝不硬编码密钥**。所有敏感配置走 `pydantic-settings`，从环境读取。
- **绝不拼接用户输入到 Cypher**。标签与关系类型必须过白名单。
- **新增日志字段时确认不含敏感信息**。已注册的密钥会被自动脱敏，但不要依赖这一点来保护新引入的敏感值。
- **绝不把真实数据提交进仓库**。示例数据必须是合成的。

### 注释原则

```python
# 差：复述代码
# 遍历所有边
for edge in edges:
    ...

# 好：解释为什么
# 跳过已访问节点：图中存在环，担保链里 A→B→A 是常见形态
if target in visited:
    continue
```

---

## 测试要求

| 改动类型 | 必须补的测试 |
|---|---|
| 新增业务规则 | 单元测试（正常 + 边界 + 异常） |
| 修改现有行为 | 先写一个会失败的测试（复现问题），再改代码 |
| 新增 API 端点 | 集成测试（成功路径 + 错误路径） |
| 修 Bug | **回归测试**必须入库，防止复现 |
| 重构 | 现有测试必须全绿（重构不改变行为） |

测试位置：

- `tests/unit/` —— 纯逻辑，**不依赖任何外部服务**
- `tests/integration/` —— API 全链路，用内存图
- `tests/integration/test_neo4j_integration.py` —— 需要真实 Neo4j

命名约定：`test_<被测单元>_<场景>_<预期>`，例如 `test_parse_percent_accepts_chinese_suffix`。

---

## 提交前检查

```bash
make format      # ruff format + --fix
make lint        # ruff check
make typecheck   # mypy
make test        # pytest
```

CI 会跑同样的检查（mypy 暂为非阻断）。

---

## Pull Request

1. **从 `develop` 切分支**：`git checkout -b feature/xxx develop`
2. **小步提交**，每个提交都能独立通过测试
3. **补齐文档**：新功能需要更新 README 与 `docs/`
4. **写清楚 PR 描述**：
   - 解决什么问题
   - 怎么解决
   - 如何验证
   - 已知限制
5. **关联 issue**（若有）：`Closes #123`
6. **请求评审**

### PR 描述模板

```markdown
## 变更内容
（做了什么，一两句话）

## 动机
（为什么需要这个变更，关联 issue）

## 验证方式
- [ ] `make test` 通过
- [ ] 新增测试覆盖了正常/边界/异常路径
- [ ] 手动验证：<具体命令与观察到的输出>

## 破坏性变更
（有无？影响哪些接口？）

## 已知限制
（还有什么没做完的）
```

---

## 需要写 ADR 的改动

以下改动**必须**先写 ADR 并通过评审：

- 引入或替换一个核心依赖
- 改变分层架构或模块边界
- 改变数据模型（节点、关系、指标、溯源）
- 改变评分或传导模型
- 改变对外 API 契约
- 改变安全策略

ADR 模板见 [`docs/adr/README.md`](docs/adr/README.md)。

---

## 报告 Bug

请开 issue 并包含：

- 系统版本（`fin-kg --version`）
- Python 版本
- 复现步骤
- 实际结果 vs 预期结果
- 相关日志片段（**请先脱敏**）

---

## 行为准则

参与即表示同意 [行为准则](CODE_OF_CONDUCT.md)。
