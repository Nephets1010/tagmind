"""QA 变异测试脚本：验证既有测试套件能否真的捕获缺陷。

用法：python /tmp/mutate.py
脚本对 src/ 下指定文件做「反向变异」（注入缺陷），跑 pytest 看是否有测试失败。
全部失败 = 测试有效；任何存活变异 = 测试套件的「假通过」缺口。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path("D:/data/WorkBuddy/程序宇宙/repos/tagmind")

# (变异 ID, 目标文件, 原代码片段, 变异后片段, 用于选取测试的路径)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # --- 1. TokenBucket.consume 恒返回 True ---
    (
        "M01_token_bucket_always_true",
        "src/tagmind/core/security.py",
        """            if self._tokens >= amount:
                self._tokens -= amount
                return True
            return False""",
        """            if self._tokens >= amount:
                self._tokens -= amount
                return True
            return True""",
        "tests/unit",
    ),
    # --- 2. 错误码重复（把 NotFoundError 改成与 ValidationError 同码）---
    (
        "M02_duplicate_error_code",
        "src/tagmind/core/errors.py",
        """class NotFoundError(TagmindError):
    \"\"\"资源不存在（404）。\"\"\"

    code = "TM_1001"
    http_status = 404""",
        """class NotFoundError(TagmindError):
    \"\"\"资源不存在（404）。\"\"\"

    code = "TM_1000"
    http_status = 404""",
        "tests/unit",
    ),
    # --- 3. 移除日志脱敏的 "password" 键名 ---
    (
        "M03_logging_drop_password_key",
        "src/tagmind/core/logging.py",
        '    "password",\n',
        "",
        "tests/unit",
    ),
    # --- 4. resolved_database_url 不做 URL 编码 ---
    (
        "M04_database_url_no_quote",
        "src/tagmind/config/settings.py",
        """        user = quote(self.postgres_user, safe="")
        password = quote(self.postgres_password.get_secret_value(), safe="")""",
        """        user = self.postgres_user
        password = self.postgres_password.get_secret_value()""",
        "tests/unit",
    ),
    # --- 5. ancestors 计算反序（祖先数组顺序错）---
    (
        "M05_ancestors_reversed",
        "src/tagmind/labeling/taxonomy.py",
        "            ancestors = (*parent_node.ancestors, parent_node.code)",
        "            ancestors = (parent_node.code, *parent_node.ancestors)",
        "tests/unit",
    ),
    # --- 6. clear_context 不清理 ---
    (
        "M06_clear_context_noop",
        "src/tagmind/core/context.py",
        """    _request_id.set(None)
    _trace_id.set(None)
    _user_id.set(None)
    _content_id.set(None)""",
        """    return None""",
        "tests/unit",
    ),
    # --- 7. optimistic_update 恒返回 True ---
    (
        "M07_optimistic_update_always_true",
        "src/tagmind/db/repositories/base.py",
        """        result = cast("CursorResult[Any]", self._session.execute(stmt))
        self._session.flush()
        return result.rowcount > 0""",
        """        result = cast("CursorResult[Any]", self._session.execute(stmt))
        self._session.flush()
        return True""",
        "tests/unit",
    ),
    # --- 8. insert_ignore 恒返回 inserted=True ---
    (
        "M08_insert_ignore_always_new",
        "src/tagmind/db/repositories/base.py",
        """            return existing, False""",
        """            return existing, True""",
        "tests/unit",
    ),
    # --- 9. _drop_server_managed 不过滤（created_at=None 会写入）---
    (
        "M09_drop_server_managed_disabled",
        "src/tagmind/db/repositories/base.py",
        """        table = self.model.__table__
        return {
            column: value
            for column, value in values.items()
            if not (
                value is None
                and column in table.columns
                and table.columns[column].server_default is not None
            )
        }""",
        """        return dict(values)""",
        "tests/unit",
    ),
    # --- 10. retry_on_deadlock 不重试 ---
    (
        "M10_retry_no_retry",
        "src/tagmind/db/repositories/base.py",
        """        except OperationalError as exc:
            if not _is_retryable(exc):
                raise""",
        """        except OperationalError as exc:
            if True:
                raise""",
        "tests/unit",
    ),
    # --- 11. taxonomy A2 校验失效（层与父的关系不检查）---
    (
        "M11_validate_structure_layer_check_off",
        "src/tagmind/labeling/taxonomy.py",
        """            elif parent.layer != node.layer - 1:
                violations.append(
                    f"A2 {node.code}：父节点 {parent.code} 的层是 L{parent.layer}，"
                    f"应为 L{node.layer - 1}"
                )""",
        """            elif False:
                pass""",
        "tests/unit",
    ),
    # --- 12. metrics: 领域层混入框架 import ---
    (
        "M12_domain_imports_sqlalchemy",
        "src/tagmind/domain/label.py",
        "from __future__ import annotations",
        "from __future__ import annotations\n\nimport sqlalchemy",
        "tests/unit",
    ),
    # --- 13. 配置：production 缺密码不报错 ---
    (
        "M13_production_no_password_ok",
        "src/tagmind/config/settings.py",
        """        if self.env == "production" and not self.postgres_password.get_secret_value():
            raise ConfigError(""",
        """        if False:
            raise ConfigError(""",
        "tests/unit",
    ),
    # --- 14. celery task_failure 去掉 **extra（复现主理人修过的缺陷）---
    (
        "M14_celery_signal_no_kwargs",
        "src/tagmind/scheduler/celery_app.py",
        "    **extra: object,\n",
        "",
        "tests/unit",
    ),
    # --- 15. API: X-Request-ID 头不返回 ---
    (
        "M15_no_request_id_header",
        "src/tagmind/api/app.py",
        '        response.headers["X-Request-ID"] = request_id\n',
        "",
        "tests/unit",
    ),
]


def run_tests(test_path: str) -> tuple[int, str]:
    """跑测试，返回 (returncode, 输出摘要)。"""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", test_path, "-q", "--no-header", "-p", "no:cacheprovider",
         "--no-cov", "-x", "--tb=no"],
        cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode, proc.stdout


def main() -> None:
    """对每个变异体跑测试并报告存活情况。"""
    baseline_rc, baseline_out = run_tests("tests/unit")
    print(f"[BASELINE] rc={baseline_rc}")
    print(baseline_out[-800:])
    print("=" * 78)

    survivors: list[str] = []
    for mut_id, rel_path, old, new, test_path in MUTATIONS:
        target = REPO / rel_path
        backup = target.read_text(encoding="utf-8")
        if old not in backup:
            print(f"[SKIP-NOTFOUND] {mut_id}: 目标片段未找到于 {rel_path}")
            continue
        target.write_text(backup.replace(old, new, 1), encoding="utf-8")
        try:
            rc, out = run_tests(test_path)
        finally:
            target.write_text(backup, encoding="utf-8")
        if rc == 0:
            survivors.append(mut_id)
            print(f"[SURVIVED  ] {mut_id}: 测试全绿 —— 该缺陷未被捕获！")
        else:
            tail = [ln for ln in out.splitlines() if "failed" in ln or "error" in ln]
            print(f"[KILLED   ] {mut_id}: 测试失败（捕获成功） {tail[-1:]}")
    print("=" * 78)
    print(f"存活变异体数量：{len(survivors)} / {len(MUTATIONS)}")
    for s in survivors:
        print(f"  - {s}")


if __name__ == "__main__":
    main()
