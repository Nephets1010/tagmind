# ADR-0005 · 协同过滤双后端：LightFM 可选 + 纯 NumPy `fallback_cf` 必交付

| 项 | 值 |
| --- | --- |
| **状态** | 已接受 |
| **日期** | 2026-11-08 |
| **决策者** | 架构师 高见远（Gao） |
| **关联** | PRD §10-Q5、§10-Q7、§3.1.5 P4-REC-003、§4.7；主理人决策 Q5、Q7 |
| **影响组件** | `src/tagmind/recommendation/cf/{base,lightfm_backend,fallback_cf}.py`、`recommendation/recall/collaborative.py`、`pyproject.toml` |

---

## Context

PRD §4.7 是本推荐系统的核心设计声明：

> 标签不是「展示用的分类」，而是**与用户画像同构的坐标**。`content.labels` 与 `user_profile.tag_weights` 共享 `label_id` 空间，因此标签偏好匹配可以退化为一次向量点积，无需额外训练映射层；**LightFM 只需把标签作为特征（而非 item ID）即可实现「新品冷启」**（新内容即使没有交互数据，也能通过标签特征被召回）。

PRD §5.5 的召回链路里有第三路「协同过滤 LightFM 重排」，P4-REC-003 进一步规定：

> Given 有足量交互历史的用户（≥ 20 条 `exposure_log`），When 执行排序，Then 返回 `algo=lightfm` 标识且 `score` 来自 LightFM 预测分（与纯召回分可区分）；交互历史不足时降级为 `algo=recall_only` 并在响应 `debug.fallback_reason` 注明 `insufficient_interactions`；降级路径下**不得报错、不得返回空**。

但 PRD §10-Q5 记录了一个**严重的工程障碍**：

> LightFM 自 2023-03 后未再发版，PyPI 上 15 个 release 全是源码分发、无任何 wheel，当前 pip 直接安装会在元数据生成阶段失败。可行路径：(a) `pip install git+https://github.com/lyst/lightfm@master`（需 git + C 工具链，锁未打标签提交）；(b) 社区分支 `lightfm-next`（模块名与 API 不变，但只提供 cp312/cp313 wheel，Python 3.11 仍需源码编译）。

主理人决策 Q5 已定：LightFM 放可选依赖 `[rec]` extra，编译失败时降级 `recall_only`；**必须实现纯 Python 纯 NumPy 的 `fallback_cf`** 作为独立后端，结果**不与 LightFM 混算**、口径分别标注。Q7 进一步确认：纯 Python 备选实现是**必须交付项**，不是可选项。

因此本 ADR 需要确定的是：

1. `fallback_cf` 的**算法选型**（什么方法能在纯 NumPy 下达到可用质量）
2. 两个后端的**契约边界**与**口径隔离机制**
3. **降级链**的精确语义

## Decision

### 决策 1 · `fallback_cf` 采用两种互补算法的组合

**算法 A · Item-based 标签共现相似度**（主算法）

内容-内容相似度基于「在同一用户行为中共同出现」的归一化共现：

$$
sim(c_1, c_2) = \frac{\big|\{(z, c_1, c_2) : c_1, c_2 \in I_z\}\big|}{\sqrt{|I_{c_1}| \cdot |I_{c_2}|}}
$$

推荐分：

$$
s_{item}(z, c) = \sum_{c' \in I_z^{recent}} sim(c, c') \cdot e^{-\lambda(t_{now} - t_{c'})}
$$

- 纯 NumPy：用交互矩阵的稀疏表示（`scipy.sparse` 或纯 `dict` + NumPy 累加）计算共现，10 万内容 × 1000 用户规模下分钟级可完成全量预计算。
- 优势：**无需迭代训练**，对交互数据稀疏（千级用户 × 十万内容）非常鲁棒；可解释（可回溯「因为你也看了 X」）。
- 与 PRD §4.7 的标签同构设计契合：item 的特征就是标签向量，共现矩阵天然捕获「标签偏好相近的内容」。

**算法 B · 隐式反馈 ALS**（可选增强）

目标函数（隐式反馈 + 置信度加权）：

$$
\min_{\mathbf{P}, \mathbf{Q}} \sum_{(z,c)\in\Omega} c_{zc}\big(r_{zc} - \mathbf{p}_z^\top \mathbf{q}_c\big)^2 + \lambda\big(\|\mathbf{P}\|_F^2 + \|\mathbf{Q}\|_F^2\big)
$$

- $r_{zc}=1$（隐式），$c_{zc}=1+\alpha r_{zc}$（$\alpha=10$），因子维度 $d=32$，$\lambda=0.05$。
- 交替最小二乘闭式解：

$$
\mathbf{p}_z = \big(\mathbf{Q}^\top \mathbf{C}^z \mathbf{Q} + \lambda \mathbf{I}\big)^{-1} \mathbf{Q}^\top \mathbf{C}^z \mathbf{r}^z
$$

- **纯 NumPy 实现**：`np.linalg.solve` 解 $32\times32$ 系统（约 0.05 ms/用户），10 轮迭代收敛。
- BPR 作为第二种口味（`TAGMIND_CF_FALLBACK_MODE=als|bpr|item`），SGD 训练。

> **依赖决策**：ALS 闭式解**不需要 scikit-learn**（那是本项目可选依赖，不是必需）。`fallback_cf.py` 只 import numpy。scikit-learn 保留在可选列表中，仅用于交叉验证参考实现。

### 决策 2 · 统一 `CFBackend` Protocol + 三个实现

```python
class CFBackend(Protocol):
    name: str  # "lightfm" | "fallback_cf" | "recall_only"
    algo: str  # 写入 exposure_log.algo 的口径标签

    def fit(self, interactions: InteractionMatrix) -> None: ...
    def predict(self, user_id: str, *, top_k: int) -> list[CFHit]: ...
    def health(self) -> BackendHealth: ...
```

| 实现 | `algo` | 依赖 | 状态 |
| --- | --- | --- | --- |
| `lightfm_backend.py` | `lightfm` | `[rec]` extra | **可选**，编译失败即不可用 |
| `fallback_cf.py` | `fallback_cf` | 仅 numpy | **必交付** |
| （内建）`recall_only` | `recall_only` | 无 | 最终兜底 |

### 决策 3 · 降级链与口径隔离

```
lightfm  →  fallback_cf  →  recall_only
```

**每一跳都必须**：

1. 在响应的 `algo` 字段写入**实际使用的后端标识**（`lightfm` / `fallback_cf` / `recall_only`）。
2. 在 `debug.fallback_reason` 写入降级原因：`lightfm_unavailable` / `insufficient_interactions` / `cold_start`。
3. 在 `exposure_log.algo` 写入同一标识 —— **按 `algo` 分列统计，绝不跨后端平均**。

**「不混算」的工程保证**：

| 机制 | 实现 |
| --- | --- |
| 存储层 | `exposure_log.algo` 是普通列，所有报表按它 `GROUP BY` |
| 评测层 | `evaluation/replay.py` 与 `evaluation/report.py` 中，`lightfm` 与 `fallback_cf` 的结果**分表输出**，不合并计算均值 |
| 文档层 | README 的指标口径表必须分列；若某次实验只用了 `fallback_cf`，报告中显式标注 |
| 禁止 | 不允许把两者的预测分放进同一个归一化池（那等价于混算） |

### 决策 4 · 「有效交互」的口径定义

P4-REC-003 的「≥ 20 条 `exposure_log`」需要明确「什么算一条交互」。定义：

**有效交互 = `action ∈ {click, dwell}`**（`skip` 与 `view` 不计入交互数，但 `skip` 计入 `behavior_count` 的负向贡献、`view` 计 0.5 倍）。

阈值 `TAGMIND_REC_CF_MIN_INTERACTIONS`（新增可配项，默认 20）。**离线回放中按回放时间窗统计**，保证可复现。

### 决策 5 · 交互不足时的降级粒度

P4-REC-003 要求「交互历史不足时降级为 `algo=recall_only`」。本 ADR 明确：

- `fallback_cf` 在交互不足时**同样不能给出有意义的排序**（协同过滤的本质要求交互历史）。
- 因此：**交互 < 阈值 → 直接 `recall_only`**，`fallback_cf` 只在「交互足够但 LightFM 不可用」时顶替。
- `recall_only` 的分数 = 路 A + 路 B 的融合分，**必须与 CF 预测分可区分**（P4-REC-003：「score 来自 LightFM 预测分（与纯召回分可区分）」）。

## Consequences

### 正面

- **LightFM 的编译问题不再是阻塞项**。Q5 原本是「[阻塞] 阶段 3」的问题，现在降级为「有则用、无则用 `fallback_cf`、再无则 `recall_only`」，**核心链路不会被依赖卡死**。
- **`fallback_cf` 是纯 NumPy**，不引入任何编译型依赖，在任何 Python 3.11 环境都能跑，CI 可测。
- **口径清晰可审计**。`algo` 三值 + `fallback_reason` 的组合，让「这次推荐到底用了什么」在响应、日志、报表三处完全一致。
- **item-based 算法可解释**，与 PRD「标签体系是可运营资产」的可解释性基调一致。

### 负面 / 代价

- **质量低于 LightFM**。纯 ALS 在数据量小时不如 LightFM 的混合特征（LightFM 可同时使用 item ID + tag 特征 + 用户特征）。`fallback_cf` 的 item-based 版本实际是「标签特征 + 共现」的简化版。
- **两套实现要维护**。`fallback_cf` 约 420 行，且需要与 `lightfm_backend.py` 保持契约一致（靠 `CFBackend` Protocol 的契约测试保证）。
- **训练与推理成本**。item-based 相似度矩阵需要离线预计算（分钟级），必须由调度任务维护，不能在线算。
- **口径分裂的报表复杂度**。同一个「CTR」指标可能需要按 `algo` 分三行报告。这增加了报告代码复杂度，但这是 Q7 明确要求的诚实性代价。

### 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| `fallback_cf` 与 LightFM 结果差异大，用户困惑 | `algo` + `fallback_reason` 显式暴露；README 给出「口径对照表」；报表分列 |
| ALS 数值不稳定（奇异矩阵） | 加 $\lambda I$ 正则（$\lambda=0.05$）；用 `np.linalg.solve` 而非 `inv`；失败时捕获 `LinAlgError` 并降级到 item-based |
| item-based 相似度矩阵在 10 万内容上过大 | 只对有交互的内容计算（千级用户 × 每日数十条 → 活跃内容约 1–3 万条）；用稀疏 dict 存储；超阈值时截断 top-N 相似项（每内容保留 top-100） |
| 有人误以为两者结果可合并 | 契约测试断言 `exposure_log.algo` 的取值集合固定为三个；报告代码中不存在跨 `algo` 的聚合函数 |

## Alternatives Considered

| 方案 | 为何被否 |
| --- | --- |
| **只用 LightFM，编译失败即整体不可用** | ① 直接违反 Q7「纯 Python 备选实现是必须交付项」；② Q5 明确它有源码编译风险且 3.11 无 wheel；③ 违反 P4-REC-003「降级路径下不得报错、不得返回空」—— 无 `fallback_cf` 时降级只能到 `recall_only`，协同过滤一路完全缺失。 |
| **只用 `fallback_cf`，不实现 LightFM 适配** | ① 违反 Q5/Q7 的双后端要求；② LightFM 的混合特征（标签作为特征而非 item ID）正是 PRD §4.7 所述实现「新品冷启」的更优方案，放弃它会削弱设计一致性；③ 缺少一个可对比的基线，无法说明「换个 CF 实现效果差多少」。 |
| **`fallback_cf` 用 scikit-learn 的 `NMF` / `TruncatedSVD`** | ① 引入 scikit-learn 作为必需依赖（与 Q5「减少编译/依赖风险」的初衷相反，虽然 sklearn 有 wheel）；② SVD 分解在隐式反馈上没有置信度加权，效果不如 ALS；③ 纯 NumPy 的 ALS 闭式解只有约 40 行核心代码，无必要引入依赖。 |
| **`fallback_cf` 用 SVD 纯奇异值分解** | 无置信度加权，对「点击 1 次 vs 点击 50 次」不做区分，效果显著差于 ALS。 |
| **两者结果做加权平均融合** | **直接违反 Q7 的「结果不与 LightFM 混算」**。两者的分数分布与语义完全不同，混合后既不可解释也不可审计。 |
| **把 `fallback_cf` 也标成 `algo=lightfm`** | 制造口径造假，让报告无法区分真实 LightFM 与备选实现的结果。**诚信事故。明确禁止。** |
| **用「标签共现」直接作为路 A（标签偏好召回）的一部分，不单独做 CF** | 路 A 是「用户标签权重 × 内容标签向量」的**内容侧**匹配；CF 是**用户行为侧**的共现。两者信息来源不同（标签 vs 交互），合并会丢失 CF 的独立价值（利用群体行为信号，P4-REC-003 的明确诉求）。 |

## 引用与追溯

- 主理人决策 Q5（LightFM 可选 + 编译失败降级）、Q7（纯 Python 备选是必须交付项、结果不混算）
- PRD §10-Q5（LightFM 编译障碍）、§10-Q7（备选实现的必要性）
- PRD §3.1.5 P4-REC-003（`algo` 标识、降级路径、分数可区分）
- PRD §4.7（标签作为特征的冷启价值）、§5.5（三路召回）
- PRD §7.3 BE-05/BE-06（召回源多样性、个性化占比 —— 需按 `algo` 分列）
- `docs/architecture.md` §11.9（ALS/BPR/item-based 目标函数）、§14.7、§16.2 A-06 / A-09
