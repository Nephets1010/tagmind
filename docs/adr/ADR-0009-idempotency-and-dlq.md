# ADR-0009 · 幂等与死信：以数据库唯一约束为最终保证

| 项 | 值 |
| --- | --- |
| **状态** | 已接受 |
| **日期** | 2026-11-08 |
| **决策者** | 架构师 高见远（Gao） |
| **关联** | PRD §8.2 NF-R01~R04、§3.1.10 P4-OPS-003、§3.1.8 P4-API-001、§7.2 SP-09、§7.4 EE-08；主理人决策 Q8（Redis 分离支撑幂等设计） |
| **影响组件** | `src/tagmind/scheduler/celery_app.py`、`scheduler/tasks/labeling.py`、`scheduler/checkpoint.py`、`db/repositories/*.py`、`db/models/*.py`、`api/routers/content.py` |

---

## Context

PRD 的幂等与容错要求分布在四处：

| 需求 | 内容 |
| --- | --- |
| **NF-R01** 接入幂等 | `idempotency_key` 重复提交返回同一 `content_id`，不产生重复内容与重复标签 |
| **NF-R02** 至少一次投递 + 幂等消费 | Celery 任务可能重复执行，但结果一致（无重复标签、无重复曝光日志） |
| **NF-R03** 死信队列 | 最终失败任务进入 DLQ，可查询、可重放、可导出 |
| **NF-R04** 重试与退避 | 按配置指数退避重试（默认 3 次），区分可重试（5xx/超时）与不可重试（401/403/参数错） |
| **SP-09** | 幂等成功率 100%（重复提交返回同一 `content_id` 且无重复行） |
| **EE-08** | 流水线幂等重放成功率 100%（重复执行同一 `content_id` 产生相同结果） |
| **P4-OPS-003** | 1000 条批量打标；人为注入 5% LLM 失败 → 重试后进 DLQ，DLQ 列表可见并可重放；DLQ 每条含原始 payload、失败原因、已重试次数；任务处理具备幂等性 |
| **P4-API-001** | 同一 `idempotency_key` 重复提交返回**同一** `content_id` 且不产生重复行 |

核心技术矛盾：

> **Celery 的投递语义是「至少一次」（at-least-once）**。任务可能因 worker 崩溃、连接超时、`SIGKILL` 而被重复投递。而打标流水线会产生**多张表的写入**（`content.labels`、`label_conflict_log`、`llm_call_audit`、`review_task`、`content_vector`、`exposure_log`）。要保证「重复执行结果一致」，必须有机制阻止重复写入。

同时还有一个更隐蔽的约束：**`content.labels` 是一个 JSONB 数组，它不是「追加式」的**。重复执行打标如果直接覆盖，会丢失历史（PRD §6.0 原则 1：「原始数据不可变，一切修正通过新版本记录表达」）。所以幂等不只是「不产生重复行」，还要「不破坏历史」。

## Decision

### 决策 1 · 三层幂等，逐层递进

| 层 | 机制 | 保证的语义 | 实现 |
| --- | --- | --- | --- |
| **L1 接入层** | `content.idempotency_key` **UNIQUE 约束** | 同 key → 同 `content_id`，不产生重复内容 | `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING` + `SELECT` |
| **L2 任务层** | 状态判定 + 目标状态短路 | 同一 `content_id` 重复执行不产生重复标签/日志 | 写前检查 `status` 与 `pipeline_version` 是否已是目标状态 |
| **L3 存储层** | 各表业务唯一约束 | 即使 L2 被绕过，DB 也不会产生重复行 | 见下表 |

**L3 的具体唯一约束**

| 表 | 约束 | 防止的重复 |
| --- | --- | --- |
| `content` | `UNIQUE (idempotency_key)` | 重复内容 |
| `llm_call_audit` | `UNIQUE (llm_call_id)` | 重复审计记录 |
| `label_conflict_log` | `UNIQUE (content_id, pipeline_version, conflict_type, candidates_hash)` | 同一版本同一类型的重复冲突日志 |
| `review_task` | `UNIQUE (task_type, target_type, target_id)` | 重复复核任务 |
| `review_record` | 触发器禁止 UPDATE/DELETE | 轨迹被改写 |
| `annotation` | `UNIQUE (set_id, content_id, annotator)` | 重复标注 |
| `label_drift_check` | `UNIQUE (check_week)` | 重复周检 |
| `exposure_log` | `UNIQUE (request_id, content_id, action, ts)` | 重复曝光记录 |

> `label_conflict_log` 的唯一键需要包含 `candidates_hash`（候选集的哈希）—— 因为同一版本下同一内容可能有多个不同类型的冲突，且 `parent`/`mutual`/`exclusive` 各自可能多条。

**L2 的状态短路伪代码**

```
def execute_tagging(content_id, pipeline_version):
    content = repo.get(content_id)
    if content.status == 'approved' and content.pipeline_version == pipeline_version:
        return SKIPPED_ALREADY_DONE        # 幂等短路
    if content.status == 'processing' and checkpoint.is_running(content_id):
        return SKIPPED_IN_PROGRESS
    # ... 正常执行
```

**关键点**：短路条件是「**目标状态 + 目标版本**」都匹配。若版本变了（如重打标），则不短路 —— 因为那是一次**新的**打标，应产生新的 `labels` 与新的 `label_raw`，而不是丢弃。

### 决策 2 · 幂等与「不可变历史」的兼容

PRD §6.0 原则 1 要求原始数据不可变，修正通过新版本记录表达。幂等重放时如何既不重复写、又不丢历史？

| 场景 | 处理 |
| --- | --- |
| **完全相同的重放**（同 `content_id` + 同 `pipeline_version` + 同后端 + 同 prompt） | **完全短路**，不写任何表。结果与首次执行逐位一致（EE-08）。 |
| **版本变化的重放**（改了 prompt 或模型或标签体系） | **不短路**。写入新一版 `labels`（覆盖 `content.labels`），但 `label_raw` 保留为 `JSONB 数组`（按 `pipeline_version` 分版本并存），`llm_call_audit` 新增记录，`label_conflict_log` 新增记录。历史通过 `pipeline_version` 可完整回放。 |

**因此 `content.label_raw` 的结构是一个数组**：

```jsonc
[
  { "pipeline_version": "clean_v1|ld_v1|pv1|det-v1|cons_v1|ts_v1",
    "backend": "deterministic", "raw_text": "...", "parsed": true, "at": "..." },
  { "pipeline_version": "clean_v1|ld_v2|pv1|det-v1|ts_v1",
    "backend": "llm_api", "raw_text": "...", "parsed": true, "at": "..." }
]
```

这同时满足：① 幂等重放不丢历史；② P4-TAG-003 的「`label_raw` 保留原始输出可追溯」；③ PRD §6.0 原则 1。

### 决策 3 · 事务边界（详见架构 §14.5）

| 规则 | 说明 |
| --- | --- |
| **LLM 调用在事务外** | 打标时，LLM 调用绝不能在数据库事务内。否则长事务持有行锁，1000 条批跑时会耗尽连接池（PG 默认 100 连接） |
| **单仓储方法 = 单事务** | 跨仓储的业务操作由编排层用 `with session.begin():` 包裹 |
| **批量提交粒度 50** | `TAGMIND_LABEL_BATCH_SIZE=50`。单条失败不阻断整批（NF-R06） |
| **IntegrityError 不重试** | 唯一约束冲突 = 幂等重放，视为成功，直接返回既有行 |
| **OperationalError 重试 3 次** | 死锁/序列化失败，指数退避 |

### 决策 4 · DLQ 设计

**落地方式**：`celery_app.py` 配置 `task_acks_late=True` + `task_reject_on_worker_lost=True` + `task_max_retries=3` + `task_retry_backoff=True` + `task_retry_jitter=True`。超过重试上限的任务进入 `dlq.<queue_name>` 队列。

**DLQ 记录结构**（每条必须含 PRD P4-OPS-003 要求的三项）：

| 字段 | 内容 |
| --- | --- |
| `original_payload` | 原始任务参数（`content_id`、`pipeline_version`、幂等键等） |
| `failure_reason` | 失败原因 + 最后一次异常类型 + 错误码 |
| `retry_count` | 已重试次数 |
| `dlq_id` / `failed_at` | DLQ 记录 ID 与失败时间 |
| `replay_count` | 已重放次数 |
| `last_error` | 最后一次异常的完整文本（截断 2 KB） |

**DLQ 存储**：Redis `/1` 的 `dlq:<queue>` 列表（与 broker 同库但不同 key 空间），同时**镜像写入 PostgreSQL 的 `label_conflict_log` 之外的独立结构** —— 决定：DLQ 用 Redis 列表 + 一个 `dlq` 命名的 PostgreSQL 表？为避免新增第 17 张表，**DLQ 只用 Redis 列表**，并提供 `tagmind dlq list --format json` 导出为文件（满足 NF-R03「可导出」）。

**重放语义**：

| 项 | 规定 |
| --- | --- |
| 入口 | `tagmind dlq replay --task labeling --ids d1,d2 --limit 100` |
| checkpoint | **强制绕过 checkpoint**（因为失败任务的 checkpoint 可能停在半途） |
| 幂等保护 | 靠 L2 状态短路 + L3 唯一约束。若内容已是目标状态，直接跳过并记 `replayed_but_skipped` |
| 批量 | 支持 `--limit` 与 `--dry-run` |

### 决策 5 · Checkpoint 是加速，不是保证

| 项 | 规定 |
| --- | --- |
| 存储 | Redis `/0`，key = `ckpt:{task}:{idempotency_key}` |
| Value | `{"stage": "tagged", "pipeline_version": "...", "at": "..."}` |
| TTL | 86400 s；任务完成即删 |
| 语义 | **仅用于跳过已完成的阶段以加速**。正确性由 DB 唯一约束保证 |
| 失败处理 | 失败时**不删** checkpoint（保留现场供排查），DLQ 重放时强制绕过 |

**为什么 checkpoint 不能作为保证**：Redis 可能被 `FLUSHDB`（见 ADR-0006），也可能因 TTL 过期而丢失。若把幂等建立在 checkpoint 上，`FLUSHDB` 后重放会产生重复标签。把保证建立在 DB 唯一约束上，`FLUSHDB` 也不影响正确性。

### 决策 6 · 错误分类与重试矩阵

| 错误 | 分类 | 重试 | 终态 |
| --- | --- | --- | --- |
| LLM 5xx | `RETRYABLE` | 指数退避，≤ 3 次 | DLQ |
| LLM 超时 / 连接错误 | `RETRYABLE` | 指数退避，≤ 3 次 | DLQ |
| LLM 401 / 403 | `FATAL` | **不重试** | **直接 DLQ + 告警「密钥失效」** |
| LLM 上下文超长 | `FATAL`（先尝试缩减分块二次调用） | 二次调用 1 次 | DLQ |
| JSON 解析失败 | `PARSE` | 先走修复性解析；仍失败则重试 1 次 | DLQ |
| DB 唯一约束冲突 | `IDEMPOTENT` | **不重试**，视为成功 | 返回既有行 |
| DB 死锁 | `RETRYABLE` | 指数退避，≤ 3 次 | 失败（不回 DLQ，因为可安全重试） |
| 约束无法自动修复 | `DOMAIN` | **不重试** | `status='needs_review'` + `review_task` |

**指数退避**：`delay = base * 2^(n-1) + jitter`，base = 2 s，jitter 为 0–1 s 随机（避免惊群）。总最坏耗时 ≈ 2 + 4 + 8 = 14 s。

## Consequences

### 正面

- **幂等是 DB 级保证，不是应用级约定**。无论哪条写入路径（API、CLI、DLQ 重放、管理台强制重打）都受同一套唯一约束保护。
- **Redis 故障不影响正确性**。`FLUSHDB`、TTL 过期、Redis 挂掉都只影响性能（checkpoint 失效 → 阶段重跑 → 但结果一致）。
- **「不产生重复」与「保留历史」同时成立**。`label_raw` 按版本数组化，重放不丢历史。
- **DLQ 可查可重放可导出**（NF-R03），且重放安全（幂等）。
- **部分失败隔离**（NF-R06）：批量提交粒度 50，单条失败不影响其他。
- **5% LLM 失败注入场景可测**：DLQ 深度 = 失败数，可断言。

### 负面 / 代价

- **`label_raw` 变成数组**，比单对象复杂。读取时需要「取最后一个版本」或「按版本查」的辅助函数。
- **唯一约束冲突的异常处理要小心**：不能笼统地把所有 `IntegrityError` 当幂等。必须**检查约束名**，只对已知的幂等约束走「返回既有行」，其他约束冲突仍然报错。
- **`content.labels` 被覆盖**（版本变化的重放时），严格说不是「不可变」。但 PRD §6.0 原则 1 的本意是「原始数据不可变」（`raw_text`、`label_raw`），结论（`labels`）可以被新版本覆盖 —— 因为 §6.0 原则 2 说「结论必带来源与版本」，而 `content.labels_version` + `label_raw` 数组已经完整保留了历史。**必须在文档中明确这一解读。**
- **DLQ 只在 Redis 里**，Redis 重启会丢 DLQ（除非开启 AOF/RDB）。这是可接受的（DLQ 是运维缓冲，不是长期存档），但要在 runbook 中写明「DLQ 需定期导出」。

### 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 唯一约束被误当成幂等信号 | 只对**白名单内的约束名**走幂等路径，其余抛 `DatabaseError` |
| 并发提交同一 key 的竞态 | `ON CONFLICT DO NOTHING` + `SELECT` 的组合在 PostgreSQL 下是原子的；若 `SELECT` 仍为空（极罕见的可见性延迟），重试 3 次后报错 |
| `content.labels` 被覆盖导致历史丢失 | `label_raw` 数组保留全部版本；一致性校验 V 系列的「版本可回放」检查 |
| DLQ 积压 unnoticed | Prometheus 指标 `tagmind_dlq_depth` + 告警规则「DLQ 非空」（NF-O05 明确要求） |
| checkpoint 残留导致跳过 | checkpoint 带 TTL；DLQ 重放强制绕过；「内容已是目标状态」时才允许跳过 |
| Redis 挂了导致 checkpoint 不可用 | checkpoint 读写全部 try/except 包裹，失败时**当作无 checkpoint**（退化为全流程重跑，正确性不受影响） |

## Alternatives Considered

| 方案 | 为何被否 |
| --- | --- |
| **只依赖应用层检查（SELECT 后判断再 INSERT）** | ① 并发下有竞态（两个 worker 同时 SELECT 都为空 → 都 INSERT → 重复）；② 不是数据库级保证，依赖所有写入路径都正确实现。**唯一约束是唯一的正确做法。** |
| **只依赖 Celery 的 `task_acks_late`（不重试）** | ① Celery 的至少一次语义无法避免重复投递；② worker 崩溃时任务会被重新投递，业务必须能承受重复。 |
| **用 `task_id` 去重（Redis SETNX）** | ① Redis 不可靠（`FLUSHDB`、TTL、重启）；② 正确性不应依赖可选组件；③ 与 ADR-0006 的 Redis 分库职责冲突。 |
| **用 PostgreSQL advisory lock 串行化同 content 的处理** | ① 锁只保证不并发，不保证不重复写；② 长事务持锁影响吞吐；③ 需要额外的锁表与清理机制。 |
| **在打标前 DELETE 再 INSERT（覆盖式）** | ① 破坏历史（PRD §6.0 原则 1）；② 删除与插入之间有窗口，崩溃会导致数据丢失；③ 无法满足「保留修改前值」的复核要求（P4-RAG-004 的 `resolved_before`）。 |
| **把 DLQ 存 PostgreSQL 表** | 需要新增第 17 张表（PRD §6 是 12 个逻辑分组 / 16 张物理表）。用 Redis 列表 + 导出功能已满足 NF-R03 的「可查询、可重放、可导出」。 |
| **DLQ 重放时也走 checkpoint 短路** | 失败的半途 checkpoint 会导致重放被跳过，任务永远无法完成。**必须绕过。** |
| **对所有 IntegrityError 一律当幂等** | 掩盖真正的数据错误（如 NOT NULL 违反、类型错误），导致静默丢数据。必须按约束名白名单处理。 |

## 引用与追溯

- PRD §8.2 NF-R01~R04（幂等、至少一次、DLQ、重试退避）、§8.2 NF-R06（部分失败隔离）
- PRD §3.1.10 P4-OPS-003（1000 条批量、5% 失败注入、DLQ 可查可重放、幂等）
- PRD §3.1.8 P4-API-001（幂等键返回同一 `content_id`）
- PRD §6.0 三条设计原则（原始不可变、结论带版本、UTC）
- PRD §7.2 SP-09（幂等成功率 100%）、§7.4 EE-08（幂等重放成功率 100%）、§7.4 EE-09（LLM 审计完整率 100%）
- ADR-0006（Redis 分库 → checkpoint 与 DLQ 的归属）
- `docs/architecture.md` §6.5（打标幂等与状态机）、§9.4（checkpoint 规范）、§14.5（事务边界）、§5.2–§5.7（DDL 与唯一约束）
