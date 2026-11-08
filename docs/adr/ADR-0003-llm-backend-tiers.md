# ADR-0003 · LLM 后端三档抽象，`deterministic` 为一等公民

| 项 | 值 |
| --- | --- |
| **状态** | 已接受 |
| **日期** | 2026-11-08 |
| **决策者** | 架构师 高见远（Gao） |
| **关联** | PRD §3.1.2 P4-TAG-004、§3.1.13 P4-ENG-002、§10-Q3、§10-Q4；主理人决策 Q3、Q4 |
| **影响组件** | `src/tagmind/llm/{base,deterministic,openai_compat,registry,structured_output,audit}.py`、`config/settings.py` |

---

## Context

PRD §10-Q3 原本把后端选择描述为「本地 Ollama Qwen2.5-14B 还是 API GLM-4-Flash，还是双轨并存？」，倾向性建议是「双轨并存且默认本地」。但主理人决策 Q3 修正为**三档，且默认 `deterministic`**。

修正的现实依据：

1. **基准硬件无独显**（Q4：8 核 CPU / 16 GB 内存 / 无独显 / PCIe SSD）。PRD §10-Q3 自己就指出「本地 14B 需要 16GB+ 显存或 CPU 慢推理」。在 CPU 上跑 14B 模型，单条打标耗时以分钟计，SP-01 的「本地模式 ≥ 5 条/分钟/worker」在 8 核上勉强，而 P95 延迟完全不可控。
2. **本机大概率没有 Ollama**。即便装了，模型权重还需下载（数 GB），演示环境不满足「干净机器 15 分钟内启动」（P4-ENG-001）。
3. **CI 必须能跑**。`deterministic` 后端让「打标 → 校验 → 仲裁 → 审核 → 召回 → 评测」的完整链路在零外部依赖下可测，且**结果逐位可复现**（P4-EVA-002 要求同版本两次运行报告逐位一致）。
4. **PRIMARY 的诚实要求**。PRD §7.5 声明 4 强调「已知偏差必须主动披露」。一个只在有 GPU/密钥时才工作的系统，其「离线评测」的数值无法复现，也就无法被诚实地汇报。

PRD 已有的相关硬约束：

| 需求 | 内容 |
| --- | --- |
| P4-TAG-004 | 双后端可切换；5xx/超时 → 退避重试至上限 → 死信队列；401/403 → 不可重试直接 DLQ；上下文超长 → 二次尝试 |
| P4-ENG-002 | 断网 + 无 API Key 时用内置确定性规则/模板后端产出**结构合法的标签与质量分**，标 `backend=fallback` + `degraded=true`；此时准确率必须标 `fallback_backend` 口径 |
| P4-TAG-002 | `temperature=0` 强制写入调用参数并出现在审计记录 |
| P4-LLM-AUDIT-001 | 每次调用有审计记录；`prompt_hash` 失配即报错，不返回错版本提示词 |
| NF-E01 | 新增后端只需实现统一接口，不改上层流水线 |

## Decision

**定义 `LLMBackend` Protocol（`chat` / `embed` / `health`），实现三档后端，默认 `deterministic`。**

### 三档定义

| 档 | `provider` | `name` | 实现 | 启用条件 | 定位 |
| --- | --- | --- | --- | --- | --- |
| **1. `deterministic`** | `deterministic` | `deterministic` | `llm/deterministic.py`：规则 + 词典 + 启发式 | **默认，永远可用** | 一等公民，不是占位符 |
| **2. `llm_local`** | `ollama` | `openai_compat` | `llm/openai_compat.py`，`base_url=http://localhost:11434/v1` | `TAGMIND_LLM_BACKEND=llm_local` 且端点可达 | 可选加速通道 / 隐私优先 |
| **3. `llm_api`** | `zhipu` | `openai_compat` | `llm/openai_compat.py`，`base_url=https://open.bigmodel.cn/api/paas/v4` | `TAGMIND_LLM_BACKEND=llm_api` 且有 API Key | 可选加速通道 |

### `deterministic` 后端的能力要求（关键）

它必须是一个**功能完整的后端**，而非返回空结果的桩：

| 能力 | 实现方式 |
| --- | --- |
| 标签输出 | 从 `label_def` 的 `description` / `examples` 抽取关键词构建词典，用内容做加权匹配；输出**结构合法的标签树**（含 `label_id` / `confidence` / `source='rule'`） |
| 置信度 | 精确词命中 0.90 / 词典命中 0.80 / 启发式 0.72；低于 `min_confidence` 的不进入 `labels`，但保留在 `label_raw`（P4-TAG-005） |
| 层配额遵守 | 输出前按 C1/C2/C3/C5/C6 自行施加（与 `labeling/constraints.py` 复用同一套校验器） |
| 仲裁 | 对语义候选冲突，用「标签语义关键词重合度 + 层距离 + IDF 相似度」的规则化打分代替 LLM，输出 `confidence` 与模板化 `reason`（≥ 20 字符，满足 P4-RAG-003） |
| 质量评分 | 由可解释信号合成：长度、标点密度、重复度、广告词命中、敏感词命中 → 映射到 0–10 分 + 模板化理由（满足 P4-QA-002 的「reason 非空」） |
| **确定性** | **完全无随机数**：同输入 → 同输出，排序稳定，浮点运算顺序固定。这是 CI 跑评测（P4-EVA-002 逐位一致）与回归对比（P4-TAG-001）的前提 |
| 诚实标记 | `provider='deterministic'`。`degraded` 取决于**是否由降级链进入**，而非后端身份 |

### 降级链（单向，无回环）

```
llm_api  →  llm_local  →  deterministic
```

每一跳都记录 `degraded=true` + `fallback_reason`（如 `api_unavailable` / `local_unavailable` / `all_backends_failed`），并在 `llm_call_audit.degraded` 落库。

### 错误分类

```python
class LLMErrorKind(StrEnum):
    RETRYABLE = "retryable"   # 5xx / 超时 / 连接错误 → 指数退避重试
    FATAL     = "fatal"       # 401 / 403 / 参数错 / 上下文超长 → 直接 DLQ
    PARSE     = "parse"       # 响应可解析性问题 → 降级解析或走确定性路径
```

- 401/403 属于 `FATAL`：**不可重试**，直接 DLQ 并告警「密钥失效」（PRD §5.2 明确）。
- 5xx/超时属于 `RETRYABLE`：指数退避，默认 3 次。
- 上下文超长：先尝试缩减分块（只取首块 + 摘要）二次调用，仍失败则 `FATAL`。

### 配置约束

- 新增 `TAGMIND_LLM_BACKEND`（默认 `deterministic`）。
- 已有的 `TAGMIND_LLM_ENABLED` 语义为「是否启用真实 LLM」。当 `LLM_ENABLED=false` 且 `LLM_BACKEND != deterministic` 时**启动报错**（避免误配导致外发请求，呼应 NF-S03 的数据出境管控）。

## Consequences

### 正面

- **演示与 CI 零依赖可跑**。这是 Q3 的核心诉求，也是 P4-ENG-002 的硬要求。
- **结果可复现**，评测数字可被第三方逐位复现（§7.5 声明 3「任何数字必须能在 10 分钟内回答」）。
- **降级链是工程事实而非文档承诺**：三档共用一个 Protocol，上层流水线一行不改。
- **成本可控**：默认档 token 成本为 0，`llm_call_audit` 仍完整记录（`tokens_in/out = 0`，`provider='deterministic'`），口径清晰。
- **隐私默认安全**：默认不出网，符合 NF-S03「默认后端为本地模型」的精神（`deterministic` 比本地模型更安全）。

### 负面 / 代价

- **`deterministic` 的准确率必然低于真实 LLM**。这是本质约束，不是实现缺陷。因此 **P4-ENG-002 与 MQ-15 的口径必须严格遵守**：`degraded=true` 的结果标 `degraded_backend`，**不计入正式准确率结论**。
- **需要额外写一套规则引擎**（`llm/deterministic.py` 约 420 行）。这是一笔实打实的工程投入，且它不是「简化版 LLM」，而是一套独立的、必须维护的分类逻辑。
- **词表维护成本**：词典从 `label_def` 自动导出（避免人工维护两份），但标签描述写得不好会直接影响 `deterministic` 的表现 —— 这实际上**倒逼标签描述质量**（与 ADR-0002 的「L1–L3 描述需高质量」一致）。
- **可能掩盖配置错误**：如果用户以为在用 `llm_api` 却因 Key 失效静默降级到 `deterministic`，结果会静默变差。对策：**降级必须显式**（响应与日志带 `degraded=true` + `fallback_reason`），且 401/403 属于 `FATAL` **不降级**，直接 DLQ 告警。

### 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 误以为 `deterministic` 的准确率是大模型能力 | 评测报告对 `provider='deterministic'` **单独成节**并标注「规则后端结果，非大模型能力评估」；`degraded=true` 的额外标 `degraded_backend=true` 且排除在正式结论外（见 §16.2 A-08） |
| `deterministic` 的规则与 `constraints.py` 的校验逻辑漂移 | 两者**复用同一套校验器**（`labeling/constraints.py`），`deterministic` 只负责「产出候选」，不自己实现约束 |
| 降级链掩盖故障 | 401/403 不可重试直接 DLQ；降级事件记 `warning` 级日志 + Prometheus 指标 `tagmind_llm_degraded_total`；启动日志打印生效后端 |
| `llm_local` 在无 GPU 机器上极慢 | 启动时探测端点延迟；超过阈值（如 5 s）在启动日志中警告「本机无 GPU，`llm_local` 可能不可用」；用户可显式切回 `deterministic` |

## Alternatives Considered

| 方案 | 为何被否 |
| --- | --- |
| **默认 `llm_local`（PRD §10-Q3 原倾向）** | ① 基准硬件无独显，CPU 推理 14B 模型的延迟与吞吐无法满足 SP-01/SP-02；② 需要模型权重下载（数 GB），与「干净机器 15 分钟启动」冲突；③ CI 环境通常无 Ollama，测试全部依赖外部服务 → 测试不稳定且不可复现。 |
| **只有两档（`local` / `api`），无 `deterministic`** | ① 直接违反 P4-ENG-002（断网 + 无 Key 必须能产出结构合法的标签与质量分）；② CI 无法跑评测；③ 违反主理人决策 Q3。 |
| **`deterministic` 只做「空返回 + 降级标记」的桩** | ① 违反 Q3「一等公民，不是占位符」；② 演示链路会因无内容而中断（打标是主流程的必经步骤）；③ 无法验证约束校验器（C1–C10）的正确性。 |
| **单一 OpenAI 兼容后端，靠 base_url 区分 local/api（不显式分档）** | 实际上 `openai_compat.py` 已经是共用实现，但**配置层必须显式分档**，否则 `llm_api` 的 Key 缺失会表现为「连接失败」而非「配置缺失」，错误信息误导。 |
| **允许降级链回环（api 挂了退回 local，local 好了再回 api）** | 破坏确定性：同一批内容在不同时间被打上不同后端的标签，版本口径混乱。降级链**单向**保证「一批内容内后端一致」，`content.model_version` 才有意义。 |
| **把 `deterministic` 做成 `llm` 目录外的独立脚本** | 会形成第二套打标逻辑，与 `labeling/engine.py` 的流水线分叉，违反 NF-E04（流水线阶段可插拔但只应有一份编排）。 |
| **用检索/分类器（如 fastText）替代 `deterministic`** | 引入新的训练数据依赖与不可复现性；且 PRD Out of scope 排除了「端到端训练」。规则后端更符合「零依赖、零密钥、可复现」的定位。 |

## 引用与追溯

- 主理人决策 Q3（三档后端，默认 `deterministic`）、Q4（基准硬件 8 核 / 16 GB / 无独显）
- PRD §10-Q3（原始问题与倾向性建议）、§10-Q4（基准硬件）
- PRD §3.1.2 P4-TAG-004（双后端切换、错误分类、DLQ）、§3.1.13 P4-ENG-002（降级路径）
- PRD §3.1.9 P4-LLM-AUDIT-001（审计与 prompt_hash）、§8.5 NF-E01（后端可插拔）
- PRD §7.1 MQ-15（降级后端准确率口径）、§7.5 声明 3/4（诚实口径）
- `docs/architecture.md` §14.7（`LLMBackend` SPI 签名与三实现说明）、§16.2 A-08（口径歧义）
