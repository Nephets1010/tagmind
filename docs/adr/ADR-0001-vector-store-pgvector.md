# ADR-0001 · 向量库选型：pgvector 为主，Chroma 为开发态可插拔替代

| 项 | 值 |
| --- | --- |
| **状态** | 已接受 |
| **日期** | 2026-11-08 |
| **决策者** | 架构师 高见远（Gao） |
| **关联** | PRD §10-Q1、§8.5.1、§3.1.5 P4-REC-001；主理人决策 Q1 |
| **影响组件** | `src/tagmind/vector/base.py`、`pgvector_store.py`、`chroma_store.py`、`indexer.py`、`db/models/vector.py` |

---

## Context

PRD 需要为 10 万条内容、1024 维向量提供**语义相似召回**能力（NF-E02 要求向量库可插拔）。候选技术：

| 候选 | 版本 | 优势 | 劣势 |
| --- | --- | --- | --- |
| **pgvector** | PostgreSQL 扩展 | 与业务库同实例；可与 `content` 直接 JOIN；MVCC 天然并发写；一套备份 | 索引构建慢于 Chroma；HNSW 内存占用需精打细算 |
| **Chroma** | 单机嵌入式 | 零外部依赖；API 简单；开发体验好 | 单进程写入并发弱；跨存储双写产生一致性问题；需单独备份 |
| Milvus / Qdrant | 服务端 | 10M 级强 | 本期 Out of scope（PRD §1.3 明确排除分布式/外置向量库） |

PRD §8.5.1 的扩容表已经给出结论：10 万内容用 pgvector（+HNSW）或 Chroma；100 万内容「pgvector 强制」；1000 万切 Milvus/Qdrant。**本期规模落在 10 万档**。

关键技术考量：

1. **一致性成本**：本系统的 `content.labels`（内容语义真相）与 `content_vector.tag_vector`（召回特征）必须严格对应。若向量在 Chroma、内容在 PG，则每次打标完成都要做跨存储双写，任何一侧失败都会产生「已发布但无向量」或「有向量但未发布」的错配。PRD §8.2 的 NF-R05（状态机一致性）与 §13 的一致性校验都会被迫降级为最终一致 + 定期对账。
2. **NF-R09 要求「向量索引可从内容表重建」**：pgvector 天然满足（同库重建即可）；Chroma 需要额外导出/导入通道。
3. **基准硬件只有 16 GB 内存**（Q4）：pgvector 的 HNSW 在 10 万 × 1024 维下约占 600 MB–1.2 GB，满足 NF-P07 的「≤ 2 GB」；Chroma 单进程还要叠加自己的内存。
4. **并发写**：PRD §3.1.2 P4-TAG-004 要求批量打标不因单点故障中断。10 万条向量的批量写入会与在线查询争抢；PostgreSQL 的 MVCC 让这一点比单进程嵌入式库安全得多。

## Decision

**采用 pgvector 作为生产默认向量库；Chroma 保留为「无 PostgreSQL 时的开发态替代」，通过 `VectorStore` Protocol 抽象，运行时由 `TAGMIND_VECTOR_BACKEND` 切换。**

具体约束：

1. **物理落点**：`content_vector` 与 `content` 同库同实例（PostgreSQL 16 + pgvector 0.7+），`embedding` 列类型为 `VECTOR(1024)`，距离度量 `vector_cosine_ops`，HNSW 索引参数 `m=16, ef_construction=64`，查询期 `hnsw.ef_search=60`（可配，下限 40）。
2. **接口抽象**：`vector/base.py` 定义 `VectorStore` Protocol，统一签名为 `upsert / search / search_by_tags / delete / count / rebuild_index / health`。两个实现都返回**「越大越相似」的余弦值**（pgvector 的 `<=>` 返回距离，适配层做 `1 - distance`），调用方无需感知差异。
3. **写路径一致性**：pgvector 模式下，`content_vector` 的 upsert 与 `content` 的状态更新在**同一个数据库事务**内完成，**从根本上消除跨存储双写问题**。Chroma 模式退化为「先写 content（`status=processing`）→ 后写向量 → 置 `approved`」，并由 `scripts/consistency_check.py` 的 V1–V4 项每日对账。
4. **Chroma 不进默认安装**：`chromadb` 列为可选依赖，不写入 `[project.dependencies]`，只在需要时 `pip install 'tagmind[dev]' && pip install chromadb`。默认 `pip install -e .` 即可跑通全部 CI 与演示。
5. **容量边界**：10 万行以内 pgvector 单机可支撑；超过 100 万行时按 PRD §8.5.1 切换 Milvus/Qdrant，`VectorStore` Protocol 届时新增第三个实现即可，**上层零改动**。
6. **不引入 Milvus / Qdrant / Weaviate**：属 PRD §1.3 的 Out of scope。

## Consequences

### 正面

- **一致性成本归零**。向量与内容在同一事务里，不存在「双写不一致」这一类故障模式。这直接消掉了 §13 的一致性校验中 V1–V4 在生产模式下的一半压力（它们退化为「防御性巡检」而非「每日修复」）。
- **召回链路可以 JOIN**。投放筛选（US-11 / P4-API-005）需要「标签过滤 + 语义排序」在同一查询里完成，pgvector 一次 SQL 即可；Chroma 需要两次往返 + Python 侧重排。
- **运维面收敛**。备份、迁移、监控、权限模型只有一套（NF-R09 的 `pg_dump` 即可覆盖向量）。
- **NF-E02 天然满足**。双后端切换是接口设计问题，不是数据搬迁问题。

### 负面 / 代价

- **索引构建较慢**。10 万 × 1024 维 HNSW 构建实测约 3–8 分钟（需在阶段 3 压测中确认），必须安排在维护窗口，且**必须在阶段 0 的迁移里预留 `CREATE INDEX CONCURRENTLY` 的路径**（非空表上不能直接建 HNSW）。
- **与业务负载争抢资源**。查询期 `ef_search` 需要与在线事务共享内存；必须通过 §8.4 的延迟预算严格控制，并在压测中验证 P99。
- **pgvector 扩展是额外的运维依赖**。PostgreSQL 镜像必须自带 pgvector（用 `pgvector/pgvector:pg16` 基础镜像），自建镜像需加 `apt install postgresql-16-pgvector`。
- **Chroma 实现是「二等公民」**。它只保证接口可用与 CI 可跑，不承诺性能等価；文档需明确「Chroma 仅供开发态」。

### 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| pgvector 扩展版本与 PostgreSQL 版本不匹配 | 固定镜像 tag `pgvector/pgvector:pg16`；`migrations/versions/0001` 中 `CREATE EXTENSION IF NOT EXISTS vector` 的失败信息必须可读 |
| HNSW 内存超预算 | `ef_search` 可配 + 降级路径（降 `ef_search` → 降路 A 候选上限 → 启 Chroma）；NF-P07 的 ≤ 2 GB 在阶段 3 压测中实测 |
| 将来需要 Milvus | `VectorStore` Protocol 已预留；届时新增 `milvus_store.py` 即可，**不涉及上层改动** |

## Alternatives Considered

| 方案 | 为何被否 |
| --- | --- |
| **纯 Chroma（不用 pgvector）** | ① 跨存储双写制造一致性故障，与 NF-R05/NF-R09 的严格要求正面冲突；② 单进程写并发弱，批量打标时与在线查询争抢同一进程；③ 需单独备份与单独监控，运维面翻倍。PRD §10-Q1 的倾向性建议也指向 pgvector。 |
| **Milvus / Qdrant** | ① PRD §1.3 明确「分布式/多机部署」为 Out of scope，§8.5.1 把它们排在 1000 万档；② 10 万规模引入独立向量服务是过度工程，增加部署与学习成本；③ 与「单机 Compose 一键启动」目标冲突。 |
| **不用向量库，纯 PostgreSQL 数组 + 暴力余弦** | 10 万 × 1024 维暴力计算约 100 ms+，**直接违反 NF-P03（P99 ≤ 100 ms）**；且无法支持「标签过滤 + 语义排序」的组合查询。 |
| **pgvector 与 Chroma 同时作为生产默认（双写）** | 双写本身就是一致性风险源；PRD 的规模与部署形态都不需要两个生产向量库。「双后端」的价值在**可插拔**而非**同时在线**。 |
| **HNSW 换成 IVF-PQ** | IVF 在 10 万级召回率不稳定，需要调 `lists`/`probes` 且结果随数据分布漂移；PQ 会引入量化误差，与 §11.7 的余弦语义定义冲突（误差会传导到融合权重）。HNSW 在 10 万级是稳妥选择。 |
| **HNSW 换成 IVFFlat** | 延迟更低但召回率波动更大，且需要先有训练样本分布；本项目标签体系会持续演进，分布不稳定。 |
| **把 `content_vector` 从 `content` 拆成独立表（已定）+ 同时保留 Chroma 作为「更快的读副本」** | 引入第二份数据 → 一致性问题回流。**明确不做。** |

## 引用与追溯

- PRD §10-Q1（主理人决策 Q1 已定：pgvector，Chroma 仅开发态可插拔替代）
- PRD §8.5.1 规模扩展路径表
- PRD §8.4 NF-E02（向量库可插拔）
- PRD §8.2 NF-R09（向量索引可从内容表重建）
- `docs/architecture.md` §5.7（`content_vector` DDL）、§7（向量选型）、§14.6（`VectorStore` SPI 签名）
