# ADR-0008 · 标签树存储：邻接表 + 物化路径 + 祖先数组三写

| 项 | 值 |
| --- | --- |
| **状态** | 已接受 |
| **日期** | 2026-11-08 |
| **决策者** | 架构师 高见远（Gao） |
| **关联** | PRD §4.5（C1–C10 硬约束）、§4.6、§3.1.2 P4-TAG-001/003、§10-Q2；主理人决策 Q2 |
| **影响组件** | `src/tagmind/db/models/taxonomy.py`、`labeling/taxonomy.py`、`labeling/constraints.py`、`db/repositories/taxonomy_repo.py` |

---

## Context

PRD §4 定义了 12 层树状标签体系，并在 §4.5 给出了 **10 条硬约束**，其中 4 条直接依赖树结构：

| 约束 | 内容 | 对树结构的要求 |
| --- | --- | --- |
| **C1** 树层唯一性 | L1–L4 构成严格树；每层每条内容有且仅有该层主标签 | 需从任意标签**向上**回溯到根，核对每层是否唯一 |
| **C2** 同父互斥 | 同一父节点下的兄弟标签互斥 | 需取某标签的**兄弟集合**并求交 |
| **C3** 祖先闭包 | 命中第 k 层标签，必须同时携带 L1…L(k−1) 的全部祖先 | 需从任意标签**一次性**取全部祖先 |
| **C10** 版本一致性 | 一条内容的 labels 全部来自同一个 `label_def.version` | 需按版本组织标签集合 |

PRD §4.5 结尾给出了成本控制的关键判断：

> 约束执行位置：C1–C7、C10 为**确定性后处理校验**（在标签解析层，无 LLM 参与，零成本、可解释）；C8 需要 LLM 仲裁（异步任务）；C9 为**发布闸门**。这一划分是成本控制的关键——把 80% 的约束用规则解决，只有真正语义模糊的才花钱调 LLM。

因此树结构的设计目标非常明确：

> **让 C1/C2/C3 的校验全部成为单表 O(1) 判定。**

批量打标的吞吐是 SP-01 的要求（API 模式 ≥ 30 条/分钟，本地 ≥ 5 条/分钟/worker）。若每次校验都需要递归查询标签表，单条内容的校验成本将从微秒级上升到毫秒级，在 10 万条的批跑中会成为主要瓶颈（虽然不至于击穿 SP-01，但会让 60 s 的 P95 端到端延迟预算紧张）。

同时 PRD 还有几个相关约束：

- §3.1.2 P4-TAG-001：标签体系通过配置/DB 加载，**新增 L3 标签无需重启服务**，且「`label_def` 变更前后系统对同一内容的其余标签输出完全一致（回归对比，差异仅限新增标签）」。
- §10-Q2：总量 1200–1800。
- P4-TAG-006（US-12）：标签有稳定版本号，投放规则不会因模型升级而失效。
- ADR-0002 已定：标签体系多版本并存。

## Decision

**在 `label_def` 表中同时保存三种结构：邻接表（`parent`，真相源）+ 物化路径（`path`，派生）+ 祖先数组（`ancestors`，派生），并以「发布期校验 + 写入期触发器」保证三者一致。**

### 三写结构定义

| 结构 | 列 | 类型 | 语义 | 用途 |
| --- | --- | --- | --- | --- |
| **邻接表** | `parent` | `VARCHAR(64)` | 父节点的 `code`；L1 为 NULL | **真相源**。树的定义 |
| **物化路径** | `path` | `VARCHAR(1024)` | `D_TECH/C_AI/大模型与LLM/模型发布` | 一次查询取全路径；字符串前缀即子树 |
| 路径前缀 | `path_prefix` | `VARCHAR(1024)` | 祖先路径 `D_TECH/C_AI/` | `LIKE 'D_TECH/C_AI/%'` 高效取子树 |
| **祖先数组** | `ancestors` | `TEXT[]` | `['D_TECH', 'C_AI', '大模型与LLM']`（L1→L(k−1) 有序） | C3 的 O(1) 包含判定 |
| 根 | `root_code` | `VARCHAR(64)` | 所属 L1 的 code | 按领域分组统计、路由 |
| 层 | `layer` / `depth` | `SMALLINT` | 1–12 | 层配额判定 |
| 路径小写物化 | `path_text` | `TEXT` | `path` 的 lowercase | 前缀查询走 `text_pattern_ops` 索引 |

### 三者的一致性保证

**1. 发布期校验（`labeling/taxonomy.py: validate_release()`）**

新版本发布前，遍历全部节点断言：

| 断言 | 说明 |
| --- | --- |
| A1 | `layer = 1` ⟺ `parent IS NULL` |
| A2 | `layer > 1` ⟹ `parent IS NOT NULL` 且父节点存在且 `parent.layer = layer - 1` |
| A3 | 无环：对每条路径做深度优先遍历，路径长度 ≤ 12 且 `depth = layer` |
| A4 | `path` 与 `parent` 链一致（逐级拼接核对） |
| A5 | `ancestors` 与 `parent` 链一致（逐级核对） |
| A6 | `path_prefix` = `path` 去掉最后一段 + `/` |
| A7 | `root_code` = L1 祖先的 code |
| A8 | 总量在 1200–1800 区间（Q2） |
| A9 | 配额合法（`max_select ≤ 8`，L1/L2 必须为 1） |
| A10 | 互斥组内标签集合非空且不含自身 |

**任一断言失败 → 拒绝发布**（抛 `ConstraintViolation`，`details` 给出违规节点的 `code` 与具体断言）。

**2. 写入期触发器（`db/models/taxonomy.py`）**

新增/修改 `label_def` 行时，触发器从 `parent` 链**重算** `path` / `ancestors` / `path_prefix` / `root_code` / `depth` 并写入。

> **为什么用触发器而不是应用层维护？** 因为 `label_def` 可能被 `scripts/seed_demo.py`、管理 API、迁移脚本、未来的数据修复脚本以多种方式写入。触发器是**唯一**的收敛点，保证「无论谁写，三者都一致」。代价是触发器逻辑与 Python 侧有重复 —— 因此发布期校验（A1–A7）作为**双重保险**存在，两者任一失效都会被另一层发现。

**3. 只读快照缓存（进程内）**

`taxonomy_repo.load_tree(version)` 一次查询取全量（≤ 1800 行），在 `labeling/taxonomy.py` 中构建为不可变的 `LabelTree` 对象并进程内缓存。变更时通过 Redis pub/sub 失效（与 ADR-0006 的 CSR 快照同机制）。

`LabelTree` 提供 O(1) 查询接口：

```
tree.node(code) -> LabelNode              # dict 查找
tree.ancestors(code) -> tuple[str, ...]   # 直接读 ancestors 列
tree.children(code) -> tuple[str, ...]    # 预计算的 children 映射
tree.siblings(code) -> tuple[str, ...]    # 同 parent 的其他 code
tree.by_layer(layer) -> tuple[str, ...]
tree.path(code) -> str
```

`children` / `siblings` 是**预计算**的内存映射（构建时一次性算好），因此 C2 的兄弟集合判定也是 O(1)。

### 各约束的判定方式

| 约束 | 判定实现 | 复杂度 |
| --- | --- | --- |
| **C1** 树层唯一性 | 按 `ancestors` 分组统计每层的命中数；L1/L2 需恰好 1 条，L3/L4 需 ≤ 配额；多出的保留最高置信度 → 冲突日志 `parent` | O(k) |
| **C2** 同父互斥 | `tree.siblings(code)` 求交 | O(1) |
| **C3** 祖先闭包 | `set(ancestors) ⊆ hit_codes`；缺失则补齐并标 `source='derived'` | O(k) |
| **C5** 每层配额 | 按层计数 | O(k) |
| **C6** 显式互斥组 | `tree.exclusive_groups[group]` 查表 | O(1) |
| **C10** 版本一致性 | `content.labels_version` vs 各标签的 `def_version` | O(k) |

### 版本并存与 `code` 唯一性的处理

见 §16.2 A-01：PRD §6.2 把 `code` 标为全局 UNIQUE，与「跨版本不变 + 多版本并存」矛盾。

**决策**：`UNIQUE(version, code)` + `UNIQUE(version, path)` + 非唯一索引 `ix_label_def_code`（支持跨版本按 code 定位）。

**版本复制语义**：发布 v(n+1) 时，`INSERT INTO label_def (version, ...) SELECT 'v(n+1)', ... FROM label_def WHERE version = 'v(n)'`，然后应用变更。旧版本行**保留**（只把被删/被改的标记 `status='deprecated'`），因此：

- 历史内容的 `labels_version` 指向的旧版本行**永远可查** → US-03「改标签不影响已发布内容」成立；
- 回滚只需切换生效版本指针 → ADR 场景的 §12.2。

### 标签体系装载的生效版本

生效版本存于 `config/thresholds.py` 的 `LABEL_DEF_ACTIVE_VERSION` 常量 + `scripts/seed_demo.py` 写入的 DB 元数据。运行时以 DB 为准（因为 DB 才是版本行的来源），配置为兜底。

**灰度**（§12.6）：`TAGMIND_LABEL_CANARY_RATIO` + `TAGMIND_LABEL_CANARY_VERSION`，按 `hash(content_id) % 100 < ratio` 分流到灰度版本，其余走 active 版本。两个版本的树**同时在进程内缓存**（各自独立的 `LabelTree` 实例）。

## Consequences

### 正面

- **C1/C2/C3/C5/C6/C10 全部是单表 O(1) 判定**，零额外 IO。这直接支撑 §4.5 的「80% 约束用规则解决」原则与 SP-01 的吞吐目标。
- **单次查询取全路径**：`tree.path(code)` 是 O(1) 读列，不需要递归。
- **子树查询高效**：`WHERE path_prefix LIKE 'D_TECH/C_AI/%'` 走 `text_pattern_ops` 索引，一次拿到整棵子树。
- **祖先闭包校验极快**：C3 判定是集合包含检查，在 1800 节点树上全量校验 < 50 ms。
- **可解释**：物化路径让「这个标签在哪棵树下」在 UI 与日志里都是一行字符串。
- **多版本并存的实现成本低**：版本复制是一条 `INSERT ... SELECT`，不需要迁移历史数据。
- **触发器收敛了写入路径**，避免多处写入逻辑不一致。

### 负面 / 代价

- **存储冗余**：`path` + `path_prefix` + `path_text` + `ancestors` + `root_code` 五个派生列，约为「仅邻接表」的 3 倍存储。1800 行 × 约 200 字节 ≈ 360 KB —— **可忽略**。
- **触发器逻辑与 Python 侧重复**。触发器（PL/pgSQL）实现路径重算，Python 侧（`validate_release`）实现一致性断言。两处都要维护。
  - 缓解：触发器只做**重算**（简单、无歧义），Python 侧只做**断言**（复杂、可读性好）。职责分离，重叠面小。
- **移动/重命名节点需要重写路径**。若把 `C_AI` 改名为 `C_AI_ML`，其所有后代的 `path` 都要变。
  - 决策：**`code` 一经发布不可变**（ADR-0007 同样要求 code 跨版本不变）。改名 = 新建 code + 废弃旧 code（`status='deprecated'`）。这是正确的做法（稳定标识符原则），代价是运营需要知道「改名要走新建流程」。
- **删除节点困难**：有子节点的标签不能物理删除，只能 `status='deprecated'`。
- **1800 节点一次全量加载进内存**：约 2–5 MB，可接受。

### 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 触发器与 Python 校验逻辑不一致 | 触发器只重算、Python 只断言，职责不重叠；一致性校验脚本 V 系列的「标签树自洽检查」每日跑一次全量断言 |
| 有人手工 SQL 改 `parent` 绕过校验 | 触发器仍然重算派生列（所以不会产生「parent 与 path 不一致」）；但可能产生「跨层挂载」——由发布期校验 A2 拦截，且每日自洽巡检会报出来 |
| 版本复制时漏复制某节点 | `INSERT ... SELECT` 是全量复制，天然不会漏；发布后跑「版本间节点集合 diff」校验 |
| 灰度期两个版本的 `LabelTree` 缓存不一致 | 两个版本各自独立缓存 + pub/sub 失效；一致性巡检额外校验「灰度版本存在且节点数合理」 |

## Alternatives Considered

| 方案 | 为何被否 |
| --- | --- |
| **仅邻接表（`parent` only）** | ① C3 祖先闭包校验需递归查询，每条内容打标都要 N 次 IO（k ≈ 12 层）；② 取全路径需递归；③ 子树查询需递归 CTE，比前缀索引慢一个数量级。**虽然 1800 节点规模下递归 CTE 也不慢，但违反「80% 约束零成本」的定位。** |
| **仅物化路径（`path` only）** | ① 判定「同父互斥」需从 path 拆分字符串再找兄弟，逻辑易错；② 兄弟关系没有直接索引，需要额外的 `parent` 反查；③ C2 是高频约束，实现复杂度不该上升。 |
| **嵌套集合（nested set，`lft`/`rgt`）** | 理论上子树查询最快，但**移动节点需要重写整个子树的区间**，且在多版本复制场景下极易产生区间错乱；调试困难。 |
| **闭包表（closure table，边表 `ancestor`/`descendant`）** | ① 存储开销大（1800 节点 × 平均祖先数 ≈ 5 → 9000 行，还乘以版本数）；② 版本复制时闭包表也要复制；③ 收益相对「祖先数组」不明显（查询都是 O(1) 集合读）。 |
| **用 PostgreSQL 的 `ltree` 扩展** | ① 额外扩展依赖（与 ADR-0001「尽量少扩展」的取向一致，`vector` 是刚需，`ltree` 不是）；② 路径段长度受限（`ltree` 标签 ≤ 100 字符，中文标签名可能超）；③ 需要 `lquery` 函数，可读性对非专家不友好。 |
| **把树存在内存（从 YAML 加载），不入库** | ① 违反 P4-TAG-001「通过配置/数据库加载」与 NF-E03「零代码扩展、无需重启」；② 无法做版本并存与回滚；③ 投放侧（US-11）需要按标签查内容的数据库查询。 |
| **只存 JSON 树（单行 JSONB）** | ① 无法用 SQL 高效做「按标签 code 查定义」；② 无法对 `code` 建唯一约束；③ 与「运营通过管理台编辑单条标签」的诉求冲突。 |
| **用触发器保证一致，但不做发布期校验** | 触发器只保证「派生列与 parent 一致」，保证不了「parent 本身合法」（如 `layer=3` 挂到 `layer=1` 下）。发布期校验是必要的。 |

## 引用与追溯

- PRD §4.5（C1–C10 硬约束表 + 「80% 规则」的成本原则）、§4.1（层级总览）、§4.6（版本解耦）
- PRD §3.1.2 P4-TAG-001（配置化、零重启、回归对比）、P4-TAG-003（层级约束校验）、P4-TAG-006（版本稳定）
- PRD §10-Q2（总量 1200–1800）、§8.3 NF-E03（零代码扩展）
- ADR-0002（分片装载依赖树结构）、ADR-0007（同一 label_id 空间）
- `docs/architecture.md` §10.1（三写结构图）、§10.2（祖先闭包）、§10.3（C1–C10 校验器）、§10.5（发布与灰度）、§5.3（`label_def` DDL）
- `docs/architecture.md` §16.2 A-01（`code` 唯一性歧义）
