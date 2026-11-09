# ADR-0010 · 口径诚实与可回滚：指标绑定版本 + 五类回滚

| 项 | 值 |
| --- | --- |
| **状态** | 已接受 |
| **日期** | 2026-11-08 |
| **决策者** | 架构师 高见远（Gao） |
| **关联** | PRD §1.2、§7（含 §7.5 诚信条款）、§8.3 NF-O08、§8.4 NF-S10、§9.6；主理人决策 Q11/Q12/Q14/Q18/Q20 |
| **影响组件** | `src/tagmind/evaluation/{gold,agreement,metrics,replay,drift_check,report}.py`、`domain/versioning.py`、`config/thresholds.py`、`scheduler/tasks/maintenance.py`、`docs/`、`README.md` |

---

## Context

PRD §7 有一节被明确标注为「**强制章节**」：

> **§7.5 诚实口径声明（诚信条款）**
> - 声明 1：三类证据不可混淆（`offline_evaluation` / `offline_simulation` / `online_gray` / `online_real`）
> - 声明 2：每个对外数字必须带来源标签（`evidence_type` 缺失即视为缺陷）
> - 声明 3：任何数字必须能在 10 分钟内回答「分子是什么？分母是什么？数据从哪来？口径有什么已知偏差？」—— 回答不了的数字不得展示
> - 声明 4：已知偏差必须主动披露（6 项，包括「金标集与提示词 few-shot 同源」「离线回放无法复现反馈循环」「留存无法验证」）
> - 声明 5：失败也是交付物（不得通过调整口径使其达标）
> - 声明 6：版本绑定（任何指标必须绑定 `label_def.version` / `model_version` / `prompt_version` / `pipeline_version`）

PRD §1.2 还写了一条：

> 任何指标若同时服务两个目标（G1 成本 / G2 效果 / G3 可控），须在 §7 拆成两条独立口径，不得合并计算。

同时 PRD 又是一个**开源作品集**（目标仓库 `Nephets1010/tagmind` 公开），主理人决策 Q18 明确 LICENSE = Apache-2.0 并要求 README 声明「演示数据全部为合成或公开许可语料，不含任何平台真实 UGC」。

这些要求共同指向一个工程事实：

> **诚实不能靠写文档保证，只能靠代码结构保证。**

如果 `evidence_type` 是评审时手工填的字段，它迟早会漏；如果回滚靠人记住「上次用的版本号是 v2」，它迟早会滚不回去。

同时 PRD §8.3 NF-O08 要求运行手册覆盖「模型版本回滚」的处置步骤，§8.2 NF-R09 要求备份恢复演练 —— 这些都指向「回滚必须是一等能力，而不是应急时的手工操作」。

## Decision

### 决策 1 · `evidence_type` 由代码注入，不允许手工填写

`domain/metrics.py` 定义：

```python
class EvidenceType(StrEnum):
    OFFLINE_EVALUATION = "offline_evaluation"
    OFFLINE_SIMULATION = "offline_simulation"
    LOAD_TEST = "load_test"
    ENGINEERING_STAT = "engineering_stat"
    ONLINE_GRAY = "online_gray"
    ONLINE_REAL = "online_real"  # 需 PRD 变更方可使用


@dataclass(frozen=True)
class MetricValue:
    name: str
    value: float
    evidence_type: EvidenceType  # 构造时必填，无默认值
    sample_size: int
    as_of: date
    label_def_version: str | None = None
    model_version: str | None = None
    prompt_version: str | None = None
    pipeline_version: str | None = None
    notes: str = ""
```

**关键设计**：

1. `evidence_type` **无默认值** —— 忘记填会在构造时 `TypeError`（开发期暴露），而不是在报告里静默缺失。
2. **PRD §7.5 声明 2 由渲染器强制**：所有报告经 `evaluation/report.py` 渲染，渲染器遍历每个 `MetricValue`，若 `evidence_type` 缺失则**抛异常拒绝出报告**。README、阶段报告、演示 PPT 都走同一个渲染器。
3. **`ONLINE_REAL` 的使用需要显式开关**：`report.py` 渲染 `ONLINE_REAL` 时额外要求 `experiment.evidence_type` 在 DB 中为 `online_gray`（PRD §6.9 规定 `experiment.evidence_type` 只能是 `offline_simulation` 或 `online_gray`）。**本项目无真实流量，该值实际不可达** —— 这是结构性保障，不是纪律要求。
4. **`MetricValue` 是 frozen dataclass**，不可变，防止渲染后被改写。

### 决策 2 · 版本绑定：`pipeline_version` 六元组

PRD §7.5 声明 6 要求指标绑定四个版本号。工程上定义聚合版本：

```
pipeline_version = f"{clean_rules_ver}|{label_def_ver}|{prompt_ver}|{model_ver}|{constraint_ver}|{tag_space_ver}"
```

| 要素 | 变更触发 | 影响的指标 |
| --- | --- | --- |
| `clean_rules_ver` | 规则文件 SHA-256 变化 | MQ-06/07/08、SP-10 |
| `label_def_ver` | 标签体系发布/回滚 | 全部 MQ |
| `prompt_ver` | 提示词模板变化 | MQ-01~05、MQ-14 |
| `model_ver` | 模型/后端变化 | MQ-01~05、MQ-09、MQ-15 |
| `constraint_ver` | C1–C10 判定逻辑变化 | MQ-07、MQ-08 |
| `tag_space_ver` | 层权/IDF 变化 | 路 A 相关的业务指标、BE-05/06 |

**评测报告的强制规则**：

- 每个 `MetricValue` 带 `pipeline_version`。
- **跨版本比较必须显式声明**：报告若同时包含多个 `pipeline_version` 的数字，必须分表展示，禁止合并平均。
- `label_def_version` 与 `pipeline_version` 同时出现（前者是后者的子集，便于按体系版本筛选）。

### 决策 3 · `MetricValue` 与 `low_support` 机制

PRD MQ-02 规定「`support < 10` 的层标记 `low_support=true` 且不计入 MQ-01 平均」，§7.1.1 表述模板 A 要求「列出被剔除的层」。§7.5 声明 5 规定「低支撑层剔除必须在报告中显式列出」。

**实现**：`evaluation/metrics.py` 输出**三个** macro F1（见 §16.2 A-07）：

| 指标 | 公式 | 用途 |
| --- | --- | --- |
| `macro_f1_all12` | $(1/12)\sum_{L=1}^{12} F1_L$ | PRD MQ-01 的字面定义，**包含低支撑层** |
| `macro_f1_supported` | $(1/|L'|)\sum_{L \in L'} F1_L$，$L'=\{L : support_L \ge 10\}$ | **正式口径**（对应 §7.1.1 表述模板 A） |
| `macro_f1_by_l1_subset` | 按 L1 子集分别计算 | 对应 Q12「≥6 个 L1 子集宏 F1 ≥ 90%」 |

被剔除的层名**必须**出现在报告的 `low_support_layers` 字段中。这是 §7.5 声明 5 的机器化执行。

### 决策 4 · 五类回滚

| 类型 | 触发场景 | 操作 | 验证 | 数据影响 |
| --- | --- | --- | --- | --- |
| **① 标签体系版本回滚** | 新体系效果不佳 / 运营误操作 | 切换生效版本指针 | `tagmind labels diff --from v3 --to v2` | 无（历史内容保留原 `labels_version`） |
| **② 提示词版本回滚** | 模板变更后质量下降 | 改回旧 `PROMPT_VERSION` 常量 + 重启 worker（≤10 s） | `prompt_hash` 校验 | 无 |
| **③ 模型/后端回滚** | API 故障 / 模型效果不佳 | 切 `TAGMIND_LLM_BACKEND` / `TAGMIND_LLM_MODEL`；或依赖降级链自动降级 | 响应中的 `provider` + `degraded` | 无 |
| **④ 数据迁移回滚** | 迁移引入 bug | `alembic downgrade -1` | `scripts/consistency_check.py --after-migration` | 可能（但迁移禁删列，见下） |
| **⑤ 灰度与特性开关** | 需要快速关闭新行为 | 切开关（热生效，5 s 内） | 观察指标分组 | 无 |

**关键规则**：

- **① ② ③ ⑤ 完全无损**（只改「新内容的处理方式」，历史内容不动）。
- **④ 有损操作的禁令**：迁移中**禁止** `DROP COLUMN` / `DROP TABLE`。破坏性变更必须拆两版：① 加新列 + 双写 → ② 下个版本删旧列，且删列前必须有数据迁移验证脚本通过。
- **降级链单向**：`llm_api → llm_local → deterministic`；`lightfm → fallback_cf → recall_only`。**不设回环**，保证「一批内容内后端一致」。
- **灰度纪律**：所有灰度必须能在 **5 分钟内关闭**（切开关，不发版）。

### 决策 5 · 合成数据的标记与披露

| 数据 | 标记字段 | 披露位置 |
| --- | --- | --- |
| 合成内容 | `content.synthetic = true` | README + 演示脚本输出 |
| 合成曝光日志 | `exposure_log.synthetic = true` | 回放报告首页 |
| 合成金标 | `annotation_set.synthetic_gold = true`、`annotation.synthetic = true` | 评测报告首页 + README |
| 语料来源 | `annotation_set.source ∈ {synthetic, public-licensed, human}` | README 数据来源表 |

**README 的强制声明（Q11/Q18/Q19/Q20）**：

1. 「本项目为工程演示，**演示数据全部为合成或公开许可语料，不含任何平台真实 UGC**。」
2. 「`data/gold/` 中的 500 条参考集为**合成参考集**（`synthetic_gold=true`），**非人工标注**，用于验证评测流水线与口径一致性，**不得作为模型准确率结论的唯一依据**。」
3. 「`rules/` 下的敏感词与广告词为**示例规则集**，生产环境需替换为平台自有规则。」
4. 「**抽检回填需业务方参与，工程侧不代劳也不伪造**。抽检流水线只做到『生成抽检表』为止。」
5. 「留存（+25%）在无真实流量下**无法验证**，本项目仅提供『7 日回访率』代理指标并显式标注为代理。」

### 决策 6 · 报告的固定文案

| 报告 | 首页固定文案 |
| --- | --- |
| 离线回放 A/B | 「**本结果为离线日志回放仿真，非线上真实流量，不代表线上 CTR 提升**」 |
| 分层 F1 评测 | 「口径类型：`offline_evaluation`；样本量：{n}；金标集版本：{gold_version}（合成参考集 / 人工标注）」（并列出剔除的低支撑层） |
| 性能压测 | 「口径类型：`load_test`；基准硬件：8 核 CPU / 16 GB 内存 / 无独显 / PCIe SSD；后端：{backend}」 |
| 降级后端评测 | 「口径类型：`offline_evaluation` + `degraded_backend=true`；**不计入正式准确率结论**」 |

这些文案由 `evaluation/report.py` 中的常量生成，**不允许调用方自定义或省略**。

## Consequences

### 正面

- **诚实由结构保证**。`evidence_type` 无默认值 + 渲染器强制校验 → 结构上不可能产出一个缺 `evidence_type` 的报告。
- **`ONLINE_REAL` 结构性不可达**。本项目无真实流量，这从代码层面排除了「把仿真说成线上」的可能。
- **回滚是日常操作而非应急**。五类回滚都有明确的操作命令与验证脚本，runbook（NF-O08）直接引用它们。
- **版本可追溯**。任何一个数字都能反查到产生它的完整配置组合。
- **低支撑层剔除被强制披露**（§7.5 声明 5）。
- **开源合规有具体落点**：LICENSE、README 声明、合成数据标记三者齐备。

### 负面 / 代价

- **`MetricValue` 的字段较多**（10 个），构造代码冗长。缓解：提供工厂函数 `metric_from(...)` 与 `ReportBuilder` 上下文。
- **报告模板的固定文案不可定制**，业务方想改措辞需改代码。这是有意的 —— 文案是口径的一部分，不是展示层。
- **三个 macro F1 的输出**增加了报表的复杂度，容易被误用。缓解：README 与报告模板明确标注「正式口径 = `macro_f1_supported`」。
- **`synthetic` 标记的维护**：每条合成数据都要正确标记，漏标会造成口径混淆。缓解：种子生成器统一写入标记；一致性校验加一项「回放/评测使用的数据中 `synthetic` 标记与来源声明一致」。
- **`pipeline_version` 六元组在内容表里占 128 字节**，10 万行约 12 MB。可忽略。

### 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 有人绕过 `MetricValue` 直接输出裸数字 | 渲染器只接受 `MetricValue` 列表，裸数字无法通过类型检查；CI 加一条静态检查：报告模板中不得出现裸 `float` 输出 |
| 有人把 `offline_simulation` 的数字写进 README 时漏标 | README 的指标表由 `evaluation/report.py` **生成**（人工只填说明文字），并有 CI 检查 README 中所有百分数附近是否伴随 `evidence_type` |
| 灰度关闭不及时 | 灰度开关在 Redis `/2` 的 `config:live`（TTL 5 s），改动即时生效；告警规则监控灰度期的指标异常 |
| 迁移回滚时数据已不可逆 | 禁删列规则 + 迁移前 `pg_dump` + `make backup` + 迁移后校验脚本 |
| `pipeline_version` 被手工填错 | 由 `domain/versioning.py` 的 `PipelineVersion.build(...)` 统一构造，从配置读取，**不允许手工拼接字符串** |

## Alternatives Considered

| 方案 | 为何被否 |
| --- | --- |
| **在 README/报告里手工标注 `evidence_type`** | ① §7.5 声明 2 说「缺失即视为交付缺陷」，而手工标注必然会漏；② 评审与汇报的每一份材料都要手工改，无法规模化；③ 无法保证「已知偏差主动披露」不被遗漏。 |
| **用一个全局常量 `EVIDENCE_TYPE` 表示当前报告类型** | ① 同一份报告可能包含不同类型的指标（模型质量 + 系统性能 + 业务效果），全局常量会掩盖混用；② 无法表达「这条是降级后端结果」。 |
| **`evidence_type` 设为可选字段，给默认值** | 默认值就是「忘记填」的表现形式，缺陷被静默。**必须无默认值。** |
| **允许 `ONLINE_REAL`** | 本项目无真实流量（PRD §1.3 明确「真实线上 A/B 平台对接」为 Out of scope）。保留该枚举值但**结构性不可达**（需要 DB 中有 `online_gray` 的实验记录），比直接删掉更诚实 —— 它表明「未来灰度时可以扩展」。 |
| **只回滚 prompt，不支持标签体系回滚** | ① 标签体系的变更（新增/改名/调配额）同样可能引发质量问题；② PRD P4-TAG-006 明确要求版本化与回滚；③ 多版本并存的存储已实现（ADR-0008），回滚是零成本的。 |
| **迁移允许删列，靠备份恢复** | ① 恢复 10 万行数据的 RTO 不可接受；② 「删列」往往是「加列 + 双写 + 切读 + 删旧列」四步流程的第四步，压缩成一步会丢掉安全窗口。 |
| **降级链允许回环（api 恢复后自动切回）** | 破坏「一批内容内后端一致」，`model_version` 字段失去意义，指标口径混乱。**单向。** |
| **金标集直接宣称为「500 条人工标注」** | 直接违反 Q11「不得宣称为人工标注」与 PRD §7.5 声明 4 的偏差披露要求。**诚信事故。** |
| **把抽检一致性用模型自评代替人工回填** | 直接违反 Q20「工程侧不代劳也不伪造」与 PRD P4-EVA-005 的验收。`manual_filled=false` 时 `consistency_rate` 必须为 NULL。 |

## 引用与追溯

- PRD §7 全章（§7.5 诚信条款 6 条声明）、§7.1.1（表述模板 A/B）、§7.3.1（模板 A/B/C）、§7.5 声明 5（失败也是交付物）
- PRD §1.2（多目标指标须拆口径）、§1.3（Out of scope：无真实 A/B 平台）
- PRD §8.3 NF-O08（运行手册含模型版本回滚）、§8.2 NF-R09（备份恢复）、§8.4 NF-S10（开源合规）
- PRD §9.6（交付物清单：README 含指标口径表与已知限制）
- 主理人决策 Q11（合成金标 + 双盲 κ）、Q12（分层达标）、Q14（留存代理）、Q18（Apache-2.0 + 数据声明）、Q19（语料来源标注）、Q20（抽检不代劳）
- ADR-0003（`deterministic` 后端的口径歧义 A-08）、ADR-0008（多版本并存 → 回滚前提）
- `docs/architecture.md` §6.3（`pipeline_version` 贯穿）、§12（五类回滚）、§14.3（异常与错误码）、§16.2 A-07/A-08（口径歧义）
