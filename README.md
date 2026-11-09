# tagmind

![CI](https://github.com/Nephets1010/tagmind/actions/workflows/ci.yml/badge.svg?branch=main)
![Python](https://img.shields.io/badge/Python-3.11%20--%203.13-3776AB?logo=python)
![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688?logo=fastapi)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16%20%2B%20pgvector-4169E1?logo=postgresql)
![Coverage](https://img.shields.io/badge/coverage-71%25%20(%E9%97%A8%E6%A7%9B%2070%25)-4CAF50)

> 企业级 LLM 驱动的智能打标与个性化内容分发系统。

用一套**可运营的 12 层标签体系**，把 UGC/PGC 内容在分钟级变成「机器可读的兴趣向量」：
用大模型替代人工标注，用多路召回替代热门单点分发，每一条标签结论都可追溯、可回放、可抽检。

tagmind 面向「内容量大、标签体系复杂、召回延迟要求高」的内容平台，提供一条
完整链路：**接入 → 清洗 → 脱敏 → 打标 → 冲突仲裁 → 质量审核 → 画像 → 多路召回 → 排序 → 曝光回流**。

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [三支柱](#三支柱)
- [12 层标签体系](#12-层标签体系)
- [真实输出](#真实输出)
- [阶段状态](#阶段状态)
- [快速开始](#快速开始)
- [架构分层](#架构分层)
- [指标口径（诚实声明）](#指标口径诚实声明)
- [数据与合规声明](#数据与合规声明)
- [开发规范](#开发规范)
- [文档](#文档)
- [许可](#许可)

---

## 它解决什么问题

**问题一：人工标注贵且慢。** 一家内容平台日均数万条 UGC/PGC，人工逐条打标
既贵又慢，标签体系还常常停留在运营脑子里、无法沉淀为机器可读的坐标。

**问题二：标签体系深了就会打架。** 层级一多，父子标签冲突、互斥标签冲突
随之而来，最终标签无法解释，投放效果也无从归因。

**问题三：分发只靠热门。** 热门榜推给所有人，冷启动用户拿不到任何个性化
内容；一旦行为数据稀疏，协同过滤直接失效。

tagmind 给出的答案是：

```
一条内容进来
  → 清洗 / 脱敏 / 分块
  → 12 层标签体系打标（配置驱动，非硬编码提示词）
  → 冲突检测 + 仲裁（证据可追溯）
  → 质量双通道闸门（规则 + 评分，阈值可配）
  → 内容向量 + 标签向量入库
  → 用户画像（标签权重向量，与内容同构）
  → 多路召回（标签偏好 / 稠密向量 / 协同过滤 / 热门兜底）
  → 融合重排 → 分发
  → 曝光回流 → 画像增量更新
```

关键设计：**`user_profile.tag_weights` 与 `content.labels` 在同一个标签空间里**。
标签不只是展示用的分类，而是直接进入召回打分的偏好坐标。

---

## 三支柱

| 支柱 | 解决什么 | 关键设计 |
| --- | --- | --- |
| **12 层多标签体系** | 业务可解释 + 模型可打标 | L1–L4 严格树（单选/同父互斥），L5–L12 开放多选；标签定义配置化，加标签不改代码 |
| **RAG 冲突消解** | 标签打架时如何可解释地裁决 | 标签定义 + 金标样本入库 → top-k 检索取证 → CoT 仲裁 → 置信度闸门转人工；冲突 log 全量可回放 |
| **轻量推荐引擎** | 冷启动与稀疏行为 | 标签偏好召回 + 稠密向量召回 + 协同过滤 + 热门/最新兜底；LightFM 可选，纯 NumPy 备选实现保底 |

---

## 12 层标签体系

| 层 | 层名 | 结构 | 配额 | 召回特征 |
| --- | --- | --- | --- | --- |
| L1 | 领域域 Domain | 严格树 · 单选 | 1 | 强 |
| L2 | 一级类目 Category | 严格树 · 单选 | 1 | 强 |
| L3 | 二级类目 Sub-category | 严格树 · 同父互斥 | ≤3 | 强 |
| L4 | 主题簇 Topic | 严格树 · 同父互斥 | ≤3 | 强 |
| L5 | 事件与场景 Event/Scenario | 开放多选 | ≤5 | 是 |
| L6 | 人物实体 Person | 开放多选 | ≤5 | 弱 |
| L7 | 机构与品牌 Org/Brand | 开放多选 | ≤5 | 弱 |
| L8 | 地理与场所 Geo/Place | 开放多选 | ≤5 | 弱 |
| L9 | 商品与作品 Product/Work | 开放多选 | ≤5 | 弱 |
| L10 | 属性与量化 Attribute | 开放多选 | ≤8 | 否 |
| L11 | 消费意图 Intent | 开放多选 | ≤3 | 强 |
| L12 | 情感·立场·风险 | 开放多选 + 互斥组 | ≤3 | 强 |

**仓库内置 1490 个真实中文标签**（L1=20 / L2=71 / L3=206 / L4=593 / L5–L12=480），
不是 `TAG_001` 这类占位符。

**十条硬约束**（C1–C10）在写入时生效，违反即转人工复核：

| 约束 | 内容 |
| --- | --- |
| C1 | 树层唯一：L1–L4 每层一个主归属 |
| C2 | 同父互斥：同层同父标签不能并存 |
| C3 | 祖先闭包：命中深层标签必须命中其全部祖先 |
| C4 | 跨层可多选：L5–L12 允许多标签 |
| C5 | 每层配额：不得超过该层 `max_select` |
| C6 | 显式互斥组：同组最多一个 |
| C7 | 置信度门槛：低于阈值不进正式标签（保留在原始输出） |
| C8 | 语义冲突仲裁：疑似冲突走检索 + 仲裁 |
| C9 | 覆盖率闸门：无树标签的内容不发布 |
| C10 | 版本一致：内容标签版本与定义版本必须匹配 |

---

## 真实输出

```
$ python -m tagmind.cli seed-labels
标签体系导入完成：version=v1，标签数=1490
  L1=20  L2=71  L3=206  L4=593  L5=60  L6=60
  L7=60   L8=60  L9=60   L10=60 L11=60 L12=180
约束校验：A1–A10 零违规
```

```
$ python -m pytest tests -q
277 passed, 36 skipped in 16.60s
TOTAL   2588   628   400   42   71%
Required test coverage of 70% reached. Total coverage: 71.08%
```

---

## 阶段状态

本仓库当前处于 **阶段 0：工程地基**。

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| 0 | 配置 / 日志 / 异常 / 领域模型 / DB 层 / 迁移 / 标签体系 / 容器 / CI | ✅ 已完成 |
| 1 | 清洗分块脱敏、LLM 三档后端、提示词装配、Celery 与 DLQ | ⏳ 未开始 |
| 2 | RAG 冲突仲裁、质量审核双通道、用户画像与漂移 | ⏳ 未开始 |
| 3 | 向量存储、多路召回融合、重排、协同过滤 | ⏳ 未开始 |
| 4 | 完整 API、调度、可观测、评测回放、作品集化 | ⏳ 未开始 |

阶段 0 已交付的能力：

- **12 层标签体系**：1490 个真实中文标签，通过发布期校验（A1–A10 零违规），
  物化路径 / 邻接表 / 祖先数组三写一致。
- **16 张物理表**的 Alembic 迁移，幂等（连续执行两次 `upgrade head` 无报错），
  含分区表、不可变触发器与权限回收。
- **配置 / 日志 / 异常**三件套：生产环境缺密码即启动失败；日志敏感字段在
  渲染前脱敏；异常错误码唯一且分层。
- **领域层零框架依赖**：由 AST 扫描在 CI 中强制。

---

## 快速开始

### Docker（推荐）

```bash
cp .env.example .env
# 至少设置 TAGMIND_POSTGRES_PASSWORD（生产环境必填）
docker compose up -d --build
docker compose exec api alembic -c alembic.ini upgrade head
docker compose exec api python -m tagmind.cli seed-labels
```

五个服务：`api` / `worker` / `beat` / `postgres`（`pgvector/pgvector:pg16`）/ `redis`。
`api`、`worker`、`beat` **复用同一镜像**，靠 `TAGMIND_ROLE` 分派。

### 本地开发

```bash
pip install -e ".[dev]"
python -m tagmind.cli migrate
python -m tagmind.cli seed-labels
python -m tagmind.cli serve --reload
```

### 常用命令

| 命令 | 说明 |
| --- | --- |
| `make serve` / `make run` | 启动 API |
| `make migrate` | 应用数据库迁移 |
| `make labels` | 导入 12 层标签体系 |
| `make seed` | 灌入合成数据集 |
| `make test` | 跑全量测试 |
| `make lint` | ruff |
| `make typecheck` | mypy |

---

## 架构分层

```
tagmind/
├── config/      环境变量映射、常量、阈值、提示词模板
├── core/        异常体系、结构化日志、请求上下文、安全原语
├── domain/      纯领域模型（零框架依赖，由 AST 扫描强制）
├── db/          ORM 模型 + 仓储（业务模块唯一的数据入口）
├── ingestion/   清洗 · 分块 · 脱敏 · 去重
├── labeling/    标签树 · 提示词装配 · 约束校验 · 冲突仲裁 · 质量闸门
├── llm/         LLM 后端三档（deterministic / llm_local / llm_api）
├── vector/      向量存储与标签特征空间
├── profile/     用户画像构建 · 更新 · 衰减 · 漂移
├── recommendation/  多路召回 · 融合 · 重排 · 协同过滤
├── scheduler/   Celery 应用与定时任务
├── observability/  指标 · 健康探针 · 告警
├── evaluation/  金标 · κ · 分层 F1 · 回放 · 漂移抽检
└── api/         FastAPI 应用与路由
```

**依赖红线**：`domain/` 不得 import fastapi / sqlalchemy / pydantic / numpy /
celery / redis；业务模块一律经 `db/repositories` 访问数据库。

---

## 指标口径（诚实声明）

本项目对所有对外数字强制携带 `evidence_type`：

| 类型 | 何时可用 | 可否对外承诺 |
| --- | --- | --- |
| `offline_evaluation` | 有金标集 | 可承诺「在该测试集上的准确率」 |
| `offline_simulation` | 有历史日志的回放 | **不可**承诺「线上 CTR 提升」 |
| `online_gray` | 灰度真实流量 | 需显著性与实验期说明 |
| `online_real` | 全量真实流量 | 需 PRD 变更方可使用 |

`MetricValue` 的 `evidence_type` **没有默认值**——忘记填会在构造时直接
`TypeError`，而不是在报告里静默缺失。这是刻意的摩擦。

---

## 数据与合规声明

- 仓库内**所有演示数据都是合成数据**（`synthetic=true` 显式标记），
  不含任何平台真实 UGC。
- `raw_text` 是未脱敏的原始正文（受控访问）；送入 LLM 的一定是
  `clean_text`（已脱敏）。
- `pii_mask_map` 只存 sha256 哈希，不存原值，且对 `PUBLIC` 无权限。
- 所有外发请求记 `egress_log`（不含正文）。

---

## 开发规范

```bash
ruff check src tests          # lint
ruff format --check src tests # 格式
mypy src/tagmind              # 类型
pytest tests -q               # 测试（覆盖率门槛 70%）
```

提交前请确保四条命令全绿。密钥扫描（`sk-` / `AKIA` / `token=` 模式）在 CI 中
强制执行。

---

## 文档

| 文档 | 内容 |
| --- | --- |
| [`docs/prd.md`](docs/prd.md) | 产品需求文档：用户故事、67 条需求池、12 层体系设计、指标口径、非功能需求、阶段计划 |
| [`docs/architecture.md`](docs/architecture.md) | 架构设计：分层图、组件清单、完整文件列表、最终 DDL、召回链路时序、调度、20 任务分解 |
| [`docs/adr/`](docs/adr/) | 10 篇架构决策记录（向量库、标签规模、LLM 分档、CF 备选、幂等与 DLQ、口径诚实与回滚……） |
| [`docs/qa/`](docs/qa/) | 各阶段 QA 验证报告 |

贡献指南见 [`CONTRIBUTING.md`](CONTRIBUTING.md)，
行为准则见 [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)，
安全问题请见 [`SECURITY.md`](SECURITY.md)。

---

## 许可

- **源代码**：[Apache License 2.0](LICENSE)
- **文档与设计材料**（`docs/`、ADR、README 架构描述）：[CC BY 4.0](LICENSE-docs)

Copyright (c) 2026 Nephets1010
