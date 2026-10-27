#!/usr/bin/env python3
"""Translate GenApp CICS/Db2 COBOL into plain GnuCOBOL for off-mainframe tests.

What it does to each program (the source in base/src is never modified):

* expands COPY / EXEC SQL INCLUDE copybooks (SQLCA comes from test/runtime)
* adds the EXEC interface block (DFHEIBLK) and PROCEDURE DIVISION USING
* replaces every EXEC CICS command with CALLs into test/runtime/genapprt.c
  (LINK becomes a dynamic CALL of the target program's module)
* replaces every EXEC SQL statement with host-variable binds and a call
  that runs the statement on PostgreSQL (Db2 syntax rewritten where needed)

Anything it does not understand stops the build with an error that points
at the program and line, so unsupported code never silently "passes".

Usage: precompile.py SOURCE.cbl OUTPUT.cob [--copy-dir DIR ...]
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field

HERE = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR = os.path.join(os.path.dirname(HERE), "runtime")

# CICS RESP values used by DFHRESP(...)
DFHRESP = {
    "NORMAL": 0, "ERROR": 1, "RDATT": 2, "WRBRK": 3, "EOF": 4, "EODS": 5,
    "EOC": 6, "INBFMH": 7, "ENDINPT": 8, "NONVAL": 9, "NOSTART": 10,
    "TERMIDERR": 11, "FILENOTFOUND": 12, "DSIDERR": 12, "NOTFND": 13,
    "DUPREC": 14, "DUPKEY": 15, "INVREQ": 16, "IOERR": 17, "NOSPACE": 18,
    "NOTOPEN": 19, "ENDFILE": 20, "ILLOGIC": 21, "LENGERR": 22, "QZERO": 23,
    "SIGNAL": 24, "QBUSY": 25, "ITEMERR": 26, "PGMIDERR": 27, "TRANSIDERR": 28,
    "ENDDATA": 29, "EXPIRED": 31, "MAPFAIL": 36, "QIDERR": 44, "NOTAUTH": 70,
    "DISABLED": 84, "SUPPRESSED": 102, "LOCKED": 100,
}

# Host variable type codes - must match genapprt.c
HV_CHAR, HV_BINARY, HV_NATIVE, HV_DISPLAY, HV_PACKED, HV_VARCHAR, HV_VARCHAR5 = 1, 2, 3, 4, 5, 6, 7


class TranslateError(Exception):
    pass


# --------------------------------------------------------------------------
# Source handling
# --------------------------------------------------------------------------

@dataclass
class Line:
    text: str          # columns 8-72, right-stripped
    origin: str        # "file:line" for error messages


def read_fixed(path: str) -> list[Line]:
    """Read a fixed-format source file, dropping comments and sequence areas."""
    out = []
    with open(path, encoding="latin-1") as f:
        for n, raw in enumerate(f, 1):
            raw = raw.rstrip("\r\n")
            if len(raw) < 7:
                out.append(Line("", f"{os.path.basename(path)}:{n}"))
                continue
            indicator = raw[6]
            body = raw[7:72].rstrip()
            origin = f"{os.path.basename(path)}:{n}"
            if indicator in "*/":
                continue
            if indicator in "dD":
                continue
            if indicator == "-":
                raise TranslateError(f"{origin}: continuation lines are not supported")
            if re.match(r"\s*(PROCESS|CBL)\b", body, re.I) and all(not l.text.strip() for l in out):
                continue
            out.append(Line(body, origin))
    return out


def find_copybook(name: str, copy_dirs: list[str]) -> str:
    for d in copy_dirs:
        for cand in (name, name.lower(), name.upper()):
            for ext in (".cpy", ".CPY", ""):
                p = os.path.join(d, cand + ext)
                if os.path.isfile(p):
                    return p
    raise TranslateError(f"copybook {name} not found in {copy_dirs}")


COPY_RE = re.compile(r"^(\s*)COPY\s+([A-Z0-9$#@-]+)\s*\.\s*$", re.I)
INCLUDE_RE = re.compile(r"^\s*EXEC\s+SQL\s+INCLUDE\s+([A-Z0-9$#@-]+)\s+END-EXEC\s*\.?\s*$", re.I)


def expand_copybooks(lines: list[Line], copy_dirs: list[str], depth: int = 0) -> list[Line]:
    """Inline COPY x. and EXEC SQL INCLUDE x END-EXEC (also when split over lines)."""
    if depth > 10:
        raise TranslateError("copybooks nested too deeply")
    out: list[Line] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        # Join a possibly multi-line EXEC SQL INCLUDE into one line.
        if re.match(r"^\s*EXEC\s+SQL\s*$", ln.text, re.I) or re.match(r"^\s*EXEC\s+SQL\s+INCLUDE\b", ln.text, re.I):
            j, joined = i, ln.text
            while "END-EXEC" not in joined.upper() and j + 1 < len(lines):
                j += 1
                joined += " " + lines[j].text.strip()
            m = INCLUDE_RE.match(joined)
            if m:
                name = m.group(1).upper()
                if name == "SQLCA":
                    path = os.path.join(RUNTIME_DIR, "SQLCA.cpy")
                else:
                    path = find_copybook(name, copy_dirs)
                out.extend(expand_copybooks(read_fixed(path), copy_dirs, depth + 1))
                i = j + 1
                continue
        m = COPY_RE.match(ln.text)
        if m:
            path = find_copybook(m.group(2), copy_dirs)
            out.extend(expand_copybooks(read_fixed(path), copy_dirs, depth + 1))
            i += 1
            continue
        if re.search(r"\bCOPY\s", ln.text, re.I) and not re.search(r"'[^']*COPY", ln.text, re.I):
            raise TranslateError(f"{ln.origin}: unsupported COPY form: {ln.text.strip()}")
        out.append(ln)
        i += 1
    return out


# --------------------------------------------------------------------------
# DATA DIVISION symbol table (for host variable types)
# --------------------------------------------------------------------------

@dataclass
class DataItem:
    level: int
    name: str
    pic: str | None
    usage: str
    children: list["DataItem"] = field(default_factory=list)
    parent: "DataItem | None" = None


def split_sentences(text: str) -> list[str]:
    """Split on periods that end an entry (outside quotes, followed by space/end)."""
    out, cur, quote = [], [], None
    i = 0
    while i < len(text):
        c = text[i]
        if quote:
            cur.append(c)
            if c == quote:
                quote = None
        elif c in "'\"":
            quote = c
            cur.append(c)
        elif c == "." and (i + 1 == len(text) or text[i + 1].isspace()):
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    if "".join(cur).strip():
        out.append("".join(cur).strip())
    return out


def tokenize_entry(s: str) -> list[str]:
    return re.findall(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|[^\s]+", s)


USAGE_WORDS = {
    "COMP": "COMP", "COMPUTATIONAL": "COMP", "BINARY": "COMP", "COMP-4": "COMP",
    "COMPUTATIONAL-4": "COMP", "COMP-5": "COMP-5", "COMPUTATIONAL-5": "COMP-5",
    "COMP-3": "COMP-3", "COMPUTATIONAL-3": "COMP-3", "PACKED-DECIMAL": "COMP-3",
    "DISPLAY": "DISPLAY", "POINTER": "POINTER", "INDEX": "INDEX",
    "COMP-1": "COMP-1", "COMP-2": "COMP-2",
}


def parse_data_division(text: str) -> dict[str, list[DataItem]]:
    symbols: dict[str, list[DataItem]] = {}
    stack: list[DataItem] = []
    for sent in split_sentences(text):
        toks = tokenize_entry(sent)
        if not toks or not toks[0].isdigit():
            continue
        level = int(toks[0])
        if level in (66, 88):
            continue
        name = "FILLER"
        rest = toks[1:]
        if rest and rest[0].upper() not in ("PIC", "PICTURE", "VALUE", "REDEFINES", "OCCURS", "USAGE") \
                and rest[0].upper() not in USAGE_WORDS:
            name = rest[0].upper()
            rest = rest[1:]
        pic, usage = None, None
        j = 0
        while j < len(rest):
            t = rest[j].upper()
            if t in ("PIC", "PICTURE"):
                j += 1
                if j < len(rest) and rest[j].upper() == "IS":
                    j += 1
                pic = rest[j].upper()
            elif t == "USAGE":
                pass
            elif t in USAGE_WORDS:
                usage = USAGE_WORDS[t]
            elif t == "VALUE" or t == "VALUES":
                break
            j += 1
        if level in (1, 77):
            stack = []
        while stack and stack[-1].level >= level:
            stack.pop()
        parent = stack[-1] if stack else None
        if usage is None:
            usage = parent.usage if parent else "DISPLAY"
        item = DataItem(level, name, pic, usage, parent=parent)
        if parent:
            parent.children.append(item)
        stack.append(item)
        if name != "FILLER":
            symbols.setdefault(name, []).append(item)
    return symbols


def pic_info(pic: str) -> tuple[bool, bool, int, int]:
    """(alphanumeric, signed, digits, scale) for a PICTURE string."""
    expanded = re.sub(r"(.)\((\d+)\)", lambda m: m.group(1) * int(m.group(2)), pic.upper())
    alpha = any(c in expanded for c in "XAN")
    signed = expanded.startswith("S")
    if "V" in expanded:
        left, right = expanded.split("V", 1)
    else:
        left, right = expanded, ""
    return alpha, signed, left.count("9"), right.count("9")


def host_type(name: str, symbols: dict[str, list[DataItem]], origin: str) -> tuple[int, int, int]:
    """(type code, scale, signed) for a host variable."""
    items = symbols.get(name.upper())
    if not items:
        raise TranslateError(f"{origin}: host variable :{name} is not defined")
    results = {_item_type(it, origin) for it in items}
    if len(results) > 1:
        raise TranslateError(f"{origin}: host variable :{name} is defined more than once with different types")
    return results.pop()


def _item_type(it: DataItem, origin: str) -> tuple[int, int, int]:
    if it.pic is None:
        kids = it.children
        if len(kids) == 2 and all(k.level == 49 for k in kids) and kids[0].pic and kids[1].pic:
            if kids[0].usage == "COMP":
                return (HV_VARCHAR, 0, 0)
            if kids[0].usage == "COMP-5":
                return (HV_VARCHAR5, 0, 0)
        raise TranslateError(f"{origin}: host variable {it.name} is a group item "
                             "(host structures are not supported)")
    alpha, signed, _digits, scale = pic_info(it.pic)
    if alpha:
        return (HV_CHAR, 0, 0)
    code = {"COMP": HV_BINARY, "COMP-5": HV_NATIVE, "DISPLAY": HV_DISPLAY, "COMP-3": HV_PACKED}.get(it.usage)
    if code is None:
        raise TranslateError(f"{origin}: host variable {it.name} has unsupported usage {it.usage}")
    return (code, scale, 1 if signed else 0)


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

def split_tokens(stmt: str) -> list[str]:
    return re.findall(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|\S+", stmt)


# All text handled here is relative to column 8 (Area A); 65 chars fit up to column 72.
LINE_MAX = 65


def wrap(stmt: str, indent: int = 4) -> list[str]:
    """Wrap one COBOL statement into fixed-format lines (Area B, up to column 72)."""
    lines, cur = [], " " * indent
    for tok in split_tokens(stmt):
        candidate = (cur + tok) if cur.strip() == "" else (cur + " " + tok)
        if len(candidate) > LINE_MAX and cur.strip():
            lines.append(cur)
            cur = " " * (indent + 4) + tok
        else:
            cur = candidate
    if cur.strip():
        lines.append(cur)
    for ln in lines:
        if len(ln) > LINE_MAX:
            raise TranslateError(f"generated line too long: {ln.strip()}")
    return lines


def close_calls(stmts: list[str]) -> list[str]:
    """Give every generated CALL its own END-CALL.

    Without it, a CALL nested in another CALL's ON EXCEPTION branch would take
    the outer END-CALL, and the statements after it would silently become
    part of the exception branch.
    """
    out = []
    for i, st in enumerate(stmts):
        if st.startswith("CALL ") and not (i + 1 < len(stmts) and stmts[i + 1] == "ON EXCEPTION"):
            st += " END-CALL"
        out.append(st)
    return out


def cobol_literal(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


# --------------------------------------------------------------------------
# EXEC CICS
# --------------------------------------------------------------------------

def parse_options(body: str, origin: str) -> tuple[str, dict[str, str | None]]:
    """Parse 'LINK PROGRAM(X) COMMAREA(Y)' into ('LINK', {'PROGRAM': 'X', ...})."""
    i, n = 0, len(body)
    words: list[tuple[str, str | None]] = []
    while i < n:
        if body[i].isspace():
            i += 1
            continue
        m = re.compile(r"[A-Za-z0-9-]+").match(body, i)
        if not m:
            raise TranslateError(f"{origin}: cannot parse EXEC CICS near: {body[i:i+30]}")
        word = m.group(0).upper()
        i = m.end()
        while i < n and body[i].isspace():
            i += 1
        arg = None
        if i < n and body[i] == "(":
            depth, j, quote = 0, i, None
            while j < n:
                c = body[j]
                if quote:
                    if c == quote:
                        quote = None
                elif c in "'\"":
                    quote = c
                elif c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if depth != 0:
                raise TranslateError(f"{origin}: unbalanced parentheses in EXEC CICS")
            arg = re.sub(r"\s+", " ", body[i + 1:j].strip())
            i = j + 1
        words.append((word, arg))
    if not words:
        raise TranslateError(f"{origin}: empty EXEC CICS")
    cmd = words[0][0]
    opts: dict[str, str | None] = {}
    for w, a in words[1:]:
        opts[w] = a
    return cmd, opts


def is_literal_number(s: str) -> bool:
    return bool(re.fullmatch(r"[+-]?\d+", s.strip()))


class Translator:
    def __init__(self, source: str, copy_dirs: list[str]):
        self.source = source
        self.copy_dirs = copy_dirs
        self.stmts: list[str] = []          # SQL statement texts
        self.cursors: dict[str, tuple[str, list]] = {}
        self.symbols: dict[str, list[DataItem]] = {}
        self.program_id = ""

    # ---- helpers --------------------------------------------------------
    def fail(self, origin: str, msg: str):
        raise TranslateError(f"{origin}: {msg}")

    def length_expr(self, opts, key, data_opt):
        if opts.get(key):
            return opts[key]
        if data_opt and opts.get(data_opt):
            return f"FUNCTION BYTE-LENGTH({opts[data_opt]})"
        return None

    def resp_moves(self, opts) -> list[str]:
        out = ["MOVE CICS-RESP TO EIBRESP", "MOVE CICS-RESP2 TO EIBRESP2"]
        if opts.get("RESP"):
            out.append(f"MOVE CICS-RESP TO {opts['RESP']}")
        if opts.get("RESP2"):
            out.append(f"MOVE CICS-RESP2 TO {opts['RESP2']}")
        return out

    def normal_resp(self, opts) -> list[str]:
        return ["MOVE 0 TO CICS-RESP", "MOVE 0 TO CICS-RESP2"] + self.resp_moves(opts)

    def check_opts(self, cmd, opts, allowed, origin):
        extra = set(opts) - set(allowed)
        if extra:
            self.fail(origin, f"EXEC CICS {cmd}: unsupported option(s) {', '.join(sorted(extra))}")

    # ---- CICS -----------------------------------------------------------
    def cics(self, body: str, origin: str) -> list[str]:
        cmd, o = parse_options(body, origin)
        common = {"RESP", "RESP2", "NOHANDLE"}

        if cmd == "RETURN":
            self.check_opts(cmd, o, common, origin)
            return ["GOBACK"]

        if cmd == "LINK":
            self.check_opts(cmd, o, {"PROGRAM", "COMMAREA", "LENGTH", "DATALENGTH"} | common, origin)
            ca = o.get("COMMAREA")
            length = self.length_expr(o, "LENGTH", "COMMAREA") if ca else "0"
            return [
                f"MOVE {o['PROGRAM']} TO CICS-PROGRAM",
                "MOVE DFHEIBLK TO CICS-LINK-EIB",
                f"MOVE {length} TO CLE-EIBCALEN",
                f"MOVE {cobol_literal(self.program_id)} TO CLE-EIBXINVK",
                f"CALL CICS-PROGRAM USING CICS-LINK-EIB {ca or 'CICS-NO-COMMAREA'}",
                "ON EXCEPTION",
                "MOVE 'AEI0' TO CICS-ABCODE",
                "CALL STATIC 'CICSABND' USING CICS-ABCODE",
                "END-CALL",
                # A LINKed program starts with fresh WORKING-STORAGE every time.
                "CANCEL CICS-PROGRAM",
            ] + self.normal_resp(o)

        if cmd == "ABEND":
            self.check_opts(cmd, o, {"ABCODE", "NODUMP", "CANCEL"} | common, origin)
            code = o.get("ABCODE") or "'ABND'"
            return [f"MOVE {code} TO CICS-ABCODE", "CALL STATIC 'CICSABND' USING CICS-ABCODE"]

        if cmd == "ASKTIME":
            self.check_opts(cmd, o, {"ABSTIME"} | common, origin)
            out = ["MOVE FUNCTION CURRENT-DATE TO CICS-NOW"]
            if o.get("ABSTIME"):
                out.append(f"MOVE 0 TO {o['ABSTIME']}")
            return out + self.normal_resp(o)

        if cmd == "FORMATTIME":
            self.check_opts(cmd, o, {"ABSTIME", "MMDDYYYY", "DDMMYYYY", "YYYYMMDD", "TIME"} | common, origin)
            out = []
            if o.get("MMDDYYYY"):
                v = o["MMDDYYYY"]
                out += [f"MOVE CICS-NOW(5:4) TO {v}(1:4)", f"MOVE CICS-NOW(1:4) TO {v}(5:4)"]
            if o.get("DDMMYYYY"):
                v = o["DDMMYYYY"]
                out += [f"MOVE CICS-NOW(7:2) TO {v}(1:2)", f"MOVE CICS-NOW(5:2) TO {v}(3:2)",
                        f"MOVE CICS-NOW(1:4) TO {v}(5:4)"]
            if o.get("YYYYMMDD"):
                out.append(f"MOVE CICS-NOW(1:8) TO {o['YYYYMMDD']}(1:8)")
            if o.get("TIME"):
                out.append(f"MOVE CICS-NOW(9:6) TO {o['TIME']}(1:6)")
            return out + self.normal_resp(o)

        if cmd == "ASSIGN":
            self.check_opts(cmd, o, {"SYSID", "INVOKINGPROG", "STARTCODE", "APPLID", "USERID"} | common, origin)
            out = []
            if o.get("SYSID"):
                out.append(f"MOVE 'GNAP' TO {o['SYSID']}")
            if o.get("APPLID"):
                out.append(f"MOVE 'GENAPP' TO {o['APPLID']}")
            if o.get("USERID"):
                out.append(f"MOVE 'TESTUSER' TO {o['USERID']}")
            if o.get("INVOKINGPROG"):
                out.append(f"MOVE EIBXINVK TO {o['INVOKINGPROG']}")
            if o.get("STARTCODE"):
                out.append(f"MOVE 'D' TO {o['STARTCODE']}")
            return out + self.normal_resp(o)

        if cmd in ("WRITE", "READ", "REWRITE", "DELETE"):
            allowed = {"FILE", "DATASET", "FROM", "INTO", "LENGTH", "RIDFLD", "KEYLENGTH", "UPDATE"} | common
            self.check_opts(cmd, o, allowed, origin)
            file_ = o.get("FILE") or o.get("DATASET")
            if not file_:
                self.fail(origin, f"EXEC CICS {cmd} without FILE")
            data = o.get("FROM") or o.get("INTO") or "CICS-NO-COMMAREA"
            key = o.get("RIDFLD") or "CICS-NO-COMMAREA"
            op = {"WRITE": "WRIT", "READ": "RDUP" if "UPDATE" in o else "READ",
                  "REWRITE": "REWR", "DELETE": "DELE"}[cmd]
            length = self.length_expr(o, "LENGTH", "FROM" if "FROM" in o else "INTO") or "0"
            keylen = o.get("KEYLENGTH") or (f"FUNCTION BYTE-LENGTH({o['RIDFLD']})" if o.get("RIDFLD") else "0")
            out = [
                f"MOVE '{op}' TO CICS-OP",
                f"MOVE {file_} TO CICS-FILE",
                f"MOVE {length} TO CICS-LEN",
                f"MOVE {keylen} TO CICS-KEYLEN",
                f"MOVE {1 if ('RESP' in o or 'NOHANDLE' in o) else 0} TO CICS-FLAG",
                f"CALL STATIC 'CICSFILE' USING CICS-OP CICS-FILE {data} CICS-LEN {key} "
                "CICS-KEYLEN CICS-RESP CICS-RESP2 CICS-FLAG",
            ]
            if cmd == "READ" and o.get("LENGTH") and not is_literal_number(o["LENGTH"]):
                out.append(f"MOVE CICS-LEN TO {o['LENGTH']}")
            return out + self.resp_moves(o)

        if cmd == "WRITEQ":
            self.check_opts(cmd, o, {"TS", "TD", "QUEUE", "QNAME", "FROM", "LENGTH", "NOSUSPEND",
                                     "MAIN", "AUXILIARY", "ITEM"} | common, origin)
            kind = "TD" if "TD" in o else "TS"
            q = o.get("QUEUE") or o.get("QNAME")
            length = self.length_expr(o, "LENGTH", "FROM")
            return [
                f"MOVE '{kind}' TO CICS-QTYPE",
                f"MOVE {q} TO CICS-QUEUE",
                f"MOVE {length} TO CICS-LEN",
                f"CALL STATIC 'CICSWRQ' USING CICS-QTYPE CICS-QUEUE {o['FROM']} CICS-LEN CICS-RESP",
                "MOVE 0 TO CICS-RESP2",
            ] + self.resp_moves(o)

        if cmd == "SYNCPOINT":
            self.check_opts(cmd, o, {"ROLLBACK"} | common, origin)
            return [f"MOVE {1 if 'ROLLBACK' in o else 0} TO CICS-FLAG",
                    "CALL STATIC 'CICSSYNC' USING CICS-FLAG"] + self.normal_resp(o)

        if cmd in ("ENQ", "DEQ"):
            self.check_opts(cmd, o, {"RESOURCE", "LENGTH", "NOSUSPEND", "TASK", "UOW"} | common, origin)
            return ["CONTINUE"] + self.normal_resp(o)  # single task: nothing to serialise

        if cmd == "SEND":
            self.check_opts(cmd, o, {"TEXT", "FROM", "LENGTH", "ERASE", "FREEKB", "WAIT"} | common, origin)
            return ["CONTINUE"] + self.normal_resp(o)  # no terminal

        if cmd == "GET" and "COUNTER" in o:
            self.check_opts(cmd, o, {"COUNTER", "POOL", "VALUE"} | common, origin)
            # No named counter server: behave as if it is unavailable, so the
            # program uses its fallback (Db2 identity column).
            return ["MOVE 16 TO CICS-RESP", "MOVE 0 TO CICS-RESP2"] + self.resp_moves(o) + (
                [] if ("RESP" in o or "NOHANDLE" in o) else
                ["MOVE 'AEIP' TO CICS-ABCODE", "CALL STATIC 'CICSABND' USING CICS-ABCODE"])

        if cmd == "RECEIVE":
            # Only reachable when started from a terminal, never in these tests.
            return ["MOVE 'GARC' TO CICS-ABCODE", "CALL STATIC 'CICSABND' USING CICS-ABCODE"]

        self.fail(origin, f"EXEC CICS {cmd} is not supported by the test runtime")
        return []

    # ---- SQL ------------------------------------------------------------
    HV_RE = re.compile(
        r":\s*([A-Za-z0-9][A-Za-z0-9-]*[A-Za-z0-9]|[A-Za-z0-9])"
        r"(?:\s+(?:INDICATOR\s+)?:\s*([A-Za-z0-9][A-Za-z0-9-]*[A-Za-z0-9]|[A-Za-z0-9]))?",
        re.I)

    def _outside_quotes(self, sql: str, fn):
        """Apply fn to the parts of sql that are not inside string literals."""
        parts = re.split(r"('(?:[^']|'')*')", sql)
        return "".join(p if i % 2 else fn(p) for i, p in enumerate(parts))

    def normalize_sql(self, sql: str) -> str:
        sql = self._outside_quotes(sql, lambda s: re.sub(r"\s+", " ", s))
        return sql.strip()

    def db2_to_pg(self, sql: str) -> str:
        def fix(s: str) -> str:
            s = re.sub(r"\bCURRENT\s+TIMESTAMP\b", "CURRENT_TIMESTAMP", s, flags=re.I)
            s = re.sub(r"\bCURRENT\s+DATE\b", "CURRENT_DATE", s, flags=re.I)
            s = re.sub(r"\bCURRENT\s+TIME\b", "CURRENT_TIME", s, flags=re.I)
            s = re.sub(r"\bIDENTITY_VAL_LOCAL\s*\(\s*\)", "lastval()", s, flags=re.I)
            s = re.sub(r"\s+WITH\s+(UR|CS|RS|RR)\s*$", "", s, flags=re.I)
            return s
        return self._outside_quotes(sql, fix)

    def extract_hostvars(self, sql: str, counter_start: int, origin: str):
        """Replace :hv [:ind] by $n. Returns (sql, [(hv, ind)])."""
        found = []
        n = [counter_start]

        def repl(m):
            found.append((m.group(1), m.group(2)))
            n[0] += 1
            return f"${n[0]}"
        sql = self._outside_quotes(sql, lambda s: self.HV_RE.sub(repl, s))
        return sql, found

    def split_into_list(self, s: str, origin: str) -> list[tuple[str, str | None]]:
        out = []
        for part in s.split(","):
            m = re.fullmatch(self.HV_RE, part.strip())
            if not m:
                self.fail(origin, f"cannot parse INTO host variable list: {s}")
            out.append((m.group(1), m.group(2)))
        return out

    def bind_code(self, direction: int, hv: str, ind: str | None, origin: str) -> list[str]:
        t, scale, signed = host_type(hv, self.symbols, origin)
        out = [
            f"MOVE {direction * 1000000 + t * 10000 + scale * 100 + signed} TO GASQL-DESC",
            f"MOVE FUNCTION BYTE-LENGTH({hv}) TO GASQL-LEN",
            f"CALL STATIC 'GASQLBND' USING GASQL-DESC GASQL-LEN {hv}",
        ]
        if ind:
            it, iscale, isigned = host_type(ind, self.symbols, origin)
            out += [
                f"MOVE {direction * 1000000 + it * 10000 + iscale * 100 + isigned} TO GASQL-DESC",
                f"MOVE FUNCTION BYTE-LENGTH({ind}) TO GASQL-LEN",
                f"CALL STATIC 'GASQLIND' USING GASQL-DESC GASQL-LEN {ind}",
            ]
        return out

    def sql_exec(self, pg_sql: str, ins, outs, origin) -> list[str]:
        self.stmts.append(pg_sql)
        name = f"GASQL-STMT-{len(self.stmts):03d}"
        code = []
        for hv, ind in ins:
            code += self.bind_code(0, hv, ind, origin)
        for hv, ind in outs:
            code += self.bind_code(1, hv, ind, origin)
        code.append(f"CALL STATIC 'GASQLEXE' USING SQLCA {name}")
        return code

    def record_declare(self, sql: str, origin: str) -> bool:
        m = re.match(r"DECLARE\s+([A-Z0-9_-]+)\s+(.*?)\bCURSOR\b\s*(WITH\s+HOLD\s+)?FOR\s+(.*)$",
                     sql, re.I | re.S)
        if not m:
            return False
        name, attrs, hold, query = m.group(1), m.group(2).strip(), m.group(3), m.group(4)
        query = self.db2_to_pg(query)
        for_update = re.search(r"\bFOR\s+UPDATE\b", query, re.I)
        # PostgreSQL: FOR UPDATE OF takes table names (Db2: column names); and
        # an updatable cursor cannot be WITH HOLD. Both are equivalent here.
        query = re.sub(r"\bFOR\s+UPDATE\s+OF\s+.*$", "FOR UPDATE", query, flags=re.I | re.S)
        query = re.sub(r"\bFOR\s+(FETCH|READ)\s+ONLY\b", "", query, flags=re.I).strip()
        pg_query, ins = self.extract_hostvars(query, 0, origin)
        hold_txt = " WITH HOLD" if hold and not for_update else ""
        decl = f"DECLARE {name} {attrs + ' ' if attrs else ''}CURSOR{hold_txt} FOR {pg_query}"
        self.cursors[name.upper()] = (decl, ins)
        return True

    def sql(self, body: str, origin: str) -> list[str]:
        sql = self.normalize_sql(body)
        verb = sql.split(" ", 1)[0].upper()

        if verb == "DECLARE":
            if not self.record_declare(sql, origin):
                self.fail(origin, f"unsupported DECLARE: {sql}")
            return []

        if verb == "OPEN":
            name = sql.split()[1].upper()
            if name not in self.cursors:
                self.fail(origin, f"OPEN of undeclared cursor {name}")
            decl, ins = self.cursors[name]
            return self.sql_exec(decl, ins, [], origin)

        if verb == "CLOSE":
            return self.sql_exec(f"CLOSE {sql.split()[1]}", [], [], origin)

        if verb == "FETCH":
            m = re.match(r"FETCH\s+(?:(NEXT|PRIOR|FIRST|LAST)\s+)?(?:FROM\s+)?([A-Z0-9_-]+)\s+INTO\s+(.*)$",
                         sql, re.I)
            if not m:
                self.fail(origin, f"unsupported FETCH: {sql}")
            direction = (m.group(1) or "NEXT").upper()
            outs = self.split_into_list(m.group(3), origin)
            return self.sql_exec(f"FETCH {direction} FROM {m.group(2)}", [], outs, origin)

        if verb == "SELECT":
            m = re.match(r"SELECT\s+(.*?)\s+INTO\s+(.*?)\s+FROM\s+(.*)$", sql, re.I | re.S)
            if not m:
                self.fail(origin, f"unsupported SELECT (expected SELECT ... INTO ... FROM): {sql}")
            outs = self.split_into_list(m.group(2), origin)
            pg, ins = self.extract_hostvars(self.db2_to_pg(f"SELECT {m.group(1)} FROM {m.group(3)}"), 0, origin)
            return self.sql_exec(pg, ins, outs, origin)

        if verb == "SET":
            m = re.match(r"SET\s+(:[^=]+?)\s*=\s*(.*)$", sql, re.I)
            if not m:
                self.fail(origin, f"unsupported SET: {sql}")
            outs = self.split_into_list(m.group(1), origin)
            pg, ins = self.extract_hostvars(self.db2_to_pg(f"SELECT {m.group(2)}"), 0, origin)
            return self.sql_exec(pg, ins, outs, origin)

        if verb in ("INSERT", "UPDATE", "DELETE"):
            pg, ins = self.extract_hostvars(self.db2_to_pg(sql), 0, origin)
            return self.sql_exec(pg, ins, [], origin)

        self.fail(origin, f"EXEC SQL {verb} is not supported by the test runtime")
        return []

    # ---- program level --------------------------------------------------
    def translate(self) -> str:
        lines = expand_copybooks(read_fixed(self.source), self.copy_dirs)

        texts = [l.text for l in lines]
        upper = [t.upper() for t in texts]

        def find_line(pattern):
            for i, t in enumerate(upper):
                if re.search(pattern, t):
                    return i
            return -1

        pid = find_line(r"\bPROGRAM-ID\b")
        m = re.search(r"PROGRAM-ID\.?\s+([A-Z0-9-]+)", upper[pid]) if pid >= 0 else None
        if not m:
            raise TranslateError(f"{self.source}: PROGRAM-ID not found")
        self.program_id = m.group(1)

        proc = find_line(r"^\s*PROCEDURE\s+DIVISION\b")
        if proc < 0:
            raise TranslateError(f"{self.source}: PROCEDURE DIVISION not found")
        if re.search(r"\bUSING\b", upper[proc]):
            raise TranslateError(f"{lines[proc].origin}: PROCEDURE DIVISION USING is not expected in a CICS program")

        # Pull every EXEC ... END-EXEC block out as one unit.
        blocks = []          # (start_line, end_line, prefix, kind, body, suffix)
        i = 0
        while i < len(texts):
            m = re.search(r"\bEXEC\s+(CICS|SQL)\b", texts[i], re.I)
            if not m:
                if re.search(r"\bEXEC\s*$", texts[i], re.I) and i + 1 < len(texts) and \
                        re.match(r"\s*(CICS|SQL)\b", texts[i + 1], re.I):
                    raise TranslateError(f"{lines[i].origin}: EXEC split from CICS/SQL keyword")
                i += 1
                continue
            prefix = texts[i][:m.start()]
            j = i
            buf = texts[i][m.end():]
            while not re.search(r"\bEND-EXEC\b", buf, re.I):
                j += 1
                if j >= len(texts):
                    raise TranslateError(f"{lines[i].origin}: EXEC without END-EXEC")
                buf += "\n" + texts[j]
            em = re.search(r"\bEND-EXEC\b", buf, re.I)
            body, suffix = buf[:em.start()], buf[em.end():]
            blocks.append((i, j, prefix, m.group(1).upper(), body, suffix))
            i = j + 1

        # Symbol table from the DATA DIVISION, without embedded EXEC blocks.
        data_start = find_line(r"^\s*DATA\s+DIVISION\b")
        data_text = []
        skip = {k for b in blocks for k in range(b[0], b[1] + 1)}
        for k in range(data_start, proc):
            if k not in skip:
                data_text.append(texts[k])
        self.symbols = parse_data_division("\n".join(data_text))

        # Record cursor declarations first (they may come after their OPEN).
        for (s, e, prefix, kind, body, suffix) in blocks:
            if kind == "SQL" and re.match(r"\s*DECLARE\b", body, re.I):
                if not self.record_declare(self.normalize_sql(body), lines[s].origin):
                    raise TranslateError(f"{lines[s].origin}: unsupported DECLARE")

        out: list[str] = []
        block_at = {b[0]: b for b in blocks}
        k = 0
        while k < len(texts):
            if k in block_at:
                s, e, prefix, kind, body, suffix = block_at[k]
                origin = lines[s].origin
                in_proc = s > proc
                if not in_proc:
                    if kind == "SQL" and re.match(r"\s*DECLARE\b", body, re.I):
                        k = e + 1
                        continue
                    raise TranslateError(f"{origin}: EXEC {kind} in DATA DIVISION is not supported: {body.strip()}")
                if prefix.strip():
                    out.append(prefix.rstrip())
                if kind == "SQL" and re.match(r"\s*DECLARE\b", body, re.I):
                    stmts = []
                else:
                    stmts = self.cics(body, origin) if kind == "CICS" else self.sql(body, origin)
                if not stmts and suffix.strip() == ".":
                    stmts = ["CONTINUE"]
                stmts = close_calls(stmts)
                for st in stmts:
                    out.extend(wrap(st))
                if suffix.strip():
                    out.append("    " + suffix.strip())
                k = e + 1
                continue
            t = texts[k]
            if k > proc:
                t = re.sub(r"DFHRESP\s*\(\s*([A-Z0-9]+)\s*\)",
                           lambda m: self._dfhresp(m.group(1), lines[k].origin), t, flags=re.I)
            out.append(t)
            k += 1

        return self.assemble(out)

    def _dfhresp(self, name: str, origin: str) -> str:
        v = DFHRESP.get(name.upper())
        if v is None:
            raise TranslateError(f"{origin}: unknown DFHRESP({name})")
        return str(v)

    def assemble(self, body: list[str]) -> str:
        eib = [l.text for l in read_fixed(os.path.join(RUNTIME_DIR, "CICSEIB.cpy"))]
        link_eib = [re.sub(r"\b(EIB[A-Z0-9]+)\b", r"CLE-\1", t) for t in eib]

        ws = [
            "01  CICS-WORK.",
            "    05 CICS-PROGRAM           PIC X(8).",
            "    05 CICS-FILE              PIC X(8).",
            "    05 CICS-QUEUE             PIC X(8).",
            "    05 CICS-ABCODE            PIC X(4).",
            "    05 CICS-OP                PIC X(4).",
            "    05 CICS-QTYPE             PIC X(2).",
            "    05 CICS-LEN               PIC S9(9) COMP-5.",
            "    05 CICS-KEYLEN            PIC S9(9) COMP-5.",
            "    05 CICS-RESP              PIC S9(9) COMP-5.",
            "    05 CICS-RESP2             PIC S9(9) COMP-5.",
            "    05 CICS-FLAG              PIC S9(9) COMP-5.",
            "    05 CICS-NOW               PIC X(21).",
            "    05 CICS-NO-COMMAREA       PIC X(1).",
            "    05 GASQL-DESC             PIC S9(9) COMP-5.",
            "    05 GASQL-LEN              PIC S9(9) COMP-5.",
            "01  CICS-LINK-EIB.",
        ] + link_eib
        for n, sql in enumerate(self.stmts, 1):
            ws.append(f"01  GASQL-STMT-{n:03d}.")
            rest = sql
            while rest:
                take = min(30, len(rest))
                while len(rest[:take].replace("'", "''")) > 30:
                    take -= 1
                chunk, rest = rest[:take], rest[take:]
                ws.append(f"    05 FILLER PIC X({len(chunk)}) VALUE {cobol_literal(chunk)}.")
            ws.append("    05 FILLER PIC X VALUE X'00'.")

        out: list[str] = []
        ws_done = link_done = False
        upper = [t.upper() for t in body]
        has_linkage = any(re.match(r"^\s*LINKAGE\s+SECTION", t) for t in upper)
        for t, u in zip(body, upper):
            if re.match(r"^\s*PROCEDURE\s+DIVISION\b", u):
                if not ws_done:
                    raise TranslateError(f"{self.source}: no WORKING-STORAGE SECTION")
                if not has_linkage:
                    out += ["LINKAGE SECTION.", "01  DFHEIBLK."] + eib
                    link_done = True
                has_ca = "DFHCOMMAREA" in self.symbols
                using = "DFHEIBLK DFHCOMMAREA" if has_ca else "DFHEIBLK"
                out.append(f"PROCEDURE DIVISION USING {using}.")
                continue
            out.append(t)
            if re.match(r"^\s*WORKING-STORAGE\s+SECTION", u):
                out += ws
                ws_done = True
            if re.match(r"^\s*LINKAGE\s+SECTION", u):
                out += ["01  DFHEIBLK."] + eib
                link_done = True
        if not link_done:
            raise TranslateError(f"{self.source}: could not place DFHEIBLK")

        result = []
        for t in out:
            if len(t) > LINE_MAX:
                raise TranslateError(f"{self.source}: line exceeds column 72 after translation: {t.strip()}")
            result.append("       " + t if t.strip() else "")
        return "\n".join(result) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("source")
    ap.add_argument("output")
    ap.add_argument("--copy-dir", action="append", default=[])
    args = ap.parse_args(argv)
    copy_dirs = args.copy_dir or [os.path.dirname(os.path.abspath(args.source))]
    try:
        text = Translator(args.source, copy_dirs).translate()
    except TranslateError as e:
        print(f"precompile: {e}", file=sys.stderr)
        return 1
    with open(args.output, "w", encoding="latin-1") as f:
        f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
