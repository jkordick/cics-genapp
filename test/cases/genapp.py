"""Helpers for testing GenApp programs off-mainframe.

* ``Layout`` reads a COBOL copybook (base/src/lgcmarea.cpy) and knows the
  offset and length of every field, so tests build and read COMMAREAs by
  the same names the COBOL code uses.
* ``GenApp.call`` runs one program the way CICS would when it is LINKed to
  with a COMMAREA, and returns the updated COMMAREA (or the abend code).
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COPYBOOK_DIR = ROOT / "base" / "src"


@dataclass(frozen=True)
class Field:
    name: str
    offset: int
    length: int
    numeric: bool


class Layout:
    """Byte layout of a display-format COBOL record (PIC X / PIC 9, REDEFINES)."""

    def __init__(self, copybook: str | Path):
        self.fields: dict[str, Field] = {}
        self.size = 0
        self._parse(Path(copybook))

    @staticmethod
    def _pic_length(pic: str) -> tuple[int, bool]:
        expanded = re.sub(r"(.)\((\d+)\)", lambda m: m.group(1) * int(m.group(2)), pic.upper())
        expanded = expanded.replace("S", "").replace("V", "")
        if any(c not in "X9A" for c in expanded):
            raise ValueError(f"unsupported PICTURE {pic}")
        return len(expanded), "X" not in expanded and "A" not in expanded

    def _parse(self, path: Path):
        text = []
        for line in path.read_text(encoding="latin-1").splitlines():
            if len(line) > 6 and line[6] in "*/":
                continue
            text.append(line[7:72])
        entries = [e.strip() for e in re.split(r"\.\s", " ".join(text) + " ") if e.strip()]

        # (level, name, start offset, pic length or None)
        stack: list[list] = []
        offset = 0
        items: list[list] = []

        def close(level):
            nonlocal offset
            while stack and stack[-1][0] >= level:
                item = stack.pop()
                item[3] = offset - item[2]   # group size
                self.fields[item[1]] = Field(item[1], item[2], item[3], False)

        for entry in entries:
            toks = entry.split()
            if not toks[0].isdigit():
                continue
            level = int(toks[0])
            if level == 88:
                continue
            name = toks[1].upper()
            upper = [t.upper() for t in toks]
            redefines = upper[upper.index("REDEFINES") + 1] if "REDEFINES" in upper else None
            pic = toks[upper.index("PIC") + 1] if "PIC" in upper else None
            close(level)
            if redefines:
                offset = self.fields[redefines].offset
            if pic:
                length, numeric = self._pic_length(pic)
                self.fields[name] = Field(name, offset, length, numeric)
                offset += length
            else:
                item = [level, name, offset, None]
                stack.append(item)
                items.append(item)
            self.size = max(self.size, offset)
        close(0)


class Commarea:
    """A COMMAREA buffer with access by COBOL field name.

    ``ca["CA-M-MAKE"] = "Ford"`` writes a field; ``ca["CA-M-VALUE"]`` reads
    it back (text without trailing blanks, or an int for PIC 9 fields).
    """

    def __init__(self, layout: Layout, data: bytes | None = None, **fields):
        self.layout = layout
        self.buf = bytearray(data if data is not None else b" " * layout.size)
        for k, v in fields.items():
            self[k] = v

    def _field(self, name: str) -> Field:
        key = name.upper().replace("_", "-")
        if not key.startswith("CA-"):
            key = "CA-" + key
        try:
            return self.layout.fields[key]
        except KeyError:
            raise KeyError(f"{key} is not a field in the COMMAREA copybook") from None

    def __setitem__(self, name: str, value):
        f = self._field(name)
        if f.numeric:
            text = str(int(value)).rjust(f.length, "0")
            if len(text) > f.length:
                raise ValueError(f"{value} does not fit in {f.name} (PIC 9({f.length}))")
        else:
            text = str(value)
            if len(text) > f.length:
                raise ValueError(f"'{value}' is longer than {f.name} (PIC X({f.length}))")
            text = text.ljust(f.length)
        self.buf[f.offset:f.offset + f.length] = text.encode("latin-1")

    def __getitem__(self, name: str):
        f = self._field(name)
        raw = bytes(self.buf[f.offset:f.offset + f.length]).decode("latin-1")
        if f.numeric:
            return int(raw) if raw.strip().isdigit() else raw
        return raw.rstrip()

    def raw(self, name: str) -> str:
        f = self._field(name)
        return bytes(self.buf[f.offset:f.offset + f.length]).decode("latin-1")

    def copy(self) -> "Commarea":
        return Commarea(self.layout, bytes(self.buf))

    def __bytes__(self):
        return bytes(self.buf)

    @property
    def return_code(self) -> str:
        return self.raw("CA-RETURN-CODE")


@dataclass
class Result:
    ca: Commarea | None
    abend: str | None
    log: str             # program output plus the runtime trace of this call

    @property
    def return_code(self) -> str:
        if self.ca is None:
            raise AssertionError(f"program abended with code {self.abend}\n{self.log}")
        return self.ca.return_code


class GenApp:
    """Runs GenApp programs built by test/build.sh."""

    def __init__(self, build_dir: Path, data_dir: Path, layout: Layout):
        self.build_dir = Path(build_dir)
        self.data_dir = Path(data_dir)
        self.layout = layout
        self.trace = self.data_dir / "trace.log"

    def commarea(self, **fields) -> Commarea:
        return Commarea(self.layout, **fields)

    def call(self, program: str, ca: Commarea | bytes, calen: int | None = None) -> Result:
        data = bytes(ca)
        if calen is None:
            calen = len(data)
        with tempfile.TemporaryDirectory() as tmp:
            cin, cout = Path(tmp) / "in", Path(tmp) / "out"
            cin.write_bytes(data)
            env = dict(os.environ,
                       GENAPP_PROGRAM=program.upper(),
                       GENAPP_COMMAREA_IN=str(cin),
                       GENAPP_COMMAREA_OUT=str(cout),
                       GENAPP_CALEN=str(calen),
                       GENAPP_DATA=str(self.data_dir),
                       GENAPP_TRACE=str(self.trace),
                       COB_LIBRARY_PATH=str(self.build_dir))
            start = self.trace.stat().st_size if self.trace.exists() else 0
            p = subprocess.run([str(self.build_dir / "cicsrun")], env=env, capture_output=True,
                               text=True, timeout=60)
            with open(self.trace, encoding="latin-1") as f:
                f.seek(start)
                trace = f.read()
            # Shown by pytest when an assertion on the result fails.
            log = f"{p.stdout}{p.stderr}--- runtime trace (CICS/SQL calls) ---\n{trace}"
            if p.returncode == 16:
                m = re.search(r"GENAPP-ABEND (\S+)", p.stderr)
                return Result(None, m.group(1) if m else "????", log)
            if p.returncode != 0:
                raise RuntimeError(f"{program} failed with exit code {p.returncode}:\n{log}")
            out = cout.read_bytes()
            full = out + data[len(out):]
            return Result(Commarea(self.layout, full), None, log)

    # --- side effects --------------------------------------------------
    def queue(self, kind: str, name: str) -> list[str]:
        """Lines written to a TS ('TS') or TD ('TD') queue."""
        p = self.data_dir / "queues" / f"{kind}-{name}.log"
        return p.read_text(encoding="latin-1").splitlines() if p.exists() else []

    def vsam(self, file: str, key: str) -> bytes | None:
        """Record stored in a VSAM KSDS file under its key, or None."""
        p = self.data_dir / "vsam" / file / key.encode("latin-1").hex()
        return p.read_bytes() if p.exists() else None
