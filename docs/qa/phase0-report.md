# tagmind 阶段 0（工程地基）独立 QA 验证报告

| 项 | 值 |
| --- | --- |
| 报告人 | 严过关（Yan）· QA 工程师 |
| 验证对象 | T01 配置/日志/异常/领域模型 · T02 数据库与 16 表迁移 · T03 标签树与 12 层种子 · T04 容器化与 CI |
| 仓库 | `D:/data/WorkBuddy/程序宇宙/repos/tagmind` |
| 环境 | Windows 11 / Python 3.13.14；**无 PostgreSQL、无 Docker**（`psql`/`pg_isready`/`docker` 均不存在） |
| 基准提交 | `9e61209 fix(qa): resolve two phase-0 blockers and repo-wide lint gaps`（验证期间队友陆续提交了 `2b583c9` / `e08e59a` / `d095b74` / `9e61209`） |
| 报告生成 | 2026-11-09 |

> **并发说明（影响读数）**：本次验证期间，其他队友持续向同一工作树提交代码并开始阶段 1 的在途改动。为避免把「别人正在写的代码」当成阶段 0 交付态，我用 `git archive HEAD | tar -x -C /tmp/qa_pristine` 建立**干净 HEAD 快照**作为判定基准，并把 QA 新增测试复制进 `/tmp/qa_head` 单独复跑。下文明确区分 **A = 阶段 0 交付态**、**B = 当前工作树（含他人在途阶段 1 文件）** 两组读数。

---

## 1. TL;DR

**阶段 0 仍不予通过验收，但两个启动级 Blocker 已修复并复验通过。** 主理人核实了我 TL;DR 里的 2 个 Blocker 全部属实，并已落地修复（celery `include` 缺包、`migration-idempotent` 连接串变量名不匹配），同时补齐全仓 lint 门禁与 `consistency_check.py` 的 `Any` 导入；详见 **§10 主理人修复后复验**。修复后门禁绿：`277 passed, 39 skipped`，覆盖率 `70.86%`（主理人复验）；我含 72 个新增测试的独立复验为 `349 passed / 39 skipped / 73.12%`（口径差异见 §2）。

**仍待工程师修复（本轮未动）**：`BaseRepository` 对 3 张无 `id` 列主键表 + 1 张复合主键表的通用方法有 3 处确定性运行错误（D-03，精确清单见 §6.1）；CI 的集成测试步骤仍是「恒假绿」，T02 五项 DB 验收零覆盖（D-02）；`mypy src/tagmind` 仍有 1 个类型错误（D-01 的 mypy 部分）；`nightly.yml` 未同步连接串修复（D-04）；compose 硬编码密码 / 绕过生产守卫 / worker-beat 无 healthcheck（D-05、D-08）；README「真实输出」伪造（D-09）；T03 ⑥ 标签树 API 端点不存在（D-10）。

**路由判定：Engineer** —— 2 个 Blocker **已修复**，但上列待修项仍需工程师处理，故不是 NoOne；测试代码本身的 bug 我已自修，不路由给 Engineer。

> 主理人所修的 3 个缺陷（ORM 重复注册、`/readyz` connect_timeout、Celery `task_failure` 缺 `**kwargs`）我已逐一复验，**修复彻底、未引入新问题**；详见 §3.5。**已修复组** / **待修复组** 的完整分界见 §9 与 §10。

---

## 2. 独立复验结果（自己跑的四条命令，真实终端回显）

### 2.1 A 组：阶段 0 交付态（干净 HEAD `9e61209` + QA 新增测试）

```
[A1] $ python -m ruff check src tests
All checks passed!

[A2] $ python -m ruff format --check src tests
77 files already formatted

[A3] $ python -m mypy src/tagmind
pyproject.toml: note: unused section(s): module = ['FlagEmbedding.*', 'lightfm.*', 'numpy', 'numpy.*', 'sentence_transformers.*', 'torch.*']
Found 1 error in 1 file (checked 57 source files)

[A4] $ python -m pytest tests -q
Required test coverage of 70% reached. Total coverage: 73.12%
349 passed, 39 skipped in 14.48s
```

### 2.2 B 组：当前工作树（含他人阶段 1 在途文件 `src/tagmind/ingestion/pii.py`、`rules.py` 等）

```
[B1] $ python -m ruff check src tests
Found 2 errors.  (SIM110 in ingestion/pii.py:172; UP035 in ingestion/rules.py:26)

[B2] $ python -m ruff format --check src tests
1 file would be reformatted, 78 files already formatted   (ingestion/rules.py:331)

[B3] $ python -m mypy src/tagmind
Found 1 error in 1 file (checked 59 source files)

[B4] $ python -m pytest tests -q
FAIL Required test coverage of 70% not reached. Total coverage: 67.21%
349 passed, 39 skipped in 14.16s
```

### 2.3 原样 HEAD（未加 QA 测试）与全仓 lint

```
$ cd /tmp/qa_pristine && python -m pytest tests -q
Required test coverage of 70% reached. Total coverage: 70.10%
274 passed, 42 skipped in 14.10s

$ python -m ruff check .          -> Found 1 error   (F401 scripts/_qa_mutate.py:10 shutil unused)
$ python -m ruff format --check . -> 3 files would be reformatted, 99 files already formatted
                                    (scripts/_qa_mutate.py / _qa_ddl_diff.py / tests/integration/test_label_seed_import.py:200)
$ python -m mypy --no-incremental src/tagmind -> Found 1 error (interaction_repo.py:143)
```

### 2.4 四条命令汇总判定

| 命令 | A（交付态 + QA 测试） | B（当前工作树） | 判定 |
| --- | --- | --- | --- |
| `ruff check src tests` | All checks passed! | Found 2 errors | A **已验证通过** / B **已验证失败**（在途代码） |
| `ruff format --check src tests` | 77 files already formatted | 1 file would be reformatted | A **已验证通过** / B **已验证失败** |
| `mypy src/tagmind` | **Found 1 error** | **Found 1 error** | **已验证失败**（见 D-01） |
| `pytest tests -q` | 349 passed / 39 skipped / 73.12% | 349 passed / 39 skipped / **67.21% FAIL** | A **已验证通过** / B **已验证失败** |
| `ruff check .`（CI 现用命令） | 全仓仍有残留 | — | **已验证失败**（见 D-01） |

> **重要差异说明**：主理人复验的「ruff check: All checks passed! / 74 files already formatted / mypy: Success / 277 passed, 36 skipped, 71.08%」是在**更早的未提交工作树 + 限定 `src tests` 范围**下取得的。我首次全仓执行 `ruff check .` 得到 **17 个错误**（6 F821 / 6 I001 / 2 E402 / 2 F401 / 1 RUF100），`ruff format --check .` 得到 **9 files would be reformatted**。`9e61209` 已把 CI 的 lint 范围从 `src tests` 扩到 `.` 并修掉大部分，但 HEAD 上仍有残留（详见 D-01）。

---

## 3. 测试真实性审查（攻击测试套件，而非只看绿灯）

### 3.1 变异测试（Mutation Testing）：15 个反向变异，6 个存活

我写了变异脚本，把 `src/` 里的实现改成明显错误，再跑 `tests/unit` 看是否有测试变红。**存活 = 测试套件抓不住这个 bug = 假通过缺口。**

```
[BASELINE] 274 passed
[KILLED   ] M01_token_bucket_always_true        (TokenBucket.consume 恒返回 True)
[KILLED   ] M02_duplicate_error_code            (NotFoundError 改成与 ValidationError 同码)
[KILLED   ] M03_logging_drop_password_key      (脱敏清单删掉 "password")
[KILLED   ] M04_database_url_no_quote          (resolved_database_url 不做 URL 编码)
[KILLED   ] M05_ancestors_reversed             (ancestors 反序)
[SURVIVED ] M06_clear_context_noop             (clear_context 改成空操作)
[SURVIVED ] M07_optimistic_update_always_true  (乐观锁恒返回 True)
[SURVIVED ] M08_insert_ignore_always_new       (幂等冲突恒报 inserted=True)
[SURVIVED ] M09_drop_server_managed_disabled   (created_at=None 覆盖 now())
[SURVIVED ] M10_retry_no_retry                 (死锁不再重试)
[SURVIVED ] M11_validate_structure_layer_check_off (发布期 A2 父层校验失效)
[KILLED   ] M12_domain_imports_sqlalchemy     (领域层混入框架 import)
[KILLED   ] M13_production_no_password_ok     (生产缺密码不报错)
[KILLED   ] M14_celery_signal_no_kwargs       (task_failure 去掉 **extra)
[KILLED   ] M15_no_request_id_header          (不返回 X-Request-ID)
存活变异体数量：6 / 15
```

**直接回答主理人的提问**：把 `TokenBucket.consume` 改成恒 `True`，`tests/unit/test_security.py::test_consume_beyond_capacity_fails` 会立刻变红 —— 该断言有效，不是恒真断言。

**6 个存活点全部集中在 `BaseRepository` 的 4 条语义、`clear_context`、以及 `validate_release` 的 A2 分支**——正是任务书点名要补的空白。我新增测试后重新变异验证：

```
baseline rc = 0
[KILLED] M06 / M07 / M08 / M09 / M10 / M11  -> 全部 rc=1
```

6 个存活变异体**全部被新增测试杀死**，缺口已闭合。

### 3.2 抽查的 5 个单元测试（断言有效性）

| 测试 | 结论 | 依据 |
| --- | --- | --- |
| `test_security.py::test_consume_beyond_capacity_fails` | **有效** | 变异 M01 被杀死；断言 `assert bucket.consume() is False` 是严格布尔 |
| `test_errors.py::test_error_codes_unique` | **有效但覆盖不全** | 变异 M02 被杀死；但它只遍历硬编码的 18 个 `REQUIRED_ERRORS`，新增异常类若重复错误码不会被抓 → 我补了动态反射扫描 |
| `test_logging_redaction.py::test_password_field_is_masked` | **有效** | 变异 M03 被杀死；但只测了清单里 1 个词 → 我补了全清单 + 大小写 + 嵌套 + 值级兜底 |
| `test_settings.py::test_database_url_derived_from_parts` | **有效但偏弱** | 变异 M04 被杀死；原断言只查 `" " not in url`，未验证 `@ : / ? # %` 与无损还原 → 我补了参数化转义测试 |
| `test_taxonomy_tree.py::test_ancestors_array_correct` | **有效** | 变异 M05 被杀死；但 `validate_release()` 的 A2 分支（变异 M11）无人测 → 我补了构造违规树的用例 |

### 3.3 「被跳过但本该能跑」的测试 —— 找到 3 个（QA 侧测试 bug，已自修）

`pytest -ra` 显示 B 组 39 条 skip 全部来自 `tests/integration/` 三个文件，**表面上全部是集成测试**。但深挖发现 `tests/integration/test_repositories.py` 有一个 `autouse=True` 的 `clean_tables` fixture，它把 `test_session`（→ `test_engine` → `migrated_db` → `db_url`）挂在了**每一个**测试上，导致 3 个纯逻辑测试在无 DB 时被连带 skip：

```
$ python -m pytest tests/integration/test_repositories.py::test_retry_on_deadlock_passes_through \
    tests/integration/test_repositories.py::test_retry_on_deadlock_gives_up \
    tests/integration/test_repositories.py::test_filterspec_ignores_none -q -ra
3 skipped  （原因：需要 TAGMIND_TEST_DATABASE_URL）
```

这 3 个测试只操作 `retry_on_deadlock` 与 `FilterSpec`，**完全不需要数据库**。

**QA 自修**：把 `clean_tables` 改为按 `request.fixturenames` 惰性解析（纯逻辑测试不触发 DB fixture 链）。修后：

```
3 passed, 15 skipped in 0.76s
```

36 → 39 skip 的变化里，有 3 条是「本该跑却没跑」，现已恢复执行。

### 3.4 `test_db_metadata.py` 与 `test_domain_purity.py` 的攻击验证

**「迁移表名集合 == ORM 表名集合」会不会因两边都为空而假通过？—— 不会，已实测否证。**

- 该用例名为 `test_migration_tables_equal_orm_tables`，实际断言是 `_migration_table_names() == set(EXPECTED_TABLES)`，其中 `EXPECTED_TABLES` 是**硬编码 16 项 frozenset**，不是 `Base.metadata.tables`。因此任一为空都会与 16 项常量不等而失败（命名有误导性，见 D-17）。
- 我注入一个伪造迁移文件 `migrations/versions/_qa_phantom.py`（含 `CREATE TABLE IF NOT EXISTS qa_phantom ...`）：

```
FAILED TestMigrationMatchesOrm::test_migration_tables_equal_orm_tables
FAILED TestMigrationMatchesOrm::test_no_migration_creates_unknown_table
E  AssertionError: 迁移创建了契约外的表：['qa_phantom']
```

  → 断言**确实生效**，不是恒真。探针文件已删除。
- `test_domain_dir_exists` 断言 `len(_domain_files()) >= 10`，防住了「目录为空导致参数化零用例」的假通过。

**AST 纯净性检查能否拦住框架 import？—— 能，已实测。**

在 `src/tagmind/domain/` 下写入探针 `_qa_violation_probe.py`（`import sqlalchemy`）：

```
FAILED tests/unit/test_domain_purity.py::test_domain_has_no_framework_imports[_qa_violation_probe.py]
E  AssertionError: 领域层出现框架依赖：_qa_violation_probe.py:2 import sqlalchemy
```

探针文件已删除。残留局限：只检查 `ast.Import` / `ast.ImportFrom`，`importlib.import_module("fastapi")` 这类动态导入拦不住（D-18，Minor）。

### 3.5 主理人 3 个修复的彻底性复验

| 修复 | 复验证据 | 结论 |
| --- | --- | --- |
| ① ORM 表重复注册 | 递归收集 `Base.__subclasses__()` 的 `__tablename__`：`重复 __tablename__: {}`、`metadata 表数: 16`；`test_reimporting_models_is_idempotent` 通过 | **彻底** |
| ② `/readyz` 缺 `connect_timeout` | `db/session.py:56` 有 `"connect_timeout": resolved.db_connect_timeout`；`settings.py:89` 默认 3；`.env.example` 有 `TAGMIND_DB_CONNECT_TIMEOUT=3` | **彻底** |
| ③ Celery `task_failure` 缺 `**kwargs` | `celery_app.py:73` 有 `**extra: object`；把该行删掉后 `test_api_and_scheduler.py::TestCeleryApp::test_app_name` 立刻在 `@task_failure.connect` 处抛 `ValueError: Signal receiver must accept keyword arguments.` | **彻底，且有回归测试守护** |

**未发现这 3 个修复引入新问题。**

### 3.6 恒真 / 无意义断言扫描

```
tests/unit/test_api_and_scheduler.py:40       assert app is not None
tests/unit/test_repositories_unit.py:232      assert statement is not None
tests/unit/test_taxonomy_tree.py:71/73/89/111 assert ... is not None
```

未发现 `assert True`。上面两条是弱断言（`create_app()` 返回 `FastAPI` 类型上就非 None；`execute()` 必然返回 statement），但**不是恒真**——若实现改成返回 `None` 仍会红。列为 Minor（D-19）。

---

## 4. 新增测试清单

| 文件 | 用例数 | 覆盖内容 |
| --- | --- | --- |
| `tests/unit/test_qa_core_semantics.py`（新增） | 50 | ① `errors.py` **动态反射全部子类**（非硬编码 18 个）的错误码唯一性 / 段位 / HTTP / payload / 内置名遮蔽 / `__all__` 导出；② `context.py` 四字段清理、可选字段语义、跨 `Context` 隔离、ID 格式、`to_log_fields`；③ `logging.py` 17 个敏感键名族（含大小写变体、`_key` 后缀）、`_SENSITIVE_KEY_TOKENS` 每项逐一生效、嵌套 dict/list、值级 sk-/AKIA/Bearer 兜底、正文长度+哈希、error 截断精确边界、count-only、user_id 哈希；④ `settings.py` 5 组特殊字符密码的 `quote(safe="")` 逐字一致 + `unquote` 无损还原、显式 URL 优先、Redis 三库分离保留凭据/query、`.env.example` ⊇ 对齐；⑤ `MetricValue.evidence_type` 无默认值（ADR-0010）。 |
| `tests/unit/test_qa_repository_and_static.py`（新增） | 22 | ① `BaseRepository.optimistic_update` 的 `True`/`False`/显式版本/行不存在四分支（杀 M07）；② `insert_ignore` 的 `inserted=True/False`、SAVEPOINT rollback/commit、冲突但查不到既有行时重抛（杀 M08）；③ `_drop_server_managed` 对 `created_at`/`published_at` 的区分（杀 M09）；④ `retry_on_deadlock` 重试成功 / 耗尽 / 不可重试 sqlstate 不重试（杀 M10）；⑤ `validate_release` 的 A1/A2/A4 违规能被检出（杀 M11）；⑥ 静态契约：迁移 `down_revision` 线性链、compose 五服务、HEALTHCHECK 路径一致、无明文密码字面量、celery `include` 目标可 import、16 表契约。 |
| `tests/integration/test_repositories.py`（**修改**，QA 自修） | +3 实际执行 | 修 `autouse` fixture 误 skip（见 §3.3）。 |

**新增测试合计 72 个**（50 + 22），另修复 3 个被误 skip 的既有用例使其真正执行。全部为纯逻辑，不依赖 PG / Redis / Docker。

### 4.1 关于「skip 从 36 变 39」的澄清（主理人问项）

**结论：我新增的测试没有把任何本该能跑的用例误标成 integration，恰恰相反——我把 3 个被误 skip 的纯逻辑测试恢复了执行。**

- 我新增的 2 个文件都在 `tests/unit/` 下，**没有** `pytest.mark.integration` 标记、**没有** `pytestmark`、不引用 `db_url` / `migrated_db` / `test_session` 等 DB fixture；自建 `_FakeSession` / `_FakeResult` 全部在内存里。实测 `python -m pytest tests/unit/test_qa_core_semantics.py tests/unit/test_qa_repository_and_static.py -q --no-cov` → **`72 passed in 0.84s`（0 skipped）**。
- 主理人观察到 `pytest tests/unit` → **346 passed**，与本报告的 72 个新增用例自洽（原 274 个 unit 用例 + 72 = 346）。
- skip 数从 36 → 39 的**唯一来源**是队友在验证期间新增了 3 条集成测试（`tests/conftest.py` 的 `migrated_db` fixture 落地后，`test_migrations_idempotent.py` / `test_repositories.py` 增补了用例），与我的改动无关；我这边做的是 **39 → 39 但把其中 3 条从 skip 变成 pass**（见 §3.3）。全量读数：`349 passed, 39 skipped`。
- 反向确认：修 `autouse` fixture 前，单独跑那 3 个纯逻辑用例是 `3 skipped`；修后是 `3 passed, 15 skipped`（该文件内），且它们仍在 `tests/integration/` 目录下、仍带 `pytest.mark.integration` 标记——这是既有的目录归类，不是我新增的误标；我选择只修 skip 根因、不擅自搬动文件归属，避免改动他人用例结构。

新增测试自身曾出现 3 处**我自己的断言错误**（把 `current_context()` 只绑定可选字段误认为应有快照；把 `quote()` 不转义 unreserved 字符误认为泄漏；把 `%` 转义符误当保留字），已按「QA 自修」原则自行修正并复跑通过——这属于测试代码 bug，不路由给 Engineer。

### 4.2 临时脚本清理确认（主理人问项）

变异测试与 DDL 比对用的两个临时脚本 `scripts/_qa_mutate.py`、`scripts/_qa_ddl_diff.py` **已从工作区删除**（`git status --short` 显示 `D scripts/_qa_ddl_diff.py` / `D scripts/_qa_mutate.py`；`ls` 报 No such file or directory）。这两个文件曾被 `git add -A` 误纳入 `2b583c9` 提交，因此**版本库 HEAD 里仍存在，需把删除动作提交掉**——否则 CI 的 `ruff check .` 会继续报它们引入的 `F401 shutil imported but unused`。探针文件 `src/tagmind/domain/_qa_violation_probe.py`、`migrations/versions/_qa_phantom.py` 同样已删除；变异脚本对 3 个 `src/` 文件的写入已用 `git checkout HEAD --` 还原（仅 CRLF 换行噪音），`git diff` 为空，**源码未被污染**。

---

## 5. 静态审查结论（因无 PG / 无 Docker，只能静态核对）

### 5.1 `docker-compose.yml`

| 核对项 | 结论 | 证据 |
| --- | --- | --- |
| 五服务齐全 | **静态通过** | `api` / `worker` / `beat` / `postgres` / `redis` 均定义；`postgres` 用 `pgvector/pgvector:pg16`，api/worker/beat 复用 `tagmind:latest` + `TAGMIND_ROLE` 分派 |
| 与 `.env.example` 变量对齐 | **静态失败（Minor）** | 脚本比对：compose 用到但 `.env.example` 未声明的变量 = `['TAGMIND_REDIS_PORT']`；其余 46 个声明项未被 compose 使用属正常 |
| 硬编码密码 | **静态失败（Major）** | `POSTGRES_PASSWORD: ${TAGMIND_POSTGRES_PASSWORD:-tagmind}` 出现在第 18/62/97/124 行共 4 处，`POSTGRES_DB`/`POSTGRES_USER` 也硬编码 `tagmind`。文件头注释自称「凭据一律从环境读，不在文件里硬编码」，与实际矛盾 |
| 生产密码守卫 | **静态失败（Major）** | compose 把 `TAGMIND_ENV` 默认成 `development`（55/92/119 行），覆盖了 Dockerfile 的 `ENV TAGMIND_ENV=production`；又给密码兜底 `tagmind`，因此 T01 的「production 缺密码 → ConfigError」在 compose 路径下**永远不触发** |
| Redis 鉴权 | **静态失败（Major）** | compose 注入 `TAGMIND_REDIS_PASSWORD`，但全仓 `grep -rn redis_password src/` 只有 `settings.py:95` 的字段声明，**无任何消费点**；`_redis_url_with_db()` 只改路径段。设了密码等于没设 |
| healthcheck | **静态失败（Major）** | 只有 3 个 `healthcheck:`（第 23/39/76 行 = postgres/redis/api）；`worker`（84）与 `beat`（111）**没有 healthcheck**，T04「5 个服务全部健康」无法成立 |
| 依赖顺序 | 静态通过 | api/worker/beat 均 `depends_on` postgres + redis 且 `condition: service_healthy` |

### 5.2 `.github/workflows/ci.yml`（6 个 job）

| job | 静态结论 | 证据 / 风险 |
| --- | --- | --- |
| `lint` (27) | **已修复 → 会跑且转绿** | 修复前：`ruff check .` 有 1 个 F401、`format` 有 3 个文件待重排（D-01）。**主理人修复后**：lint 范围从 `src tests` 扩到 `.`，全仓执行 `ruff check --fix .` + `ruff format .` → `All checks passed!` / `100 files already formatted`。残留：两个 QA 临时脚本在工作区已删但 HEAD 里仍在，删除动作需提交（§4.2） |
| `typecheck` (49) | **会跑，但必红** | `mypy src/tagmind` 在干净 HEAD（含 `--no-incremental`）稳定报 `interaction_repo.py:143` 1 个错误 → 见 D-01 |
| `test` (68) | **部分假绿** | 单测跑两遍（89/93 行重复，浪费）；**第 96 行 `pytest tests/integration -q -m integration` 所在 job 既无 `services: postgres` 也无 `TAGMIND_TEST_DATABASE_URL`** → 39 条集成测试必然全 skip，job 恒绿但零验证 → 见 D-02 |
| `secret-scan` (98) | 会跑，本地模拟通过 | `git grep -nIE` 三种模式，排除 `.env.example`/`docs/**`/`tests/**`；本地实测 rc=0 无命中。局限：`git grep` 只搜已跟踪文件，未跟踪文件里的密钥扫不到 |
| `dependency-scan` (131) | 会跑，**无法验证结论** | `pip-audit --strict -r <(python -c ...)` 依赖外网 PyPI 漏洞库，`--strict` 遇任意已知 CVE 即失败；本机离线无法验证。进程替换 `<()` 在 GH Actions 默认 bash 下可用 |
| `migration-idempotent` (156) | **Blocker 已修复 → 会跑；但仍不完整** | **主理人已修复并复验**：`env:` 现同时给出 `TAGMIND_DATABASE_URL`（`postgresql+psycopg://...`）+ `TAGMIND_TEST_DATABASE_URL` + `TAGMIND_POSTGRES_PASSWORD`，空密码回退问题解除；`alembic upgrade head` 连跑两次 + `consistency_check.py`（该脚本的 `Any` 导入也已补齐，不再 `NameError`）。**但该 job 不执行 `pytest tests/integration`** → T02 的五条 DB 验收项仍无 CI 覆盖 → 见 D-02（待修） |

> **直接回答「`migration-idempotent` 在无 PG 时会不会假绿」**：该 job **有** `services: postgres`（`pgvector/pgvector:pg16` + health-cmd），所以不会因缺 PG 而假绿；但真正的假绿在 `test` job 的集成测试步骤（D-02）。修复前的 `migration-idempotent` 则是「必红而非假绿」：只设了 `TAGMIND_TEST_DATABASE_URL`，而 `migrations/env.py::_resolve_url()` 读的是 `TAGMIND_DATABASE_URL`，回退到 `Settings.resolved_database_url = postgresql+psycopg://tagmind:@localhost:5432/tagmind`（密码为空），与 PG 服务的 `tagmind` 密码不匹配 → 认证失败。该 Blocker 已由 `9e61209` 修复。

### 5.3 `.github/workflows/nightly.yml`

| 核对项 | 结论 | 证据 |
| --- | --- | --- |
| DB 连接串 | **静态失败（修复不彻底）** | 第 46 行仍只有 `TAGMIND_TEST_DATABASE_URL: postgresql://tagmind:tagmind@localhost:5432/tagmind`，**缺 `TAGMIND_DATABASE_URL` 与 `TAGMIND_POSTGRES_PASSWORD`**。我按 nightly 的环境模拟 `_resolve_url()`：`TAGMIND_DATABASE_URL env = None` → `alembic 实际会用 = postgresql+psycopg://tagmind:@localhost:5432/tagmind`（空密码）→ 第 64 行 `alembic -c alembic.ini upgrade head` 认证失败。`9e61209` 只修了 ci.yml，没修 nightly.yml → D-04 |
| 评测真实性 | **静态失败（Minor）** | 第 75/79 行 `python -m tagmind.cli eval` / `bench` 都是 `cli.py` 里 `typer.echo("...阶段 4 实现"); raise typer.Exit(1)` 的占位命令，却配了 `continue-on-error: true`；nightly 自称「产出 artifact 报告」，实际 `reports/` 不会生成，且第 102 行引用的 `docs/metrics-report.md` **不存在**。属「恒绿但无产出」 |
| 口径声明 | 静态通过 | 汇总文案明确写 `evidence_type: offline_simulation` 与「非线上真实流量」，符合 PRD §7.5 模板 A |

### 5.4 `Dockerfile`

| 核对项 | 结论 | 证据 |
| --- | --- | --- |
| 多阶段构建 | **静态通过** | `builder`（编译 wheels）→ `runtime`（只装 wheels），带 `build-essential git` 但不进运行时；`COPY src/ /app/src/` + `PYTHONPATH=/app/src`；`pip install /wheels/*.whl` |
| 非 root | **静态通过** | `groupadd/useradd --uid 1001 tagmind` + `chown -R tagmind:tagmind /app` + `USER tagmind` |
| HEALTHCHECK 路径 | **静态通过** | `CMD curl -fsS http://localhost:8000/healthz`；compose api healthcheck 同为 `/healthz`；`api/app.py:93` 注册 `@app.get("/healthz")` —— 三方一致 |
| 启动命令 | **静态通过（修复后）** | `entrypoint.sh` 按 `TAGMIND_ROLE` 分派 api/worker/beat；`create_app` 零参可调用（有 `--factory`）；`9e61209` 补了 `scheduler/tasks/__init__.py` 后实测 `celery worker` 与 `celery beat` 均能进入启动横幅 |
| 环境变量 | 静态瑕疵（并入 D-08） | `ENV TAGMIND_ENV=production` 被 compose 覆盖成 development；`TAGMIND_REDIS_PASSWORD` 无消费点 |

### 5.5 五个迁移脚本

| 核对项 | 结论 | 证据 |
| --- | --- | --- |
| `down_revision` 链 | **静态通过** | `0001(None) ← 0002 ← 0003 ← 0004 ← 0005`，5 个 revision、恰好 1 个 base、无分叉无环；我另写了测试 `test_migration_down_revision_chain_is_linear` 锁定 |
| `0001` 是否启用 `vector` | **静态通过（条件启用）** | 第 98–103 行：`pg_trgm`/`btree_gin` 无条件 `CREATE EXTENSION IF NOT EXISTS`；`vector` 仅在 `_vector_available()`（查 `pg_available_extensions`）为真时创建。设计文档明说「未装 pgvector 时降级为 `REAL[]`」，`0003` 用 `_has_vector_extension()` 决定 `VECTOR(1024)` + HNSW 还是 `REAL[]`。CI/compose 用 `pgvector/pgvector:pg16`，故生产路径会启用；降级路径是刻意设计，不算缺陷 |
| 16 张表集合 | **静态通过** | 自建 DDL 解析器（括号配平 + 引号感知）解析 5 个迁移，与 `Base.metadata.tables` 比对：迁移 16 张 / ORM 16 张，**集合完全一致**；`test_db_metadata.py` 亦独立验证 |
| 列名逐字段 | **静态通过（有解析器局限）** | 16 张表的列名集合与 ORM 一致；我解析器报出的 16 处 NOT NULL 差异全部是 `PRIMARY KEY` 列（`PRIMARY KEY` 隐式 NOT NULL，DDL 未写 `NOT NULL` 但语义等价），**非真实漂移**；`experiment.traffic_split` 的 JSON 默认值造成 1 处解析假阳性 |
| 约束 / 触发器 / 权限 | **静态通过** | `REVOKE ALL ON pii_mask_map FROM PUBLIC;`（0003:116）；`tg_review_record_immutable` BEFORE UPDATE OR DELETE + `fn_forbid_mutation()`（0004:79-82）；`exposure_log` `PARTITION BY RANGE (ts)` + `exposure_log_default` + 动态预建上月/本月/下月三个分区；`ux_exposure_idem` 唯一索引；`ux_label_def_version_code` / `ux_label_def_version_path` 具名唯一约束 |
| 约束命名一致性 | **静态失败（Minor）** | ORM 的 `ux_annotation_set_version` / `ux_experiment_name` / `ux_llm_call_audit_llm_call_id` 在迁移里写成行内 `UNIQUE`（PG 自动命名为 `*_key`）；`consistency_check.py:62/66/67` 按自动名校验，与迁移自洽但**与 ORM 不一致**，`alembic autogenerate` 会看到差异 |
| 跨月数据落分区 | **静态失败（Major）** | 集成测试写死 `2026-01-15` / `2026-06-15`，而迁移只预建「上月/本月/下月」（当前 2026-11 → 10/11/12 月）。两条数据都落 `exposure_log_default`，`assert len({tableoid}) == 2` 在真 PG 上**必然失败** → D-07 |

### 5.6 指标诚实性审查（ADR-0010 / PRD §7.5）

| 核对项 | 结论 | 证据 |
| --- | --- | --- |
| `MetricValue` 存在且 `evidence_type` 无默认值 | **已验证通过** | `domain/versioning.py:153-170` 为 `frozen=True` dataclass，字段 `evidence_type: EvidenceType` 无默认；实测 `MetricValue(name=..., value=..., sample_size=..., as_of=...)` → `TypeError: missing 1 required positional argument: 'evidence_type'`。`sample_size` / `as_of` 同样必填 |
| 是否声称「线上真实数据」/ 把仿真说成线上 | **静态通过** | 全仓 grep 仅出现「非线上真实流量」「无真实流量」「不可验证」「不得冒充线上」等否定式声明；`experiment.evidence_type` 的 DDL/ORM CHECK 只允许 `offline_simulation` / `online_gray`（0005:52-53），`online_real` 结构性不可达 |
| README 指标表述 | **静态失败（Major）** | ① 「## 真实输出」整段 CLI 回显是**伪造**的：`README.md:123` 写 `标签体系导入完成：version=v1，标签数=1490` + 逐层清单 + `约束校验：A1–A10 零违规`，而真实 `python -m tagmind.cli seed-labels --dry-run` 只输出一行 `标签体系校验通过：1490 个标签，版本 ld_v1`（版本名也不同）；② `README.md:131-133` 的 `277 passed, 36 skipped` / `71.08%` 与交付态实测 `274 passed, 42 skipped` / `70.10%` 不符，覆盖率 badge 也写死 71%；③ `README.md:154` 把「连续执行两次 `upgrade head` 无报错」写成已交付事实，**本机无 PG 从未验证过**——正是 PRD §7.5「不得把无法验证说成通过」的红线 → D-09 |
| 占位数据被当真实数据 | **静态通过** | 12 层种子实测 L1=20 / L2=71 / L3=206 / L4=593 / L5–L11=各 60 / L12=180，**合计 1490**（落在 1200–1800）；`test_label_names_are_real_chinese` / `test_every_label_has_description` 拦截占位符；演示数据明确标 `synthetic=true` |
| DB 层 `evidence_type` 默认值 | Minor | `experiment.evidence_type` DDL 有 `DEFAULT 'offline_simulation'`（0005:52），与领域层「无默认值」约定不一致（默认值本身是安全的保守值，不算诚信事故） |

### 5.7 静态审查逐项结论总表（主理人要求的「通过 / 失败 / 无法验证」三态）

| 对象 | 通过 | 失败 | 无法验证 | 2 个 Blocker 修复后状态 |
| --- | --- | --- | --- | --- |
| `docker-compose.yml` | 五服务齐全、`depends_on` 健康条件、变量回退写法 | 硬编码默认密码 4 处（D-05）、`TAGMIND_ENV` 降级绕过生产守卫、worker/beat 无 healthcheck（D-08）、`TAGMIND_REDIS_PORT` 未对齐（D-12）、`redis_password` 无消费点（D-06） | `docker compose up` 五服务健康、首次启动 ≤15 分钟 | **不适用**（Blocker 在 celery / CI 侧） |
| `.github/workflows/ci.yml` | 6 个 job 全部定义；secret-scan 本地模拟通过；lint 范围已扩到 `.` | `typecheck` 的 mypy 错误（D-01 待修）、`test` job 集成步骤恒假绿（D-02）、单元测试重复执行（D-20） | `dependency-scan` 的 `pip-audit --strict` 结论（需外网）；GH Actions 上 mypy 的 SQLAlchemy 版本解析 | **F-02 已修复**：`migration-idempotent` 的 `env:` 补齐 3 个变量，空密码回退解除；`9e61209` + 主理人复验通过 |
| `.github/workflows/nightly.yml` | 口径声明文案符合 PRD §7.5 模板 A | 仍缺 `TAGMIND_DATABASE_URL`（D-04，**F-02 的同步遗漏**）、eval/bench 占位恒绿（D-14） | 真实评测产物 | **未同步修复**，仍是待修项 |
| `Dockerfile` | 多阶段、非 root、`/healthz` 三方一致、role 分派 | `TAGMIND_ENV=production` 被 compose 覆盖（并入 D-08） | 镜像实际可运行性、构建耗时 | **F-01 已修复**：`scheduler/tasks/__init__.py` 落地后 worker/beat 不再崩溃 |
| 5 个迁移脚本 | `down_revision` 线性无环、16 表集合与列名集合与 ORM 一致、`0001` 条件启用 pgvector、REVOKE / 不可变触发器 / 分区 DEFAULT 齐备 | 约束命名与 ORM 漂移（D-13）、跨月测试写死月份（D-07） | 空库幂等、information_schema 逐项、跨月落分区、触发器拒绝、PUBLIC 权限、UNIQUE 生效 | **F-03 已修复**：`consistency_check.py` 补 `Any` 导入后 `--help` 正常；alembic env/revisions 加了 E402/I001 例外 |

> 诚实声明：上表「通过」列全部是**静态审查通过**，不等于动态验证通过；「无法验证」列各项因本机无 PG / 无 Docker **一律不标为通过**。

---

## 6. 发现的缺陷清单

### 6.0 本次验证期间「发现 → 已被队友修复 → 已回归确认」（不计入下方开放缺陷数）

| 编号 | 现象 | 修复 | 回归证据 |
| --- | --- | --- | --- |
| F-01 **Blocker** | `celery_app.py:31` `include=["tagmind.scheduler.tasks"]` 指向不存在的包，`celery worker` / `celery beat` 在 `init_worker()` 抛 `ModuleNotFoundError: No module named 'tagmind.scheduler.tasks'` | `9e61209` 新增 `src/tagmind/scheduler/tasks/__init__.py` | 干净 HEAD 实测 `celery worker` 打印启动横幅、`celery beat` 打印 `celery beat v5.7.0 ... is starting.`，不再崩溃；我新增 `test_celery_include_target_exists` 锁死回归 |
| F-02 **Blocker** | CI `migration-idempotent` 只设 `TAGMIND_TEST_DATABASE_URL`，alembic/env.py 读 `TAGMIND_DATABASE_URL`，回退到空密码 DSN 导致迁移认证失败 | `9e61209` 同时补齐 `TAGMIND_DATABASE_URL` / `TAGMIND_TEST_DATABASE_URL` / `TAGMIND_POSTGRES_PASSWORD`，并把 CI lint 范围扩到 `.` | 静态核对 `ci.yml:174-180` 已含三变量；`ruff check .` 在移除误提交脚本后为 `All checks passed!` |
| F-03 | `scripts/consistency_check.py:96` 用了未导入的 `Any`（F821） | `9e61209` 补 `from typing import Any` | `python scripts/consistency_check.py --help` 退出码 0 |

> 这三项是我在会话前半段独立发现并留证的；队友在 `9e61209` 的 commit message 里也注明「QA independent verification found two blockers」。**修复有效，但 F-02 只覆盖了 ci.yml，nightly.yml 未同步（D-04）**。

### 6.1 Blocker（3 项）

**D-01 · CI 的 `lint` 与 `typecheck` 两个 job 在干净 HEAD 上必然失败**

- **文件:行号**：`src/tagmind/db/repositories/interaction_repo.py:143`；`.github/workflows/ci.yml:66`（typecheck）、`:44`/`:47`（lint）；仓库中还残留 `scripts/_qa_mutate.py:10`
- **现象**：`mypy src/tagmind` → `Incompatible types in assignment (expression has type "ReturningInsert[int]", variable has type "Insert")`；`ruff check .` → `F401 shutil imported but unused`；`ruff format --check .` → `3 files would be reformatted`（`scripts/_qa_mutate.py`、`scripts/_qa_ddl_diff.py`、`tests/integration/test_label_seed_import.py:200`）
- **根因**：`stmt = pg_insert(...).values(...)` 推断为 `Insert`，再赋值给 `stmt.returning(...)`（`ReturningInsert[int]`），mypy 不允许同名变量换型；另外 QA 的两个临时变异探针脚本被 `git add -A` 误纳入 `2b583c9` 提交，其中一个有未使用 import，且两个都不符合格式；`test_label_seed_import.py` 在 HEAD 上未格式化（`9e61209` 声称「100 files formatted」，但 HEAD 里该文件仍未改）
- **复现**：`git archive HEAD | tar -x -C /tmp/qa_pristine && cd /tmp/qa_pristine && python -m mypy --no-incremental src/tagmind && python -m ruff check . && python -m ruff format --check .`
- **建议修法**：把 `stmt.returning(...)` 赋给新变量（如 `returning_stmt`）；从版本库删除 `scripts/_qa_mutate.py` / `scripts/_qa_ddl_diff.py`（我已在当前工作树删除，需提交）；对 `tests/integration/test_label_seed_import.py` 跑 `ruff format` 并提交；把 CI 的 `mypy` 与 `ruff` 结果作为合并前必跑门禁重新确认
- **归属**：`src/` 真实类型错误 + 配置 → **Engineer**
- **修复后状态（§10 更新）**：**ruff 部分已由主理人修复**（全仓 `ruff check --fix .` + `ruff format .` → `All checks passed!` / `100 files already formatted`，CI lint 范围已扩到 `.`）；但 **`mypy src/tagmind` 的 `interaction_repo.py:143` 类型错误未见修复记录**，主理人给出的复验四项里没有 mypy 输出。该项**仍待工程师确认/修复**，不能因 lint 变绿就整体关闭 D-01。另：两个临时脚本在工作区已删除，但 HEAD 里仍存在，删除动作需提交（见 §4.2）。

**D-02 · T02 的 5 条 DB 验收项在 CI 中从未执行（集成测试步骤恒假绿）**

- **文件:行号**：`.github/workflows/ci.yml:96`（`pytest tests/integration -q -m integration`）；`:68-96`（`test` job 无 `services:`、无 `TAGMIND_TEST_DATABASE_URL`）；`:156-199`（`migration-idempotent` job 有 PG 但不跑集成测试）
- **现象**：`test` job 的集成测试步骤在无 PG、无 `TAGMIND_TEST_DATABASE_URL` 时，`conftest.py:36` 的 `db_url` fixture 必定 `pytest.skip` → 39 条集成测试全 skip → 该步骤恒绿但零验证。而唯一有 PG 的 `migration-idempotent` job 只跑 `alembic upgrade head` ×2 + `consistency_check.py`，**没有** `pytest tests/integration`
- **影响**：架构 §15.2 T02 ② information_schema 逐项比对、③ 分区跨月、④ `review_record` 触发器、⑤ `pii_mask_map` 权限，以及 T03 ④ 唯一约束、⑥ 标签树 API↔DB 一致性，全部只存在于集成测试里，CI 从未真正跑过
- **复现**：本地 `pytest tests/integration -q -m integration` → `39 skipped`（无 PG 时）；读 ci.yml 确认 `test` job 无 `services:` 段
- **建议修法**：在 `migration-idempotent` job 的 `alembic upgrade head` 之后增加 `pytest tests/integration -q -m integration`（两者共用同一 `TAGMIND_TEST_DATABASE_URL`）；或者把 PG service 与两个环境变量移到 `test` job。同时把 `migration-idempotent` 的 `consistency_check.py` 与集成测试都设为**硬失败**
- **归属**：CI 配置 → **Engineer**

**D-03 · `BaseRepository` 通用方法硬编码 `self.model.id`，对 3 张无 `id` 列的表 + 1 张复合主键表确定性崩溃**（主理人已确认**本项尚未修复**，本节为派工依据）

#### D-03.1 精确行号清单（可直接据此派工）

| # | 文件:行号 | 代码 | 触发条件 | 失败现象 |
| --- | --- | --- | --- | --- |
| 1 | `src/tagmind/db/repositories/base.py:208` | `stmt.order_by(order_by if order_by is not None else self.model.id)` | 对 `UserProfile` / `ContentVector` / `ExperimentAssignment` 调 `list()` 且不传 `order_by`（默认路径） | `AttributeError: type object 'Xxx' has no attribute 'id'` |
| 2 | `src/tagmind/db/repositories/base.py:396` | `select(version_column).where(self.model.id == entity_id)` | 对无 `id` 列的表调 `optimistic_update()` 且不传 `expected_version` | `AttributeError: ... has no attribute 'id'` |
| 3 | `src/tagmind/db/repositories/base.py:406` | `.where(self.model.id == entity_id)` | 对无 `id` 列的表调 `optimistic_update()`（任何路径） | `AttributeError: ... has no attribute 'id'` |
| 4 | `src/tagmind/db/repositories/base.py:440` | `delete(model).where(model.id.in_(entity_ids))` | 对无 `id` 列的表调 `bulk_delete(model, session, ids)` | `AttributeError: ... has no attribute 'id'` |
| 5 | `src/tagmind/db/repositories/base.py:245` | `if entity_id is not None and entity_id <= 0:` | `upsert()` 且主键为字符串（`UserProfile.user_id`） | `TypeError: '<=' not supported between instances of 'str' and 'int'` |
| 6 | `src/tagmind/db/repositories/base.py:255` | `row = self.model(id=entity_id, **values)` | `upsert()` 且主键列名不是 `id`（`content_id` / `user_id`） | `TypeError: 'id' is an invalid keyword argument for Xxx()` |
| 7 | `src/tagmind/db/repositories/base.py:404-411` | `if len(primary_key) != 1: raise DatabaseError("仓储基类只支持单列主键")` | 对复合主键表调 `upsert()` / `insert_ignore()`（`ExposureLog` PK=`(id,ts)`、`ExperimentAssignment` PK=`(user_id,experiment_id)`） | `DatabaseError: 仓储基类只支持单列主键` |

> **主理人结论的一处事实修正**：主理人称「受影响模型 `UserProfile`(PK=user_id)、`ContentVector`(PK=content_id)、`ExperimentAssignment`(复合 PK)、`ExposureLog`(PK=id+ts)，**这些模型都没有 `.id` 属性**」。实测 `Base.metadata.tables` 后需修正——**`ExposureLog` 是有 `id` 列的**（`exposure_log` 的 PK 是 `(id, ts)`，列集合里 `id` 存在，`has_id=True`），因此它**不会**触发 #1–#4 的 AttributeError；它触发的是 #7（`_primary_key_name()` 因主键列数为 2 而抛 `DatabaseError`）。真正「没有 `.id` 属性」的是 3 张表：`UserProfile`、`ContentVector`、`ExperimentAssignment`。另主理人列的 4 处 `self.model.id`（208/396/406/440）经我复核**全部属实**，此外还有 245/255/404-411 三处同源缺陷需一并修。

#### D-03.2 受影响模型与主键（实测 `Base.metadata.tables`）

| 表 / ORM 模型 | 主键列 | 有 `id` 列？ | 命中缺陷 |
| --- | --- | --- | --- |
| `user_profile` / `UserProfile` | `user_id` (str) | 否 | #1 #2 #3 #4 #5 #6 |
| `content_vector` / `ContentVector` | `content_id` | 否 | #1 #2 #3 #4 #6 |
| `experiment_assignment` | `user_id, experiment_id`（复合） | 否 | #1 #2 #3 #4 #6 #7 |
| `exposure_log` / `ExposureLog` | `id, ts`（复合） | **是** | #7（**不**触发 AttributeError） |

#### D-03.3 最小复现片段（干净 HEAD 实测输出）

```python
# 环境：PYTHONPATH=src；不需要 PostgreSQL，纯 ORM 元数据 + 假 Session
import sys; sys.path.insert(0, "src")
import tagmind.db.models  # 触发 ORM 注册
from tagmind.db.repositories.interaction_repo import InteractionRepository
from tagmind.db.repositories.vector_repo import VectorRepository
from tagmind.domain.profile import UserProfile
from datetime import UTC, datetime

class F:                       # 最小假 Session
    def __init__(self): self.added = []
    def execute(self, *a, **k): raise AssertionError("不应走到 DB")
    def get(self, *a): return None
    def add(self, e): self.added.append(e)
    def flush(self): pass

for cls in (InteractionRepository, VectorRepository):
    try: cls(F()).list()
    except Exception as e: print(cls.__name__, "->", type(e).__name__, e)
try: InteractionRepository(F()).upsert(UserProfile(user_id="u-1", updated_at=datetime.now(UTC)))
except Exception as e: print("upsert ->", type(e).__name__, e)
```

实测输出：
```
InteractionRepository.list() -> AttributeError: type object 'UserProfile' has no attribute 'id'
VectorRepository.list()      -> AttributeError: type object 'ContentVector' has no attribute 'id'
InteractionRepository.upsert() -> TypeError: '<=' not supported between instances of 'str' and 'int'
```

#### D-03.4 根因与建议修法

- **根因**：基类把主键列名写死为 `id`（4 处），并用「数值 `<= 0`」表示「尚未落库」，还假设主键一定是单列。领域层里 `UserProfile.user_id` 是字符串、`ContentVector` 用 `content_id`、分区表 `ExposureLog` 按 §5.6 必须是复合 PK `(id, ts)`——这三条都与基类假设冲突。
- **建议修法**：
  1. 新增 `self._pk_columns() -> tuple[Column, ...]`，从 `self.model.__table__.primary_key.columns` 派生；所有 `self.model.id` / `model.id` 改为 `getattr(self.model, pk_name)`，复合主键时构造 `and_(*(col == v for col, v in zip(pk_cols, values)))`。
  2. 「未落库」判据改为 `entity_id is None`（或按列类型的 Python 默认值比较：`str` 用 `== ""`、`int` 用 `== 0`），**不要**对字符串做 `<= 0`。
  3. `upsert` 构造行时用 `self.model(**{pk_name: entity_id, **values})`，不要写死 `id=`。
  4. `_primary_key_name()` 明确支持复合主键（返回列名元组），或在 `InteractionRepository` / `VectorRepository` / 曝光仓储里覆写；`insert_ignore` 的 SAVEPOINT 语义保持不变。
  5. 补一组参数化单测：对 `UserProfile` / `ContentVector` / `ExperimentAssignment` / `ExposureLog` 分别调 `list()` / `upsert()` / `optimistic_update()` / `insert_ignore()`，断言不抛 `AttributeError` / `TypeError` / `DatabaseError`。
- **优先级**：阶段 1B 的画像写入、向量写入、曝光回流都会直接踩到 #1/#2/#5，建议与阶段 1 开工同步修。
- **归属**：`src/` 真实 bug → **Engineer**

### 6.2 Major（8 项）

**D-04 · `nightly.yml` 未同步 F-02 修复，迁移步骤必失败**
`.github/workflows/nightly.yml:46` 仅 `TAGMIND_TEST_DATABASE_URL`；第 64 行 `alembic -c alembic.ini upgrade head` 回退到 `postgresql+psycopg://tagmind:@localhost:5432/tagmind`（空密码）→ 认证失败。模拟输出见 §5.3。**建议**：与 ci.yml 一样补 `TAGMIND_DATABASE_URL` + `TAGMIND_POSTGRES_PASSWORD`。

**D-05 · compose 硬编码默认密码并绕过生产密码守卫**
`docker-compose.yml:18/62/97/124` 的 `${TAGMIND_POSTGRES_PASSWORD:-tagmind}` 共 4 处；`:55/92/119` 把 `TAGMIND_ENV` 默认成 `development`，覆盖 Dockerfile 的 `production`。因此 T01 的「production 缺密码 → ConfigError」在 compose 路径永不触发，且任何人不配 `.env` 就能用弱口令 `tagmind` 起库。**建议**：去掉密码的 `:-tagmind` 兜底（改为 `${TAGMIND_POSTGRES_PASSWORD:?必须设置}`），并明确区分 development / production 的 env 注入。

**D-06 · `TAGMIND_REDIS_PASSWORD` 注入但从未被消费**
compose `:64/99/126` 注入该变量，`src/` 中 `grep -rn redis_password` 只有 `settings.py:95` 的字段声明，`_redis_url_with_db()`（`settings.py:37-54`）只改路径段。运维以为开了 Redis 鉴权，实际没有。**建议**：在派生 broker/cache URL 时把密码写入 netloc，或删除该字段与 compose 注入避免误导。

**D-07 · 跨月分区集成测试写死月份，真 PG 上必然失败**
`tests/integration/test_migrations_idempotent.py:313` 写死 `("2026-01-15 10:00:00+00", "2026-06-15 10:00:00+00")`，而 `0003_interaction_and_vector.py:165` 只预建 offset `(-1,0,1)` 三个月分区（当前 2026-11 → 10/11/12 月）。两条数据都进 `exposure_log_default`，`:330` 的 `len({tableoid}) == 2` 必红。**建议**：改为按 `datetime.now(UTC)` 动态构造「上月 / 本月」两个时间戳，并断言落在两个**具名**分区；或在测试里显式创建对应月份的分区。

**D-08 · worker / beat 无 healthcheck，T04「5 服务全部健康」不成立**
`docker-compose.yml` 只有 3 个 `healthcheck:`（postgres/redis/api），`worker`(`:84`) 与 `beat`(`:111`) 缺。`docker compose up` 后这两服务永远只是 `running` 而非 `healthy`。**建议**：给 worker 加 `celery ... inspect ping`、给 beat 加进程/文件探针，并让 api 的 `depends_on` 覆盖它们。

**D-09 · README「真实输出」整段与真实命令不符（ADR-0010 诚实口径）**
`README.md:119-133` 的 CLI 回显是编造的（真实 `seed-labels --dry-run` 只输出 `标签体系校验通过：1490 个标签，版本 ld_v1`）；`:131-133` 的 `277 passed, 36 skipped` / `71.08%` 与交付态 `274 passed, 42 skipped` / `70.10%` 不符；`:8` 覆盖率 badge 写死 71%；`:154` 把从未在本机验证过的「连续两次 `upgrade head` 无报错」写成已交付事实。PRD §7.5 声明 2/4 与 ADR-0010 要求「区分已验证与无法验证」。**建议**：把该节改名为「示例输出（示意）」或改为由 CI 自动注入的真实输出；对未验证项显式标注「需 PG 环境验证」。

**D-10 · T03 验收项 ⑥「标签树 API 返回结构与 DB 一致」无实现**
架构 §15.2 T03 明确要求该项（P4-API-004 抽样 20 节点比对），但 `src/tagmind/api/app.py` 只注册 `/healthz`(`:93`) 与 `/readyz`(`:102`)，`api/routers/` 只有 `__init__.py`，全仓 grep 无 `/labels`、`label_tree`、`to_tree_payload` 的 API 端点。`tests/unit/test_taxonomy_tree.py::test_to_tree_payload` 只测了领域层序列化，**不能替代「与 DB 一致」的比对**。**建议**：明确该验收项是延后到阶段 4（T16）还是必须在阶段 0 补一个只读的 `/labels/tree` 端点；当前状态必须标注为「未交付」，不得勾选 T03 通过。

**D-11 · 当前工作树门禁已红（含他人在途阶段 1 文件）**
`src/tagmind/ingestion/pii.py:172`(SIM110)、`ingestion/rules.py:26`(UP035) 触发 `ruff check` 2 错；`rules.py:331` 未格式化；新增约 244 条未测语句把覆盖率从 73.12% 拉到 **67.21% < 70%** → `pytest` 覆盖率门禁失败。**建议**：阶段 1 合入前补测试 / 修 lint，或明确把在途文件隔离在分支；不要让主分支处于红态。

> 补充：原样 HEAD 单测覆盖率 **70.10%**，仅高出门槛 **0.10 个百分点**（CI `test` job 跑的是 `pytest tests/unit`），任何新增未测源码都会让 CI 转红，属高脆弱性配置（并入 D-11 管理）。

### 6.3 Minor（10 项）

| 编号 | 文件:行号 | 现象与建议 |
| --- | --- | --- |
| D-12 | `docker-compose.yml:36` | `TAGMIND_REDIS_PORT` 未在 `.env.example` 声明，T01「逐项对齐」只校验了 Settings ⊇ env，未校验 compose ⊆ env。建议补声明或改回固定端口 |
| D-13 | `0004:89` / `0005:44,78` | 行内 `UNIQUE` 生成 PG 自动名（`*_key`），与 ORM `ux_*` 命名漂移；`consistency_check.py:62/66/67` 按自动名校验，与 ORM 不一致，autogenerate 会看到差异。建议统一具名约束 |
| D-14 | `nightly.yml:75-81, 102` | `eval` / `bench` 是 `cli.py:147/154` 的 `Exit(1)` 占位命令且 `continue-on-error: true`；引用的 `docs/metrics-report.md` 不存在。恒绿但无产出，建议改为 `xfail` 显式标注或补实现 |
| D-15 | `tests/integration/test_repositories.py:72`（原） | 3 个纯逻辑测试被 `autouse` DB fixture 误 skip —— **QA 已自修**（见 §3.3 / §4） |
| D-16 | `scripts/_qa_mutate.py` / `_qa_ddl_diff.py` | QA 临时探针脚本被误纳入 `2b583c9` 提交；工作树已删除，需提交删除（并入 D-01 一并处理） |
| D-17 | `tests/unit/test_db_metadata.py:122-124` | 用例名 `..._equal_orm_tables` 但实际比的是硬编码 `EXPECTED_TABLES` 常量，命名误导。空集假通过风险已被实测否证（§3.4），建议改名或改成真的与 `Base.metadata.tables` 比较 |
| D-18 | `tests/unit/test_domain_purity.py:78-93` | AST 检查只覆盖 `ast.Import` / `ast.ImportFrom`，`importlib.import_module("fastapi")` 等动态导入拦不住。建议补 `__import__`/`import_module` 字符串常量扫描 |
| D-19 | `tests/unit/test_api_and_scheduler.py:40`、`test_repositories_unit.py:232` | `assert X is not None` 弱断言（类型上就非 None）。建议改为具体值/类型断言 |
| D-20 | `ci.yml:89, 93` | `pytest tests/unit` 重复执行两遍（一次带 `--cov-report=xml`、一次带 `--cov-fail-under=70`），浪费 CI 时间。建议合并为一步 |
| D-21 | `0005:52`、`domain/versioning.py:165` | `experiment.evidence_type` DDL 有 `DEFAULT 'offline_simulation'`，与领域层 `MetricValue` 的「无默认值」约定不一致（保守默认值，不构成诚信事故，但口径需统一） |

### 6.4 缺陷统计

| 严重度 | 数量 | 编号 | 修复后状态（§10） |
| --- | --- | --- | --- |
| **Blocker** | **3** | D-01, D-02, D-03 | D-01 的 ruff 部分已修、**mypy 部分待修**；D-02 / D-03 **待工程师修** |
| **Major** | **8** | D-04 ~ D-11 | D-04（nightly DSN）、D-05（compose 密码）、D-06、D-07、D-08、D-09、D-10、D-11 均**待修** |
| **Minor** | **10** | D-12 ~ D-21 | D-15（误 skip）、D-16（临时脚本）已由 QA 自修；其余待修 |
| **合计** | **21** | 开放缺陷 21 项 | **已修复**：F-01/F-02/F-03（2 Blocker + 1 Minor）+ D-01 的 lint 部分；**待工程师修**：D-01 mypy、D-02、D-03、D-04~D-14、D-17~D-21 |

> 说明：F-01（celery `include`）、F-02（CI DSN）、F-03（`consistency_check.py` 的 `Any`）在验证期间由队友提交 `9e61209` 修复，主理人又独立核实并复验通过，均**不计入开放数**；详见 §6.0 与 §10。主理人消息里用的编号 `D-B1` = 本报告 `F-01`，`D-B2` = 本报告 `F-02`，「D-05/F821」= 本报告 `F-03` + CI lint 范围扩到 `.`（对应本报告 D-01 的 ruff 部分）。

---

## 7. 未能验证的部分（明确区分三类）

| 分类 | 项 | 替代手段 | 可信度 |
| --- | --- | --- | --- |
| **已验证通过** | 四条门禁命令（A 组，除 mypy）、变异测试 15 例、AST 拦截探针、db_metadata 空集/幽灵表探针、36→39 skip 归因、1490 标签总量与逐层分布、12 层无环/无孤儿/路径/祖先/配额、迁移链线性、16 表集合一致、compose 五服务定义、Dockerfile 多阶段/非 root/HEALTHCHECK 一致、`MetricValue` 无默认值、3 个既有修复的彻底性 | 直接执行 + 静态解析 + 变异测试 | 高 |
| **已验证失败** | mypy 类型错误、CI lint 残留、CI 集成测试假绿、`BaseRepository` 非 id 主键崩溃、nightly DSN、compose 密码与 healthcheck 缺口、跨月测试写死月份、README 伪造输出、当前工作树覆盖率不达标 | 直接执行 / 模拟环境变量 / 静态比对 | 高 |
| **因环境限制无法验证** | ① 空库 `alembic upgrade head` 成功且二次幂等；② `information_schema` 与 §5 DDL 逐项（名/型/可空/默认）零差异；③ `exposure_log` 跨月数据落正确分区；④ `review_record` UPDATE/DELETE 被触发器拒绝；⑤ `pii_mask_map` 对 PUBLIC 无权限；⑥ `UNIQUE(version,code)` / `UNIQUE(version,path)` 在真实 DB 生效；⑦ 标签树 API 返回结构与 DB 一致（且端点根本不存在）；⑧ 祖先闭包 1800 节点 < 50 ms（现有单测在 1490 节点上测，为代理指标）；⑨ `docker compose up` 五服务健康、首次启动 ≤ 15 分钟；⑩ `pip-audit --strict` 的真实结论（需外网）、GH Actions 上 mypy 的 SQLAlchemy 版本解析结果；⑪ `scripts/consistency_check.py --after-migration head` 退出码 0 | 逐行读 5 个迁移脚本的 DDL、读 compose/CI/Dockerfile、用自建 DDL 解析器做「迁移 SQL ↔ ORM metadata」静态逐字段比对（16 张表列名集合一致）、用假 Session 覆盖仓储纯逻辑、用环境变量模拟复现 DSN 回退 | **静态结论中高；动态结论为零 —— 一律不标为「通过」** |

> 特别声明：本报告**没有**把上述 ①–⑪ 中任何一项写成「验证通过」。README 与部分注释里「幂等（连续执行两次无报错）」等表述属于**未经本机验证的断言**，已列为 D-09。

---

## 8. 对阶段 1 的建议（开工前必须修掉）

**P0 · 阻断开工（必须修）**

1. **D-01**：修 `interaction_repo.py:143` 的 mypy 赋值类型错误；提交删除 `scripts/_qa_mutate.py` / `_qa_ddl_diff.py`；格式化并提交 `tests/integration/test_label_seed_import.py`；确认 CI 的 `lint` + `typecheck` 两个 job 在干净 HEAD 上真的绿。
2. **D-02**：把 `pytest tests/integration -q -m integration` 挪到有 PG 的 `migration-idempotent` job（或给 `test` job 配 PG service + 两个 DB 环境变量），让 T02 的五条验收项在 CI 里真跑、真失败；否则阶段 1 的所有 DB 改动都处于「无回归网」状态。
3. **D-03**：重构 `BaseRepository` 主键处理（派生主键列名 + 兼容字符串/复合主键）。阶段 1 的清洗入库、画像写入、向量写入都会用到 `InteractionRepository` / `VectorRepository`，不修会直接撞 `AttributeError` / `TypeError`。
4. **D-04 / D-05 / D-06**：修 nightly DSN、去掉 compose 密码兜底、让 Redis 密码真正生效（或删除该字段）。这三处都是安全/正确性配置，拖到阶段 1 会污染所有后续配置基线。
5. **D-11**：把当前工作树的阶段 1 在途文件补测试 / 修 lint，让覆盖率回到 ≥70%，主分支不得处于红态。

**P1 · 建议同期修**

6. **D-07**：把跨月分区测试改为动态月份，否则一旦 CI 接上 PG 立刻红。
7. **D-08 / D-12 / D-13**：补 worker/beat healthcheck；对齐 compose 变量与 `.env.example`；统一约束命名。
8. **D-09 / D-10 / D-14**：README 改为真实/示意标注；明确 T03 ⑥ 的交付阶段（阶段 0 必须标「未交付」）；nightly 占位步骤显式标注为「未实现」而非 `continue-on-error` 恒绿。
9. 把覆盖率门槛从「贴着 70%」提到有缓冲（例如给阶段 1 新增代码同步补测试后再上调），避免 D-11 反复触发。

**流程建议**：阶段 0 的验收签字应附带本报告 §7 的「未验证清单」，在首个具备 PG + Docker 的环境（CI `migration-idempotent` job 修好后）跑一次完整集成套件，作为阶段 0 的**补充验收**，而不是以本机 349 passed 绿灯代替。

---

## 9. 智能路由判定

> **路由判定：Engineer（修复后的真实结论）**

**判定更新说明**：本判定已按主理人 §10 的四组修复重新计算。**两个启动级 Blocker 已修复，不再挂着**；但仍有 `src/` 真实 bug 与 CI/容器配置缺陷待处理，故仍判 Engineer，不是 NoOne；测试代码本身的 bug 我已自修，不路由给 Engineer。

### 9.1 【已修复】组（主理人核实并落地，QA 已回归确认，见 §10）

| 编号对照 | 缺陷 | 修复 | 复验结论 |
| --- | --- | --- | --- |
| 主理人 `D-B1` = 本报告 `F-01` | celery `include=["tagmind.scheduler.tasks"]` 指向不存在的包，worker/beat 启动即 `ModuleNotFoundError` | 新建 `src/tagmind/scheduler/tasks/__init__.py` | `celery_app.loader.import_default_modules()` → `OK`；实测 worker/beat 打印启动横幅 |
| 主理人 `D-B2` = 本报告 `F-02` | `ci.yml` migration job 只设 `TAGMIND_TEST_DATABASE_URL`，`migrations/env.py:44` 读 `TAGMIND_DATABASE_URL`，回退到空密码 DSN 必然失败 | migration job `env:` 同时给出 `TAGMIND_DATABASE_URL` + `TAGMIND_TEST_DATABASE_URL` + `TAGMIND_POSTGRES_PASSWORD` | 静态核对已补齐；主理人复验通过 |
| 主理人「D-05/F821」= 本报告 `F-03` + D-01 的 ruff 部分 | `scripts/consistency_check.py:84,96` 用 `Any` 未导入，调用即 `NameError`；CI lint 只覆盖 `src tests` 造成盲区 | 补 `from typing import Any`；`pyproject.toml` 给 `migrations/env.py` 加 `E402`、`migrations/versions/**` 加 `I001`；全仓 `ruff check --fix .` + `ruff format .`；CI lint 范围扩到 `ruff check .` / `ruff format --check .` | `python scripts/consistency_check.py --help` 正常；`ruff check .` → `All checks passed!`；`ruff format --check .` → `100 files already formatted` |
| 本报告 `D-15`（QA 自修） | 3 个纯逻辑测试被 `autouse` DB fixture 误 skip | 改 `clean_tables` 按 `request.fixturenames` 惰性解析 | `3 passed, 15 skipped`；全量 `349 passed, 39 skipped` |
| 本报告 `D-16`（QA 自修） | 2 个临时变异脚本被误纳入提交，含 F401 | 已从工作区删除 | `ls` 报 No such file；**HEAD 里仍在，删除动作需提交** |

### 9.2 【待工程师修】组（本轮未动，派工依据）

| 优先 | 缺陷 | 文件:行号 | 失败现象 |
| --- | --- | --- | --- |
| **P0** | **D-03** `BaseRepository` 硬编码主键（精确清单见 §6.1 D-03.1，主理人已确认尚未修） | `base.py:208,396,406,440`（4 处 `self.model.id`/`model.id`）、`:245,255`（str 主键 / `id=` 关键字）、`:404-411`（复合主键） | `InteractionRepository.list()` / `VectorRepository.list()` → `AttributeError: ... has no attribute 'id'`；`upsert(UserProfile)` → `TypeError: '<=' not supported between str and int`；复合主键表 → `DatabaseError: 只支持单列主键` |
| **P0** | D-01 的 **mypy 部分**（ruff 部分已修，勿整体关闭本条） | `src/tagmind/db/repositories/interaction_repo.py:143` | `mypy src/tagmind` → `Incompatible types in assignment (ReturningInsert[int] → Insert)`；主理人复验四项未含 mypy 输出，需确认 |
| **P0** | D-02 集成测试恒假绿 | `.github/workflows/ci.yml:96`（及 `:68`、`:156-199`） | 39 条集成测试恒 skip，T02 五项 DB 验收 CI 零覆盖 |
| **P0** | D-04 nightly 未同步 F-02 | `.github/workflows/nightly.yml:46,64` | `alembic upgrade head` 仍用空密码 DSN → 认证失败 |
| **P0** | D-05 / D-06 / D-08 compose 安全与健康检查 | `docker-compose.yml:18,55,62,92,97,119,124,84,111` + `settings.py:37-54` | 硬编码 `tagmind` 密码、`TAGMIND_ENV` 降级绕过生产守卫、`redis_password` 无消费点、worker/beat 无 healthcheck |
| **P1** | D-07 跨月分区测试写死月份 | `tests/integration/test_migrations_idempotent.py:313` | 真 PG 上 `len({tableoid})==2` 必失败 |
| **P1** | D-09 / D-10 / D-14 诚实口径 | `README.md:119-133,154`；`nightly.yml:75-81` | 「真实输出」伪造、未验证写成已验证；T03 ⑥ 端点不存在；nightly 占位恒绿 |
| **P1** | D-11 当前工作树红态（含他人在途阶段 1 文件） | `src/tagmind/ingestion/pii.py:172`、`rules.py:26,331` | 2 个 ruff 错误 + 覆盖率 67.21% < 70% |
| **P1** | D-12 / D-13 / D-17~D-21 其余 Minor | 见 §6.3 | 变量对齐、约束命名、AST 动态导入、弱断言、CI 重复执行等 |

**不是 NoOne 的理由**：开放缺陷 21 项（3 Blocker / 8 Major / 10 Minor），其中 D-03 是阶段 1B 画像/向量写入路径必然踩到的 `src/` 崩溃，D-02 使整个 T02 验收无回归网。

---

## 10. 主理人修复后复验（本轮新增）

> 本节记录主理人独立核实我 TL;DR 里 2 个 Blocker 后落地的修复与复验输出。**以下复验命令/输出由主理人提供，我未重跑**；QA 侧的独立佐证（变异测试、`git archive` 快照、celery 启动实测）另在 §3 / §6.0 记录。编号对照见 §6.4 注。

### 10.1 四组修复与复验逐项记录

| # | 缺陷（主理人编号 → 本报告编号） | 根因确认 | 修复内容 | 复验命令 | 复验输出 |
| --- | --- | --- | --- | --- | --- |
| 1 | **D-B1 → F-01** celery `include` 指向不存在的模块 | `src/tagmind/scheduler/celery_app.py:31` 写 `include=["tagmind.scheduler.tasks"]`，但 `src/tagmind/scheduler/tasks/` 目录**根本不存在**；worker/beat 在 `init_worker()` → `import_default_modules()` 抛 `ModuleNotFoundError` | 创建 `src/tagmind/scheduler/tasks/__init__.py`（含说明：本包由 celery include 加载，阶段 1B 在此实现打标任务） | `PYTHONPATH=src python -c "from tagmind.scheduler.celery_app import celery_app; celery_app.loader.import_default_modules()"` | `OK: include 模块全部可导入` |
| 2 | **D-B2 → F-02** CI migration-idempotent 连接串变量名不匹配 | `.github/workflows/ci.yml` 的 migration job 只设 `TAGMIND_TEST_DATABASE_URL`，而 `migrations/env.py:44` 读的是 `TAGMIND_DATABASE_URL`，回退到 `Settings` 派生值（密码为空）必然认证失败 | migration job 的 `env:` 同时给出 `TAGMIND_DATABASE_URL`（`postgresql+psycopg://...`）、`TAGMIND_TEST_DATABASE_URL`、`TAGMIND_POSTGRES_PASSWORD`；同时把 `lint` job 范围从 `ruff check src tests` 扩到 **`ruff check .`**（`format --check` 同理），堵住门禁盲区 | 静态核对 ci.yml `env:` 段 + 范围行 | 三变量齐全、lint 范围为 `.`；主理人复验通过 |
| 3 | **D-05/F821 → F-03** `scripts/consistency_check.py` 真实运行错误 | 第 84、96 行用了 `Any` 但文件没导入 → 脚本一调用即 `NameError`（QA 侧也独立发现 ruff `F821 Undefined name Any`） | 补 `from typing import Any`；另在 `pyproject.toml` 的 per-file-ignores 给 `migrations/env.py` 加 `E402`、给 `migrations/versions/**` 加 `I001`（alembic 的 sys.path 注入与 revision 链是合理例外） | `python scripts/consistency_check.py --help` | 正常输出用法（退出码 0），不再 `NameError` |
| 4 | 全仓 lint 门禁 | 验证期间 `ruff check .` 曾有 17~18 个错误、`ruff format --check .` 有 9 个文件待重排；CI 只查 `src tests` 造成盲区 | 全仓执行 `ruff check --fix .` + `ruff format .` | `ruff check .` / `ruff format --check .` | `All checks passed!` / `100 files already formatted` |

### 10.2 修复后门禁复验（主理人提供）

```
277 passed, 39 skipped
覆盖率 70.86%（门槛 70%）
```

QA 侧补充读数（含我新增的 72 个测试，口径不同但自洽）：

```
$ python -m pytest tests -q                      # 干净交付态 + QA 新增测试
349 passed, 39 skipped, 覆盖率 73.12%

$ python -m pytest tests/unit -q --no-cov        # 主理人独立核实
346 passed

$ python -m pytest tests/unit/test_qa_core_semantics.py \
         tests/unit/test_qa_repository_and_static.py -q --no-cov
72 passed in 0.84s   # 我新增的两个文件，0 skipped
```

> 口径说明：三者不矛盾，差异来自「测试范围」与「是否计入我新增的 72 个用例」。主理人 `277 passed / 39 skipped` 是修复后**未计**我 72 个新增用例的全量读数；`346 passed` 是 `tests/unit` 范围（274 个既有 unit 用例 + 72 个新增）；`349 passed` 是全量（346 个 unit 用例 + 3 个恢复执行的纯逻辑用例，它们位于 `tests/integration/` 目录下）。覆盖率 `70.86%`（主理人）与 `73.12%`（QA 含新增测试）同理——新增测试会提高覆盖率，属预期。

### 10.3 QA 独立佐证（我自己的实测，非引述）

- **F-01 修复前**：`PYTHONPATH=src python -m celery -A tagmind.scheduler.celery_app:celery_app worker` → traceback 末尾 `ModuleNotFoundError: No module named 'tagmind.scheduler.tasks'`；`beat` 同样崩溃。**修复后**（干净 HEAD 快照）：worker 打印 `celery@... v5.7.0` 启动横幅、beat 打印 `celery beat v5.7.0 (collider) is starting.`。我另加 `tests/unit/test_qa_repository_and_static.py::test_celery_include_target_exists` 锁死回归。
- **F-02 修复前**：按 CI env 模拟 `_resolve_url()` 得到 `postgresql+psycopg://tagmind:@localhost:5432/tagmind`（空密码），与 PG 服务 `tagmind` 密码不匹配 → 认证失败。修复后静态核对 ci.yml 三变量齐全。
- **F-03 修复前**：`python -m ruff check .` → `6 F821 undefined-name`，其中 `scripts/consistency_check.py:96` 两处 `Any` 未定义。修复后主理人给出 `All checks passed!`。

### 10.4 修复后仍需处理的事项（不要让读者以为 2 个 Blocker 还挂着，也不要以为全绿）

1. **两个启动级 Blocker 已关闭**（F-01 / F-02），`F-03` 与全仓 lint 门禁也已修复 → 阶段 0 的「能启动 / CI 不因 lint 与 DSN 必红」问题已解决。
2. **但 `mypy src/tagmind` 的 `interaction_repo.py:143` 类型错误在主理人的四组修复与复验输出里均未出现**，我按干净 HEAD 实测仍是 1 个错误（D-01 的 mypy 部分）→ 需工程师确认是否已修，未修则 `typecheck` job 仍红。
3. **D-02 未修**：CI 的集成测试步骤仍无 PG service / 无 `TAGMIND_TEST_DATABASE_URL`，39 条恒 skip，T02 五项 DB 验收零覆盖。
4. **D-03 未修**：`BaseRepository` 的 7 处主键硬编码（精确清单 §6.1 D-03.1），主理人已明确「这部分我还没修」，是本节之后最需要派工的一项。
5. **D-04 未修**：`nightly.yml` 未同步 F-02 的连接串修复，nightly 迁移仍会用空密码 DSN。
6. **两个临时脚本在工作区已删除、但 HEAD 里仍在**：删除动作需提交，否则 CI 的 `ruff check .` 仍会报 `F401`（§4.2）。
7. **无 PG / 无 Docker 的 11 项动态验收仍未验证**（§7），修复的是配置与代码，**不等于** T02 的分区/触发器/权限验收通过。

---

## 附：本次验证留下的可复核证据

- 变异测试：15 个反向变异 → 6 存活 → 新增测试后 6 个全部被杀死（脚本临时放在 `scripts/_qa_mutate.py`，**已从工作树删除**；本报告记录了全部变异 ID 与结果）
- 自建 DDL ↔ ORM 静态比对：16 张表集合一致、列名集合一致（解析器局限见 §5.5）
- `/tmp/qa_pristine`（`git archive HEAD`）与 `/tmp/qa_head`（HEAD + QA 测试）两个隔离快照，用于把阶段 0 交付态与在途阶段 1 改动分开
- 所有探针文件（`src/tagmind/domain/_qa_violation_probe.py`、`migrations/versions/_qa_phantom.py`）均已删除；`src/` 未被变异脚本残留污染（`git diff HEAD --` 为空，仅 CRLF 换行噪音已用 `git checkout HEAD --` 还原）
- 临时脚本 `scripts/_qa_mutate.py` / `scripts/_qa_ddl_diff.py` 已从工作区删除（§4.2），但曾被 `git add -A` 误纳入 `2b583c9`，**版本库中的删除需提交**
