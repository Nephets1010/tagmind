# tagmind 技术债排查与低风险优化审计报告

| 项 | 值 |
| --- | --- |
| 审计日期 | 2026-11-10 |
| 基线提交 | `9e61209`（`main`） |
| 范围 | 当前代码库与配置（不改业务需求、不新增功能） |
| 方法 | 三方独立只读审查（产品契约 / 架构性能 / 稳定性边界）+ 主理人独立复核 + 工程师实施 |
| 环境限制 | **本机无 PostgreSQL、无 Docker**；凡涉及真库/容器的动态验收一律标「未验证」 |

---

## 1. 排查结论概览

| 来源 | 新增问题 | Blocker | Major | Minor | 可立即修复 | 需确认 |
| --- | --- | --- | --- | --- | --- | --- |
| 既有 QA 报告 `docs/qa/phase0-report.md` | 21 | 3 | 8 | 10 | — | — |
| 产品经理（业务流程 / 需求契约） | 21 | 2 | 14 | 5 | 12 | 9 |
| 架构师（架构 / 性能 / 可维护性） | 17 | 1 | 10 | 6 | 7 | 10 |
| QA（稳定性 / 边界 / 行为一致性） | 15 | 0 | 7 | 8 | 11 | 4 |
| **本轮新增合计** | **53** | **3** | **31** | **19** | **30** | **23** |

> 三方审查均被要求**排除既有 QA 报告的 21 项**，故新增项与既有项不重复计数（少量交叉引用已在各自报告标注）。
> 另有 13 项属「因无 PG/Docker 无法动态验证」或「业务链路尚未接线」，一律**不标为通过**。

**核心判定**：阶段 0 的地基代码质量合格，但存在三类系统性问题：

1. **门禁假绿**：CI 的 `test` job 无 PG，39 条集成测试恒 skip；`mypy` 增量缓存掩盖真实类型错误；CI lint 只查 `src tests` 漏掉 `scripts/` 与 `migrations/`。
2. **宣称与实现落差**：README 用现在时描述阶段 2–4 能力、固化已过期的指标数字、把未验证项写成已交付；PRD 与架构文档存在阶段归属与版本口径矛盾。
3. **边界与安全缺口**：日志堆栈明文泄漏密钥、`TokenBucket.consume(-1)` 可反向补充令牌、`/readyz` DB 已断仍返回 200、配置护栏可被直接构造绕过、`BaseRepository` 对非 `id` 主键确定性崩溃。

**业务流程断点（PM 结论）**：主链路 10 环中 **0 环端到端可跑通**；「已有代码」集中在横切地基与标签体系装载，业务环节多为「仅有模型」或「完全缺失」。
本报告**不把「阶段 1–4 尚未实现」本身当作缺陷**，只处理「未实现却宣称已实现」与「决策无记录」。

---

## 2. 已实施优化（全部为明确、低风险、可验证项）

### 2.1 门禁与工程质量（工程师实施 + 主理人复核）

| ID | 问题 | 修复 | 验证 |
| --- | --- | --- | --- |
| D-01 / A1 | `interaction_repo.py:143` 类型错误（`ReturningInsert[int]` 赋回 `Insert`）；增量 mypy 假绿 | 拆成 `insert_stmt` / `conflict_stmt` / `returning_stmt`，SQL 语义不变 | `mypy --no-incremental` 干净 |
| D-03 / A5 | `BaseRepository` 7 处硬编码 `self.model.id`，对 `UserProfile`(PK=user_id)、`ContentVector`(PK=content_id)、`ExperimentAssignment`、`ExposureLog` 确定性崩溃 | 新增 `_pk_columns()` / `_pk_names()` / `_pk_condition()` / `_default_order_by()` / `_is_new_entity_id()`；复合主键用 `and_()`，无法支持的路径保持**显式可诊断拒绝** | 修复前后最小复现对比：`AttributeError`/`TypeError` → `OK`，复合主键 → `DatabaseError: 复合主键请使用专用仓储方法` |
| D-11 / A4 | 覆盖率 67.21% < 70%，`ingestion/pii.py`、`rules.py` 零覆盖 | 新增 `test_pii.py`(222 行)、`test_rules.py`(273 行)、`test_base_repository_pk.py`(378 行) | 覆盖率 67.21% → **77.30%** |
| A2 / A3 | `SIM110`、`UP035` 两处 lint | 语义等价改写 | `ruff check .` 干净 |
| D-07 | 跨月分区集成测试写死 2026-01/06，真 PG 上必失败；`len(Result)` 用法错误 | 按 `datetime.now(UTC)` 动态构造「上月/本月」；改 `.all()` | 静态复核（本机无 PG，未动态跑） |
| — | `ruff 0.17` 会格式化 Markdown 内的 Python 代码块，导致 QA 报告被无意义重排 | `pyproject.toml` 加 `extend-exclude = ["*.md","docs/**/*.md"]` | `ruff format --check .` 干净 |

### 2.2 架构与可观测性（架构师审查 7 项）

| ID | 问题 | 修复 | 验证 |
| --- | --- | --- | --- |
| A-01 **(Blocker)** | `/readyz` 是 `async def` 却同步调 `ping()`，DB 抖动时阻塞事件循环（实测 `/healthz` 并发延迟 404.7ms） | 改 `await run_in_threadpool(ping)`，保持 async 签名与响应体 | 新增 `test_ping_is_dispatched_to_threadpool` |
| A-02 | 中间件把 request_id 放进 `request.state` 却从未调 `bind_context`，contextvar 恒空、所有日志无 `trace_id` | 中间件改用 `context_scope(request_id=..., trace_id=...)` | 实测日志现含 `request_id` / `trace_id`，且与响应头一致 |
| A-03 | `tag_vector` GIN 用 `jsonb_path_ops`（只支持 `@>`）但查询用 `?\|`，索引不可用 → 10 万行全表扫 | 迁移 0003 去掉 opclass，改用默认 `jsonb_ops` | 静态核对；**已部署环境需手工重建索引**（迁移不重跑） |
| A-12 | `bind_context` 多字段只返回单个 token，`reset` 只恢复第一个，其余泄漏 | 新增 `context_scope()` contextmanager，逆序 reset 全部 token；`bind_context` 签名不变并加 Warning | 新增 3 条测试（正常/异常/相邻作用域不串号） |
| A-13 | `call_next` 抛异常时访问日志与 `X-Request-ID` 整段不执行 | 异常路径补日志 + 统一 `TM_5002` 错误体 + 追踪头 | 新增 `test_unhandled_exception_returns_structured_500_with_trace_header` |
| A-14 | `TimeStamper` 输出 `timestamp`，契约要求 `ts` | 加 `_add_ts_alias` processor，**双字段共存**而非改键名（兼容既有消费者） | 实测输出同时含 `ts` 与 `timestamp` |
| A-15 | `ERROR_CODES` 零引用、与 `core/errors.py` 双真源 | 新增 `test_error_codes.py` 锁定 20 个类码一致、唯一、命名规范 | 参数化测试通过 |

### 2.3 稳定性与安全（QA 审查 11 项）

| ID | 问题 | 修复 | 验证 |
| --- | --- | --- | --- |
| N-01 **(Major)** | `redact_event` 排在 `format_exc_info` 之前，`exc_info=True` 的堆栈既不脱敏也不截断，明文 `sk-*` / `password=` 落日志 | 调整 processor 顺序；`exception` 加入 `_TRUNCATE_KEYS`；截断前先做密钥/PII 脱敏；新增 `_mask_kv_secrets()` 抹除自由文本里的 `password=hunter2` 类键值对 | 实测输出 264 字符，`sk-SECRET123456` 与 `hunter2` 均不在日志中 |
| N-02 **(Major)** | `auth`/`api-key`/`X-API-Key`/`pwd`/`jwt`/`key`/`session`/`client_id` 全部明文落日志；注释称「恰为 key 也抹除」但实现只判 `endswith("_key")` | 键名归一化（连字符/驼峰）+ 精确匹配短键集，避免误伤 `author`/`ping_ms` | 8 个敏感键全抹除，正常字段未误伤 |
| N-03 **(Major)** | `/readyz` DB 不可用时返回 **HTTP 200** `degraded`，编排层会把流量导向故障实例 | 降级返回 **503**；同步更新 2 个回归测试 | 实测 `503 {'status':'degraded','database':'down'}` 且追踪头仍在 |
| N-04 **(Major)** | 三个安全校验器只挂 `get_settings()`，直接 `Settings(...)` 全部绕过 | 改 `@model_validator(mode="after")`，`get_settings()` 去掉重复调用 | 3 种直接构造（production 无密码 / 禁用却外发 / canary 越界）全部抛 `ConfigError` |
| N-06 **(Major)** | `consume(-100)` 返回 True 且反向补 100 令牌，可绕过限流 | 加 `math.isfinite` + `amount > 0` 校验 | 4 种非法 amount 全 `ValueError`，`available` 保持 2.0 |
| N-08 | `migrations/env.py` 裸 `except` 吞掉 `ConfigError`，把「缺密码」掩盖成「找不到连接串」 | 收窄：`ConfigError` 重新抛出带上下文 | 静态核对 |
| N-09 | `Makefile` lint/format 仍是 `src tests`，与 CI 的 `.` 不一致 | 三行改为 `.` | `grep` 复核 |
| N-10 | `make demo`/`bench` 是 `Exit(1)` 占位，help 却写「Run the end-to-end demo」 | help 标注「[阶段 4 占位] currently exits 1」 | `make help` 可见 |
| N-11 | `serve --reload` 同时传 `workers`，uvicorn 忽略并告警 | reload 时显式传 `workers=None` | 静态核对 |
| N-12 | `_redact_value` 对 `set` 直接 `str()` 兜底，顺序不稳且不脱敏 | 排序后逐项脱敏 | 实测 `{'sk-SECRET999','plain'}` → `['plain','***']` |
| N-13 | `ping()` 裸 `except` 吞异常不记日志，无法区分认证失败/超时/DNS | `logger.warning("db.ping.failed", error_type=..., error=...)`，返回契约不变 | 实测日志含 `OperationalError` / `ConnectionTimeout` |

### 2.4 CI / 部署配置（QA 报告 8 项）

| ID | 问题 | 修复 |
| --- | --- | --- |
| D-02 | `test` job 无 PG，39 条集成测试恒 skip（假绿） | 集成测试移到唯一有真实 PG 的 `migration-idempotent` job，设为硬失败；`test` job 只跑单测 |
| D-04 | `nightly.yml` 只读 `TAGMIND_TEST_DATABASE_URL`，迁移必失败 | 补 `TAGMIND_DATABASE_URL` / `POSTGRES_PASSWORD`；YAML 冒号语法一并修正 |
| D-05 | compose 4 处 `:-tagmind` 弱口令兜底，绕过 production 密码守卫 | 改 `${TAGMIND_POSTGRES_PASSWORD:?必须设置}`，未配置时 compose 直接拒绝启动 |
| D-06 | `TAGMIND_REDIS_PASSWORD` 注入但从未被消费 | `Settings.resolved_redis_url` 把密码并入 netloc（URL 已带密码时不覆盖），特殊字符 URL 编码 |
| D-08 | worker/beat 无 healthcheck | **未修**，见 §3（需 Docker 验证） |
| D-14 | nightly 用 `continue-on-error` 让未实现的 `eval`/`bench` 恒绿 | 改为显式标注「[阶段 4 未实现]」的 echo 占位，不产出任何指标 |
| D-09 / D-10 | README 伪造 CLI 输出、未验证项写成已交付 | 「真实输出」→「如何自行验证」；补「尚未交付/未验证的项」清单（含 T03 ⑥ `GET /labels/tree` 本期未交付） |
| — | CI lint 范围漏 `scripts/` / `migrations/` | 扩到 `ruff check .` / `ruff format --check .` |

### 2.5 文档诚实性（PM 审查 10 项，纯措辞）

| ID | 修正 |
| --- | --- |
| PM-03 | 移除「幂等（连续执行两次 upgrade head 无报错）」无条件断言，改标「静态审查通过，动态未验证」 |
| PM-05 | 「本仓库的 500 条金标集…工具链已实现」→「阶段 4 规划产出，当前尚未交付」 |
| PM-06 | 「评估与验证」整节加「阶段 4 目标口径，当前未实现」；删除 PRD/架构均无定义的 **GAUC**；NDCG@K 标注 P1 降级 |
| PM-07 | 首屏加「分 5 个阶段实施，当前仅阶段 0」；三支柱表加「交付阶段」列；`evaluation/` 标〔阶段 4 待建〕；风险栏去掉「工具链已实现」 |
| PM-10 | 鉴权改为「阶段 4 · T16 计划，当前未接线」，说明 `verify_api_key` 是未接线工具函数 |
| PM-11 | `request_id` 表述收敛到「HTTP → contextvar → 访问日志 + `X-Request-ID`」，注明曝光回流属阶段 3 |
| PM-17 | `L5–L12=480` → `600`（实测 L5–L11 各 60 + L12 180；原数求和 1370 ≠ 1490） |
| PM-18 | 「覆盖 18 项校验」→「14 项（V1–V6、V8–V12、V14、V16、V18；V7/V13/V15/V17 待补）」 |
| PM-19 | PRD 4 处 `/health` → `/healthz`（架构附录 A.4 已裁决，代码本就 `/healthz`） |
| PM-21 | 架构文档第二个 `### A.4` 改为 `### A.5`，消除锚点歧义 |
| — | 覆盖率 badge 从写死的 `71%` 改为静态门槛 `70% min`；删除会过期的指标数字 |

---

## 3. 需进一步确认 / 未实施的项（记录原因与后续处理）

### 3.1 高风险或信息不足（不做推测性修改）

| 来源 | 项 | 影响 | 为何不立即修 | 后续处理 |
| --- | --- | --- | --- | --- |
| PM | PM-01 / PM-02（**Blocker**） | 阶段 0 验收口径：README「✅ 已完成」↔ QA「不予通过验收」；PRD §9.1 的 4 条 P0 出口标准至少 3 条未满足且无重排决策记录 | 属**验收状态与需求变更**，需 team-lead + PM + QA 定口径并升 PRD 版本，不是措辞修正 | 发布 PRD v1.1，逐条回写 A-01~A-10 与阶段重排；以 QA 报告 §7 未验证清单做补充验收 |
| PM | PM-08 / PM-09 | API 阶段归属三处互斥（README 全标阶段 4，架构 T08=阶段1 / T13·T15=阶段3）；`/dlq` HTTP 端点与 ADR-0009「DLQ 只用 Redis + CLI」冲突 | 涉及**对外接口契约**，需架构师裁决以哪份为准，或新增 ADR 推翻 | 架构师出裁决，统一 README / 架构 / `app.py` 注释 |
| PM | PM-12 / 13 / 14 / 15 / 16 | PRD 内部矛盾（同一 P0 双列出口、画像阶段三处不一致、PRD 未升版、金标验收口径未回写、四服务断言 vs 五服务实现） | 需 PM 拆条与重排，属需求基线变更 | 与 PM-01/02 合并为一次 PRD v1.1 修订 |
| 架构 | A-04 | 迁移只预建三个月分区，无后续预建 / DEFAULT 数据迁移 / 90 天 DROP 实现 | 需确认维护任务归属阶段、DEFAULT 存量迁移策略、保留按 `ts` 还是 `created_at` | 阶段 3 前定稿 `scheduler/tasks/maintenance.py` |
| 架构 | A-05 | `thresholds.py` 全仓零引用且与 settings 同名项冲突（8.0 vs 6.0）；`PipelineVersion` 无生产构造点，版本绑定落空 | 需确认 thresholds 是唯一真源还是默认值文档、版本串生成器落点、已落库数据兼容 | **阶段 1 打标引擎开工前定稿**（越晚代价越大） |
| 架构 | A-07 | 8 核下 worker 默认 8 prefork × 20 连接 = 160，加 api/beat 约 200 > PG 默认 100 | 需确认目标并发数、PG `max_connections`、是否改 `NullPool` | 部署前压测并定 `concurrency` / `max_connections` |
| 架构 | A-08 / N-14 | `cli.py` 从非安装包目录 `scripts/` 反向导入，wheel 安装后 CWD 非仓库根时 `ModuleNotFoundError` | 修法涉及移动模块（>3 文件），需决定 `scripts/` 是否纳入 wheel | 把 `seed_demo` 移入 `src/tagmind/seed/` |
| 架构 | A-09 | `path` / `path_prefix` 用标签 **name** 拼接，契约是 **code** 路径，按 code 做子树查询永不命中索引 | 涉及已落库数据与 `UNIQUE(version,path)`，需决策改数据还是改查询 | 阶段 3 召回接入前裁定 |
| 架构 | A-10 / A-11 / A-16 | 两套标签倒排并存且 `label_ids` 可选导致漂移；批量打标 N+1（1000 条约 2000 次往返）；`search_by_tags` 无排序/无 approved 过滤导致回放不可复现 | 需统一「哪套倒排是唯一真相」、批量接口形状、候选池上限与排序字段 | 阶段 3 开工前统一裁决，避免两套各修一半 |
| 架构 | A-06 / A-17 | 只读 session 工厂与写工厂完全相同；种子默认 100 用户 < 回放要求 1000 | 需确认是否真要拆第二 Engine、演示规模是否刻意压低 | 随阶段 3/4 定稿 |
| QA | N-05 / N-07 / N-15 | `DEMO_MODE`/`TAGMIND_DEMO_MODE` 无字段且 `verify_api_key` 零调用；`RateLimiter.cleanup()` 生产零调用导致桶无界增长；架构的 `LOG_RAW_USER` DEBUG 开关无实现 | 需确认鉴权/限流是否刻意延后到 T16、cleanup 调度策略、DEBUG 例外契约是否仍有效 | 阶段 4 鉴权中间件一并接线；或改架构文档声明契约废弃 |
| QA | 边界场景 6 组 | 空正文/超长正文/空标签集/全低置信/全 skip/Redis 与 LLM 降级 | 对应 T05/T06/T07/T11/T17 尚未实现，**无代码可测**，不算当前缺陷但不得标「已验证通过」 | 随阶段 1–3 交付时补测 |
| QA | `extra="ignore"` | 拼错的环境变量键静默丢弃（`TAGMIND_LLM_BACKND` 静默回退 deterministic） | 改 `forbid` 会与 `TAGMIND_TEST_DATABASE_URL` 等外部变量冲突，需产品决策 | 评估后决定是否加未知键告警 |

### 3.2 因环境限制未动态验证（**不标为通过**）

1. 空库 `alembic upgrade head` 幂等、`information_schema` 逐项比对（T02 ②）。
2. `exposure_log` 跨月数据落正确分区、`review_record` 触发器拒绝、`pii_mask_map` 对 PUBLIC 无权限。
3. 并发同 `idempotency_key` 提交：`_unique_column_names()` 只收单列唯一约束，而 `ux_exposure_idem` 是 4 列复合唯一索引，静态推断幂等重放可能失败。
4. `docker compose up` 五服务全部健康（**D-08 未修**：worker/beat 的 healthcheck 本机无 Docker 无法验证，写一个错误探针会让服务永久 unhealthy，反而阻断启动）。
5. `migration-idempotent` job 接上真 PG 后，集成测试可能暴露既有 DB 缺陷——这是**消除假绿的预期结果**，需跟进 CI 首跑。

---

## 4. 变更影响范围

### 4.1 有意为之的行为变化（均已同步回归测试）

| 变更 | 变更前 | 变更后 | 影响面 |
| --- | --- | --- | --- |
| `/readyz` 降级 | HTTP **200** `degraded` | HTTP **503** `degraded` | K8s/LB 就绪判定：DB 断时不再接流量。**compose 的 healthcheck 用的是 `/healthz`，不受影响** |
| 未捕获 5xx | Starlette 默认纯文本 500，无访问日志/追踪头 | 统一 `TM_5002` JSON 错误体 + 访问日志 + `X-Request-ID` | 状态码仍 500，错误体规范化，可观测性增强 |
| `TokenBucket.consume` 非法 amount | 负数/0/NaN 静默成功或反向补令牌 | 抛 `ValueError` | 现有合法调用（正数）不受影响；全仓无非法调用方 |
| `Settings` 校验 | 仅 `get_settings()` 校验 | 所有构造路径校验 | 直接 `Settings(...)` 的非法配置现在会早失败；现有测试全部通过 |
| compose 密码 | 弱口令 `tagmind` 兜底，无配置也能起 | 未设 `TAGMIND_POSTGRES_PASSWORD` 时**拒绝启动** | 快速上手需先 `cp .env.example .env` 并填密码；README 已说明 |
| `migrations/env.py` | `ConfigError` 被吞，报「找不到连接串」 | 抛 `RuntimeError` 带真实原因 | 迁移失败信息更可诊断 |
| 迁移 0003 GIN opclass | `jsonb_path_ops` | 默认 `jsonb_ops` | **已建库不会因迁移脚本改动自动生效**，需手工 `REINDEX` |

### 4.2 保持不变的部分

- 所有对外函数签名、HTTP 路径、请求/响应字段（除上述 503 与统一错误体外）。
- `bind_context` / `clear_context` 签名与语义；新增的是 `context_scope`。
- 日志**同时**输出 `ts` 与 `timestamp`，既有采集字段不破。
- Redis 无密码时 URL 与原来逐字一致（实测默认 `redis://localhost:6379/1`）。
- 12 层标签体系 1490 个标签、16 张表 DDL、C1–C10 约束、ADR 决策均未改动。

### 4.3 文件范围

- `src/`: `api/app.py`、`config/settings.py`、`core/{__init__,context,logging,security}.py`、`db/{session.py,repositories/base.py,repositories/interaction_repo.py,models/...}`、`migrations/env.py`、`cli.py`、`ingestion/{pii,rules}.py`、`scheduler/tasks/__init__.py`
- `tests/`: 新增 7 个单测文件、改 3 个集成测试
- 配置: `pyproject.toml`、`.github/workflows/{ci,nightly}.yml`、`docker-compose.yml`、`Makefile`、`.env.example`、`migrations/versions/0003*`
- 文档: `README.md`、`docs/prd.md`、`docs/architecture.md`
- **未触碰**: 业务链路实现（阶段 1–4）、任何 ADR 决策、任何已落库数据的语义

---

## 5. 验证结果

### 5.1 四条门禁（最终，同一时点）

```
$ python -m ruff check .
All checks passed!

$ python -m ruff format --check .
93 files already formatted

$ python -m mypy --no-incremental src/tagmind
Success: no issues found in 59 source files

$ python -m pytest tests/unit -q --cov=src/tagmind --cov-report=term-missing
TOTAL  2950  564  486  38  77%
Required test coverage of 70% reached. Total coverage: 77.30%
488 passed in 14.64s

$ python -m pytest tests -q
491 passed, 39 skipped in 14.64s
```

**修复前基线**：`ruff` 2 错、`format` 2 文件待重排、`mypy --no-incremental` 1 错、覆盖率 **67.21%（FAIL）**。

### 5.2 逐项行为验证（均为真实终端回显）

```
[PASS] N-01 堆栈脱敏: len=264，明文密钥/口令均不在日志中
[PASS] N-02 敏感键名: 8 个敏感键全抹除；author/ping_ms 未误伤
[PASS] N-12 set 脱敏: {'groups': ['plain', '***']}
[PASS] N-04 配置护栏: 3 种直接构造全部被 model_validator 拦下
[PASS] N-06 限流绕过: 4 种非法 amount 全拒绝，available 保持 2.0
[PASS] N-03 readyz 503: {'status':'degraded','database':'down'}，追踪头仍在
[PASS] N-13 ping 日志: 含 error_type=OperationalError / ConnectionTimeout
```

架构项：A-12 上下文退出后 `current_context() is None`；A-14 输出同时含 `ts`/`timestamp`；A-02 实测日志含 `request_id`/`trace_id` 且与响应头一致；A-01 `ping` 经线程池调度。
D-06 Redis：无密码 URL 逐字不变；独立密码 `p@ss:word` → `redis://:p%40ss%3Aword@localhost:6379/1`；URL 自带密码时不覆盖。

### 5.3 配置校验

- 三个 YAML（ci.yml / nightly.yml / docker-compose.yml）`yaml.safe_load` 全部通过；ci 6 jobs、nightly 1 job、compose 5 services。
- 文档判据 grep：`连续执行两次` / `本仓库的 500 条` / `工具链已实现` / `GAUC` / PRD `` `/health` `` 残留均为 **0**；架构 `### A.4` 标题 **1** 个；`L5–L12=600`。
- 临时补丁脚本已全部清理，`scripts/` 无 `_*.py` 残留。

### 5.4 无法验证的项

本机无 PostgreSQL / Docker，§3.2 的 5 类动态验收**未执行**。CI 的 `migration-idempotent` job 已改为带真 PG 并硬失败，作为这些项的补充验收；其首跑结果需在下一个 CI 周期跟进。

---

## 6. 结论

本轮在不改变对外接口与业务行为的前提下，把仓库从「门禁红态 + 多处宣称失真」收敛到**四条门禁全绿、覆盖率 77.30%**，并修复了 3 类真实安全风险（日志密钥泄漏、限流绕过、就绪探针误判）与 2 类确定性崩溃（非 `id` 主键仓储、事件循环阻塞）。

**遗留的最大风险不在代码，而在契约**：PRD v1.0 与实现基线已分离（PM-01/02/14），阶段 0 的验收状态在 README 与 QA 报告之间互相矛盾。建议下一步优先发布 PRD v1.1 并做一次带真 PG 的补充验收，再开启阶段 1 打标引擎（开工前须先定稿 A-05 版本串生成器）。
