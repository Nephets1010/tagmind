# ADR-0004 · 不引入 LlamaIndex，自写仲裁编排

| 项 | 值 |
| --- | --- |
| **状态** | 已接受 |
| **日期** | 2026-11-08 |
| **决策者** | 架构师 高见远（Gao） |
| **关联** | PRD §10-Q6、§3.1.3 P4-RAG-001~004、§4.7；主理人决策 Q6 |
| **影响组件** | `src/tagmind/labeling/conflict/{detector,retriever,arbitrator}.py`、`vector/indexer.py` |

---

## Context

业务蓝本原文提到「编排 LlamaIndex」，PRD §10-Q6 把这个问题提出并给出倾向性建议：**不引入 LlamaIndex**。主理人决策 Q6 已拍板，不需再讨论替代方案；本文档记录**为什么**以及**自写方案的具体边界**，以免后续工程师「顺手引入」。

PRD 对 RAG 环节的描述其实很薄：

> §5.3：候选标签集合 + 内容向量 → 冲突检测（① 父子越级 ② 同父互斥 ③ 显式互斥组 ④ 跨层语义候选）→ 确定性冲突走规则修复 → 语义候选走「内容+候选标签联合向量化 → top-k 检索（标签定义 + gold 样本 + 历史人工结论，k=5）→ 证据有效性判定（max_score ≥ 阈值）→ 构造 CoT 仲裁提示（证据 + 候选 + 冲突描述）→ LLM 仲裁（temperature=0）→ 解析 → confidence ≥ 0.75？」→ 写 `label_conflict_log`」

同时 §5.3 末尾给出了**可解释性契约**：

> `label_conflict_log` 必须同时包含「候选集」「检索证据」「结论」「自然语言理由」「仲裁模型与版本」「置信度」「复核状态」七要素，缺一不可。

以及 §4.7 末尾一条硬要求：

> 仲裁检索是「标签定义 + 金标样本 → top-k → CoT 仲裁」，逻辑很薄。

**LlamaIndex 在这个场景能提供什么？**

| LlamaIndex 能力 | 本项目是否需要 | 理由 |
| --- | --- | --- |
| 文档切分（`SentenceSplitter`） | 不需要 | 分块由 `ingestion/chunker.py` 按 P4-PRE-003 的规则完成（token 估算 ≤ 2048、字符覆盖率 ≥ 99%），且必须产出 `content.chunks` 的偏移量 |
| 文档加载器（`SimpleDirectoryReader` 等） | 不需要 | 数据源是 PostgreSQL 里的 `content` 表 + `annotation` 表，不是文件 |
| 向量库抽象层（`VectorStoreIndex`） | 部分需要 | 已有 `VectorStore` Protocol（ADR-0001），pgvector 是首选；LlamaIndex 的抽象反而多一层 |
| 检索器（`VectorIndexRetriever`） | 部分需要 | 只需 top-k ANN，`vector/indexer.py` 三行 SQL 即可 |
| 索引构建（`Index`） | 不需要 | 标签定义中心向量 + 内容向量的索引都由 `vector/indexer.py` 显式管理，需要精确控制刷新时机 |
| 多步 Agent / ReAct | 不需要 | 仲裁是**单步**的：检索 → 提示 → 调用 → 解析。PRD §5.3 的流程图没有循环 |
| 查询改写 / HyDE / 重排器 | 不需要 | PRD 未要求；且查询改写会引入不确定性，破坏「同版本逐位一致」 |
| 引用溯源（`CitationQueryEngine`） | 需要但更简单 | 需求的溯源粒度固定为 7 个字段，写死比框架化更清楚 |

**规模对比**：本项目的 RAG 逻辑约 **880 行**（`detector.py` 320 + `retriever.py` 280 + `arbitrator.py` 300）。用 LlamaIndex 实现同样功能，大约需要 300–500 行胶水代码 + 8–12 个传递依赖（llama-index-core / llama-index-vector-stores-* / llama-index-embeddings-* / llama-index-llms-openai 等）。

## Decision

**不引入 LlamaIndex。用「向量检索 + 固定提示模板 + 结构化解析」自写仲裁编排，拆成三个职责单一的模块。**

### 三个模块的职责边界

| 模块 | 职责 | 不做什么 | 行数 |
| --- | --- | --- | --- |
| `labeling/conflict/detector.py` | **纯确定性**：从 `labels` 检出 4 类冲突，产出 `ConflictCandidate` | 不做检索、不调 LLM | 320 |
| `labeling/conflict/retriever.py` | **纯检索**：按候选标签 + 内容向量取 top-k 证据，产出 `Evidence`（含 `sample_id`/`score`/`matched_label`/`source`） | 不做判定、不调 LLM | 280 |
| `labeling/conflict/arbitrator.py` | **判定**：固定 CoT 提示模板 + `LLMBackend.chat` + 结构化解析 + 置信度闸门 | 不做检索、不做规则修复 | 300 |

### 关键实现约束

1. **检索证据的可解释性**：`Evidence` 必须携带 `source ∈ {gold, history, label_def}`（P4-RAG-001 的硬要求）与归一化到 `[0,1]` 的 `score`。`retriever` 同时返回 `retrieved_evidence_ids` 与 `retrieved_scores` 两个数组，供 `arbitrator` 原样写入 `label_conflict_log`。
2. **CoT 提示模板固定化**：提示模板是 `config/prompt_templates.py` 中的常量（带 `PROMPT_VERSION`），结构为「冲突描述 → 候选标签 → 检索证据 → 判定步骤 → 输出 JSON Schema」。**不允许运行时动态拼接模板**（保证 prompt_hash 可复现）。
3. **结构化解析**：`labeling/parser.py` 的容错解析器复用（容忍 ```` ```json ```` 包裹与前后噪声），保证 MQ-09 的 98% 一次解析成功率。
4. **置信度闸门**：`confidence < 0.75` → `label_conflict_log.review_status='pending'` + 创建 `review_task` + `content.status='needs_review'`。**不降级放行**。
5. **确定性规则优先**：detector 的前三类冲突（parent / mutual / exclusive）**根本不进入检索与仲裁**，直接走规则修复并记 `detected_by='rule'`。只有第四类（`semantic_candidate`）才消耗 LLM 成本。这是 §4.5「把 80% 的约束用规则解决」原则的直接体现。

### 可插拔性的保留

虽然不引入框架，但仍保留**将来引入**的可能：三个模块的签名（`detect()` / `retrieve()` / `arbitrate()`）就是天然的接口边界。若将来标签图谱推理（PRD §3.3 P4-RAG-007，P2 触发条件）落地，可在 `arbitrator.py` 内部替换判定实现，不影响 `detector.py` 与 `retriever.py`。

## Consequences

### 正面

- **依赖面小**：不引入 8–12 个传递依赖，`pip install -e .` 仍是 16 个直接依赖。降低了 NF-S08（依赖漏洞扫描）与开源合规（NF-S10）的审计成本。
- **完全可控**：仲裁的每一步都可见、可测、可调。`label_conflict_log` 的 7 要素是**代码保证**（每次写库都填满 7 个字段），而不是「框架产出的引用」需要事后解析。
- **可测试性**：三个模块可独立单测。`detector.py` 是纯函数（无 IO），覆盖率可以做到 95%+，直接支撑 §14.8 的「`labeling/constraints.py` 目标 ≥ 90%」同类要求。
- **确定性可保**：不引入查询改写/重排等不确定性组件，符合 P4-EVA-002 的逐位一致要求。
- **与 `deterministic` 后端对等**：ADR-0003 的 `deterministic` 需要实现「规则化仲裁」，框架化编排反而难以降级到规则实现。

### 负面 / 代价

- **自己承担了「检索抽象」的维护成本**：`VectorStore` Protocol、ANN 检索、索引刷新都要自己写。但这部分在 ADR-0001 中已经存在，不是新增成本。
- **若将来需要多步推理，必须重新设计**：比如「标签图谱多跳推理」这类需求，自写方案的扩展性不如框架。但该需求属 P2（触发条件：标签数 > 300 且运营需要自助调整），本期不启动。
- **失去生态的现成能力**：如 rerank 模型集成、query decomposition。PRD 均未要求。

### 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 工程师「顺手」引入 LlamaIndex 简化检索 | ① `docs/architecture.md` §3 的依赖红线明确「禁止跨层直连，检索只经 `VectorStore` Protocol」；② ADR 记录在 `docs/adr/` 供 code review 引用；③ 依赖新增需走 ADR 流程 |
| 自写检索质量不足 | top-k 检索的质量由 `vector/indexer.py` 的索引质量决定，与框架无关；且 P4-RAG-001 的验收断言（返回 k 条、score 归一化、降序）可在单测中固定 |
| 未来多步推理需求出现 | 三个模块的签名边界即为改造点；届时新增 ADR 而非推翻现有结构 |

## Alternatives Considered

| 方案 | 为何被否 |
| --- | --- |
| **引入 LlamaIndex** | ① 8–12 个传递依赖，与「依赖面最小」的开源作品集定位不符；② 抽象层与本项目已有的 `VectorStore` Protocol **功能重叠**，会出现两层向量抽象；③ 框架的默认行为（query 改写、rerank）会引入不确定性，破坏逐位可复现；④ 索引生命周期由框架管理，与 PRD 需要的「标签体系发布后触发重建 + 灰度期新旧并存」不匹配，需要大量定制。 |
| **引入 Haystack / DSPy 等其他编排框架** | 同类问题，且生态热度与维护活跃度不如 LlamaIndex，无额外收益。 |
| **引入 `langchain`** | 依赖更多（数百个传递依赖）、抽象更重（LCEL 表达式树），对 880 行的逻辑属于过度设计。 |
| **把检索与仲裁合并到一个函数** | 违反单一职责；detector 与 retriever 的**纯函数特性**会丢失，无法高覆盖单测；且 PRD §5.3 的流程图本身就把它们画成独立步骤。 |
| **自写编排 + 引入一个轻量检索库（如 `faiss`）** | `faiss` 需独立进程/索引文件，与 ADR-0001 的 pgvector 决策冲突（两套向量存储）。10 万规模 pgvector 足够。 |
| **完全规则化仲裁，不用 LLM（连 `semantic_candidate` 也走规则）** | PRD P4-RAG-003 明确要求「top-k 检索 + CoT 判定由 LLM 仲裁并给出理由」，且 `reason` 需 ≥ 20 字符的自然语言理由。规则化无法满足这一验收断言。**注意：`deterministic` 后端的规则化仲裁仍保留**，因为它是该后端的实现方式，而非绕过 LLM 契约。 |

## 引用与追溯

- 主理人决策 Q6（不引入 LlamaIndex）
- PRD §10-Q6（原始问题与倾向性建议）
- PRD §3.1.3 P4-RAG-001（检索证据结构）、P4-RAG-002（四类冲突）、P4-RAG-003（CoT 仲裁与 7 要素）、P4-RAG-004（全量可追溯）
- PRD §4.5 结尾（约束分层：80% 规则、20% LLM）、§4.7（标签与画像同构）、§5.3（仲裁流程与可解释性契约）
- PRD §7.1 MQ-09（JSON 一次解析成功率）、MQ-10/MQ-11（仲裁准确率与转人工率）
- `docs/architecture.md` §10.3（C1–C10 校验器）、§14.7（`LLMBackend` SPI）
