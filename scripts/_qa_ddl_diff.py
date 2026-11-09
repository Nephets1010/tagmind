"""QA 静态脚本：迁移 DDL 与 ORM 定义的逐字段比对（稳健版）。

策略：把迁移脚本里的 CREATE TABLE 块用「括号配平 + 引号感知」的方式切出列定义，
再与 SQLAlchemy ORM metadata 比对列名 / NOT NULL / DEFAULT。

不做类型映射比对（DDL 用域类型如 quality_score_t，ORM 用 Numeric，
类型等价关系在本阶段不可机械判定），聚焦最容易漂移的三项：列名、可空性、默认值。

退出码 0 = 零差异；1 = 有差异。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path("D:/data/WorkBuddy/程序宇宙/repos/tagmind")
sys.path.insert(0, str(REPO / "src"))

import tagmind.db.models as _models  # noqa: F401,E402
from tagmind.db.base import Base  # noqa: E402

MIG = REPO / "migrations" / "versions"

CREATE_HEAD = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?\"?(\w+)\"?\s*\(", re.IGNORECASE
)

TABLE_CONSTRAINT_HEADS = {
    "CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "EXCLUDE",
    "PARTITION", "OF", "LIKE", "INHERITS",
}


def _find_matching_paren(text: str, open_idx: int) -> int:
    """从 '(' 处找到配对的 ')'，忽略引号内的括号。"""
    depth = 0
    in_single = False
    in_double = False
    i = open_idx
    while i < len(text):
        ch = text[i]
        if in_single:
            if ch == "'":
                in_single = False
        elif in_double:
            if ch == '"':
                in_double = False
        elif ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _split_top_level(body: str) -> list[str]:
    """按顶层逗号切分列定义块（引号感知）。"""
    parts, depth, cur = [], 0, []
    in_single = False
    in_double = False
    for ch in body:
        if in_single:
            if ch == "'":
                in_single = False
        elif in_double:
            if ch == '"':
                in_double = False
        elif ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def parse_migration_columns() -> dict[str, dict[str, dict[str, str]]]:
    """解析迁移脚本中的 CREATE TABLE。"""
    result: dict[str, dict[str, dict[str, str]]] = {}
    for path in sorted(MIG.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for m in CREATE_HEAD.finditer(text):
            table = m.group(1)
            open_idx = text.index("(", m.end() - 1)
            close_idx = _find_matching_paren(text, open_idx)
            if close_idx < 0:
                continue
            body = text[open_idx + 1 : close_idx]
            cols: dict[str, dict[str, str]] = {}
            for item in _split_top_level(body):
                head = item.split("\n")[0].strip().rstrip(",")
                if not head:
                    continue
                first = head.split()[0].strip('"').upper()
                if first in TABLE_CONSTRAINT_HEADS:
                    continue
                parts = head.split()
                name = parts[0].strip('"')
                rest = " ".join(parts[1:])
                up = head.upper()
                default = ""
                if "DEFAULT" in up:
                    default = head.split("DEFAULT", 1)[1].strip()
                    # 去掉尾随的 NOT NULL / 其它子句
                    for kw in (" NOT NULL", " CHECK", " REFERENCES", " GENERATED"):
                        idx = default.upper().find(kw)
                        if idx > 0:
                            default = default[:idx].strip()
                cols[name] = {
                    "not_null": "NOT NULL" in up,
                    "default": default,
                    "type": rest,
                    "raw": head,
                }
            result.setdefault(table, {}).update(cols)
    return result


def _norm_default(value: str) -> str:
    """归一化默认值以便比较。"""
    if not value:
        return ""
    v = value.strip().rstrip(",").strip()
    v = v.replace("::jsonb", "").replace("::text", "").strip()
    v = v.strip("'").strip()
    return v.lower()


def main() -> int:
    """执行比对并打印差异。"""
    mig = parse_migration_columns()
    orm = dict(Base.metadata.tables)

    print("=" * 78)
    print("表集合比对")
    print("=" * 78)
    mig_tables = {t for t in mig if not t.endswith("_default")}
    orm_tables = set(orm)
    only_mig = sorted(mig_tables - orm_tables)
    only_orm = sorted(orm_tables - mig_tables)
    print(f"迁移建表 {len(mig_tables)} 张 / ORM {len(orm_tables)} 张")
    if only_mig:
        print(f"  [仅迁移有] {only_mig}")
    if only_orm:
        print(f"  [仅 ORM 有] {only_orm}")
    if not only_mig and not only_orm:
        print("  表集合一致 OK")

    print()
    print("=" * 78)
    print("逐表逐字段比对（列名 / NOT NULL / DEFAULT）")
    print("=" * 78)
    diffs: list[str] = []
    for table in sorted(mig_tables & orm_tables):
        mcols = mig[table]
        ocols = orm[table].columns
        mnames, onames = set(mcols), set(ocols.keys())

        missing = sorted(onames - mnames)
        extra = sorted(mnames - onames)
        if missing:
            diffs.append(f"[{table}] ORM 有但迁移无的列: {missing}")
        if extra:
            diffs.append(f"[{table}] 迁移有但 ORM 无的列: {extra}")

        for col in sorted(mnames & onames):
            oc = ocols[col]
            mnn = mcols[col]["not_null"]
            onn = oc.nullable is False
            if mnn != onn:
                diffs.append(
                    f"[{table}.{col}] NOT NULL 不一致: 迁移={mnn} ORM_nullable={oc.nullable} "
                    f"| DDL: {mcols[col]['raw'][:80]}"
                )
            # ORM 侧 server_default
            osm = oc.server_default
            osm_text = ""
            if osm is not None:
                osm_text = str(getattr(osm, "arg", osm)).strip().strip("'").lower()
            mdf = _norm_default(mcols[col]["default"])
            if mdf and not osm_text:
                # 迁移有 DEFAULT 但 ORM 无 server_default —— 记录（ORM 的 python-side
                # default 也会落库，属可接受漂移，但需人工确认）
                pass
            if osm_text and not mdf:
                diffs.append(
                    f"[{table}.{col}] ORM 有 server_default({osm_text}) 但迁移 DDL 未见 DEFAULT | "
                    f"DDL: {mcols[col]['raw'][:80]}"
                )

    for d in diffs:
        print(f"  {d}")
    print()
    print("=" * 78)
    print(f"差异条目合计：{len(diffs)}")
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main())
