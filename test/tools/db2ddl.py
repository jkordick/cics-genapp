#!/usr/bin/env python3
"""Turn GenApp's Db2 setup job (base/cntl/db2cre.jcl) into PostgreSQL SQL.

The JCL stays the single source of truth for the schema: a new table added
there (for example for a new policy type) is picked up by the tests
automatically. Only CREATE TABLE and INSERT statements are used; storage
groups, databases, tablespaces, indexes and GRANTs are mainframe
housekeeping with no effect on behaviour and are skipped.

Usage: db2ddl.py [base/cntl/db2cre.jcl]  > schema.sql
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_JCL = os.path.join(HERE, "..", "..", "base", "cntl", "db2cre.jcl")

SKIP = re.compile(
    r"^(SET\s+CURRENT|CREATE\s+(STOGROUP|DATABASE|TABLESPACE|(UNIQUE\s+)?INDEX)|GRANT|COMMIT)\b", re.I)


class DDLError(Exception):
    pass


def instream_sql(jcl: str) -> str:
    """Concatenate the SYSIN DD * data (the SQL) of every step."""
    out, in_sysin = [], False
    for line in jcl.splitlines():
        if in_sysin:
            if line.startswith("/*") or line.startswith("//"):
                in_sysin = False
            else:
                out.append(line[:72])
                continue
        if re.match(r"^//SYSIN\s+DD\s+\*", line):
            in_sysin = True
    return "\n".join(out)


def split_statements(sql: str) -> list[str]:
    stmts, cur, quote = [], [], False
    for c in sql:
        if c == "'":
            quote = not quote
        if c == ";" and not quote:
            stmts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
    if "".join(cur).strip():
        stmts.append("".join(cur).strip())
    return [s for s in stmts if s]


def split_top_level(s: str) -> list[str]:
    parts, cur, depth, quote = [], [], 0, False
    for c in s:
        if c == "'":
            quote = not quote
        if not quote:
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
            elif c == "," and depth == 0:
                parts.append("".join(cur).strip())
                cur = []
                continue
        cur.append(c)
    parts.append("".join(cur).strip())
    return parts


def matching_paren(s: str, start: int) -> int:
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    raise DDLError("unbalanced parentheses")


def strip_qualifier(s: str) -> str:
    return re.sub(r"<[A-Za-z0-9_]+>\.", "", s)


def convert_column(col: str) -> str:
    col = re.sub(r"\s+", " ", col.strip())
    # Identity options: Db2 separates them with commas, PostgreSQL does not.
    # CACHE is dropped so numbers stay consecutive across test processes.
    def ident(m):
        opts = re.sub(r"\s*,\s*", " ", m.group(1))
        opts = re.sub(r"\bCACHE\s+\d+\b|\bNO\s+CACHE\b", "", opts, flags=re.I)
        return f"AS IDENTITY ({re.sub(r' +', ' ', opts).strip()})"
    col = re.sub(r"AS\s+IDENTITY\s*\(([^)]*)\)", ident, col, flags=re.I)

    m = re.search(r"\bWITH\s+DEFAULT\b(\s+\S+)?", col, re.I)
    if m and not m.group(1):
        ctype = col.split(" ")[1].upper() if " " in col else ""
        default = {"TIMESTAMP": "CURRENT_TIMESTAMP", "DATE": "CURRENT_DATE",
                   "TIME": "CURRENT_TIME"}.get(ctype)
        if default is None:
            default = "''" if re.match(r"(VAR)?CHAR", ctype) else "0"
        col = col[:m.start()] + f"DEFAULT {default}" + col[m.end():]
    elif m:
        col = col[:m.start()] + "DEFAULT" + m.group(1) + col[m.end():]
    return col


def convert_create_table(stmt: str) -> str:
    stmt = strip_qualifier(stmt)
    m = re.match(r"CREATE\s+TABLE\s+([A-Za-z0-9_]+)\s*\(", stmt, re.I)
    if not m:
        raise DDLError(f"cannot parse: {stmt[:60]}")
    start = m.end() - 1
    end = matching_paren(stmt, start)
    cols = [convert_column(c) for c in split_top_level(stmt[start + 1:end])]
    body = ",\n    ".join(cols)
    # Everything after the column list (CCSID, IN tablespace...) is storage only.
    return f"CREATE TABLE {m.group(1)} (\n    {body}\n);"


def convert_insert(stmt: str) -> str:
    stmt = strip_qualifier(stmt)
    stmt = re.sub(r"[ \t]+\n", "\n", stmt)
    return stmt.rstrip() + ";"


def convert(jcl_text: str) -> str:
    out = ["-- Generated from base/cntl/db2cre.jcl by test/tools/db2ddl.py. Do not edit.",
           "SET client_min_messages TO WARNING;"]
    for stmt in split_statements(instream_sql(jcl_text)):
        flat = re.sub(r"\s+", " ", stmt).strip()
        if SKIP.match(flat):
            continue
        if re.match(r"CREATE\s+TABLE\b", flat, re.I):
            out.append(convert_create_table(stmt))
        elif re.match(r"INSERT\s+INTO\b", flat, re.I):
            out.append(convert_insert(stmt))
        else:
            raise DDLError(f"statement not understood (extend test/tools/db2ddl.py): {flat[:80]}")
    return "\n\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else DEFAULT_JCL
    try:
        with open(path, encoding="latin-1") as f:
            sys.stdout.write(convert(f.read()))
    except DDLError as e:
        print(f"db2ddl: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
