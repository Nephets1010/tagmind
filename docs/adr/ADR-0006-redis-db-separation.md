# ADR-0006 · Redis 三库分离与离线回放不读缓存

| 项 | 值 |
| --- | --- |
| **状态** | 已接受 |
| **日期** | 2026-11-08 |
| **决策者** | 架构师 高见远（Gao） |
| **关联** | PRD §10-Q8、§3.1.5 P4-REC-004、§3.1.12 P4-EVA-003；主理人决策 Q8 |
| **影响组件** | `config/settings.py`、`core/context.py`、`recommendation/recall/hot_latest.py`、`evaluation/replay.py`、`scheduler/celery_app.py` |

---

## Context

PRD §10-Q8 提出的问题：

> Redis 用于 Celery broker/result、缓存热门榜、可能用作推荐结果缓存。多用途下缓存 key 与队列 key 冲突风险、以及「热门榜缓存 TTL ≤ 60 s」与「离线回放需要历史一致热门榜」之间的矛盾需要澄清。

具体风险：

**风险 1 · key 空间冲突**

Celery 的 broker 使用 `kombu` 的队列 key（默认 `celery` / 队列名）。如果热门榜缓存也用同 namespace，一次 `FLUSHDB` 就会清掉队列；反之，运维的「清缓存」操作会丢任务。

**风险 2 · `FLUSHDB` 的破坏性**

单机演示与 CI 中，「重置环境」最常见的手段是 `redis-cli FLUSHALL`。若三类用途共用一个 DB，一次误操作会同时丢掉：未消费的 Celery 任务、结果缓存、以及全部应用缓存。**在 P4-OPS-003 的 1000 条批量打标场景中，这等于丢任务。**

**风险 3 · 缓存与回放的口径矛盾（最关键）**

P4-REC-004 要求热门榜缓存 TTL ≤ 60 s；P4-EVA-003 要求离线回放能复现历史时期的推荐行为以对比基线。

如果回放时读当前缓存的热门榜，那么：
- 回放时间窗是 30 天前，但热门榜是「此刻」的热门 → 基线失真；
- 同一份日志跑两次回放，因缓存 TTL 边界不同可能得到不同结果 → **不可复现**；
- 结论「个性化 vs 热门基线的 CTR 提升」将建立在错误的基线上 → **结论无效**。

**风险 4 · 验收断言的自我矛盾**

P4-REC-004 断言「热门榜按近 7 天曝光/行为计算并有缓存，缓存 TTL ≤ 60 s」。P4-EVA-003 断言「回放使用与线上一致的召回与排序代码路径（同一函数，参数化基线）」。两者要同时成立，必须存在**同一条代码路径但能切换数据来源**的机制。

## Decision

### 决策 1 · Redis 三库分离

| DB | 用途 | 关键 Key | TTL | 清库影响 |
| --- | --- | --- | --- | --- |
| **`/0`** | 应用缓存 | `profile:{user_id}`、`hot:{window}`、`ckpt:{task}:{key}`、`rate:{ip}`、`config:live` | profile 60 s / hot ≤ 60 s / ckpt 24 h | 只丢缓存，**不影响任务** |
| **`/1`** | Celery broker + result backend | Celery 默认队列与结果 | 按 Celery 配置 | 丢未消费任务（运维需知情） |
| **`/2`** | 结果缓存 + 特征快照 | `rec:{user_id}:{ver}`、`csr_snapshot:{ts}`、`tagdef:{ver}`、`metrics:cache` | 结果 30 s / 快照 30 s | 只丢缓存 |

**配置派生**：`.env.example` 只有一个 `TAGMIND_REDIS_URL`（默认 `redis://localhost:6379/0`）。配置类派生出三个 URL：

```
redis_url         = TAGMIND_REDIS_URL（保持原样，= /0）
redis_broker_url  = TAGMIND_REDIS_URL 的 db 改为 1
redis_cache_url   = TAGMIND_REDIS_URL 的 db 改为 2
```

**不新增环境变量**（保持与 `.env.example` 逐项对齐），派生逻辑在 `config/settings.py` 的 `model_post_init` 中完成。若用户显式设置了 `TAGMIND_REDIS_BROKER_URL` / `TAGMIND_REDIS_CACHE_URL` 则优先使用（这两个键在 `.env.example` 中标注为可选新增）。

**硬性纪律**：
- **禁止 `FLUSHALL`**。CI 与演示脚本只允许 `FLUSHDB` 指定 DB（`redis-cli -n 2 FLUSHDB`）。
- `celery_app.py` 的 `broker_url` 显式指向 `/1`，**不依赖 Celery 默认值**。
- 每个模块的 Redis 客户端由 `core/context.py` 提供的工厂创建，**必须显式传 db 编号**，不允许使用 `Redis.from_url(env_url)` 的裸调用。

### 决策 2 · 离线回放强制不读缓存

`recommendation/service.py` 与 `recommendation/recall/hot_latest.py` 增加一个**上下文开关**：

```python
@dataclass(frozen=True)
class RecContext:
    request_id: str
    replay_ts: datetime | None = None  # 非 None ⇒ 回放模式
    cache_bypass: bool = False
    debug_level: int = 0
```

规则：

| 场景 | `replay_ts` | 缓存行为 | 热门榜来源 |
| --- | --- | --- | --- |
| 在线请求 | `None` | 读 Redis `/0` 与 `/2` | 近 7 天窗口实时聚合 + 缓存（TTL ≤ 60 s） |
| **离线回放** | 回放时间点 | **完全绕过缓存**（`cache_bypass=True`） | **按回放时间窗从 `exposure_log` 重算** |

**「重算」的具体口径**（`hot_latest.recall(replay_ts=...)`）：

- 热门榜 = `exposure_log` 中 `ts ∈ [replay_ts - 7d, replay_ts]` 且 `action ∈ {view, click}` 的内容按加权热度排序（`view` 权重 1，`click` 权重 3，`dwell` 权重 2）。
- 最新池 = `content` 中 `published_at ≤ replay_ts` 且 `status='approved'` 的最新 N 条。
- **完全从 `exposure_log` / `content` 表查询**，不读任何 Redis。

这样保证了：同一份日志 + 同一时间窗 → **两次回放结果逐位一致**。

### 决策 3 · 同一代码路径，参数化数据源

为满足 P4-EVA-003「回放使用与线上一致的召回与排序代码路径（同一函数，参数化基线）」：

- `recommendation/service.py` 的 `recommend()` 是**唯一**的召回+排序实现。
- 回放通过 `RecContext(replay_ts=..., cache_bypass=True)` 调用**同一个函数**。
- 三条基线（业务基线 / 纯标签 / 纯向量 / 全量融合）通过**参数**（`RecContext.recall_plan`）切换召回组合，而非另写一套代码。

```python
@dataclass(frozen=True)
class RecContext:
    ...
    recall_plan: tuple[str, ...] = ("tag_preference", "vector_similarity", "collaborative")
    # 基线 A（业务）：("hot", "latest")
    # 基线 B（消融-纯标签）：("tag_preference",)
    # 基线 C（消融-纯向量）：("vector_similarity",)
    # 基线 D（全量融合）：("tag_preference", "vector_similarity", "collaborative")
```

### 决策 4 · 缓存失效与新鲜度

| 数据 | 缓存 TTL | 失效方式 |
| --- | --- | --- |
| 用户画像 `/0:profile:{uid}` | 60 s | TTL + 曝光回流时主动 DEL |
| 热门榜 `/0:hot:{window}` | ≤ 60 s（P4-REC-004 硬性） | TTL + 调度每 5 分钟刷新 |
| 推荐结果 `/2:rec:{uid}:{ver}` | 30 s | TTL |
| CSR 特征快照 `/2:csr_snapshot:{ts}` | 30 s | TTL + 写路径 pub/sub 通知失效 |
| 生效配置 `/2:config:live` | 5 s | TTL（热生效的灰度/权重开关） |

**写路径的失效通知**：`vector/indexer.py` 在写入新向量后，通过 Redis pub/sub 广播 `csr_invalidated`，各 worker 收到后立即重建 CSR 索引（避免等待 30 s TTL 造成「新内容召回不到」）。

## Consequences

### 正面

- **任务不丢**。缓存清空不影响 Celery 队列。
- **回放可复现**。Q8 的核心诉求达成：同一日志 + 同一窗口 → 逐位一致的结果。
- **回放结论有效**。热门基线是「回放时点的真实热门」，而非「此刻的热门」→ BE-01 的 CTR 提升对比有意义。
- **同代码路径**。满足 P4-EVA-003 的「同一函数，参数化基线」，且从设计上杜绝了「回放实现与线上实现漂移」这一经典 bug。
- **运维可预期**。`FLUSHDB -n 2` 安全；三个 DB 的用途在 README 与 runbook 中写清。

### 负面 / 代价

- **多两个 Redis DB 的配置复杂度**。运维需要知道哪个 DB 能清、哪个不能。
- **回放变慢**。绕过缓存意味着每次回放请求都要重新聚合热门榜。缓解：回放是批量的，可把同一时间窗的热门榜**在一次回放运行内缓存到进程内存**（run-scoped cache，不是 Redis），既保证同一次运行内一致，又不跨运行泄漏。
- **CSR 快照的失效机制引入 pub/sub**，增加了一点复杂度。
- **`RecContext` 多了一个字段就要改所有调用点**，但这正是「显式优于隐式」的价值。

### 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 有人用 `FLUSHALL` | README + runbook 显式警告；`scripts/` 里的所有 Redis 操作只允许 `FLUSHDB` 指定 DB；可加一条 CI 静态检查禁止 `flushall` 字面量 |
| 回放时误读缓存 | `RecContext.cache_bypass` 在 `evaluation/replay.py` 中**强制置 True**，且 `recommend()` 内部对 `replay_ts is not None` 二次校验（双保险） |
| 回放期间热门榜算错（跨时区） | 全部时间戳 UTC 存储；回放窗口以 `replay_ts` 为右端点，左端点 = `replay_ts - 7d`；数据库连接强制 `timezone=UTC` |
| 三库中被误用 | 每个客户端由工厂创建并绑定用途名；`core/context.py` 中记录 `purpose` 便于排查 |

## Alternatives Considered

| 方案 | 为何被否 |
| --- | --- |
| **单库共用（现状 `.env.example` 的写法）** | ① key 空间冲突风险；② `FLUSHALL` 误清队列；③ 无法区分「清缓存」与「清任务」两类运维意图。 |
| **两库（应用缓存 + broker）** | 仍需把「结果缓存 + 特征快照」与应用缓存分开：特征快照体积大（CSR 矩阵 ≈ 8 MB × 版本），且其失效时机（写路径触发）与应用缓存（TTL 驱动）完全不同，混在一起会导致频繁重建。 |
| **四库（再加一个独立的热门榜库）** | 过度拆分。热门榜本质是应用缓存的一类，放 `/0` 即可；分库过多的运维成本大于收益。 |
| **回放时读缓存，但把快照时间固定**（即把当时的热门榜缓存下来） | ① 需要在**打标/曝光时**就为每个时间点缓存热门榜，存储成本随时间线性增长；② 无法覆盖「任意时间窗回放」的场景（只支持当初恰好缓存过的点）；③ 回放的历史窗口常常是事后才决定的。**从 `exposure_log` 重算是唯一可靠的做法。** |
| **回放完全独立一套实现（不复用线上代码）** | 直接违反 P4-EVA-003 的「同一函数，参数化基线」验收断言；且必然出现「回放结果与线上不一致却查不出原因」的问题。 |
| **用 PostgreSQL 的物化视图替代 Redis 缓存热门榜** | 10 万级下 PostgreSQL 聚合够快，但会与写事务争锁；且 P4-REC-004 明确要求「有缓存，TTL ≤ 60 s」。 |
| **引入独立的缓存服务（Memcached）** | 引入新组件与新依赖（部署、监控、备份），单机 Compose 下收益为负。 |

## 引用与追溯

- 主理人决策 Q8（三库分离 + 离线回放不读缓存）
- PRD §10-Q8（原始问题与倾向性建议）
- PRD §3.1.5 P4-REC-004（热门榜缓存 TTL ≤ 60 s）、§3.1.12 P4-EVA-003（同代码路径、参数化基线）
- PRD §3.1.10 P4-OPS-003（批量打标不丢任务）、§8.2 NF-R02（至少一次投递）
- PRD §7.3 BE-01（仿真 CTR 提升 —— 基线必须正确）、§7.5 声明 4（已知偏差披露）
- `docs/architecture.md` §9.5（Redis DB 分离表）、§8.1（召回总览）、§9.1（数据流图）
