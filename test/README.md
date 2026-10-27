# Off-mainframe tests for GenApp

These tests run the real GenApp COBOL programs from `base/src` on Linux,
without a mainframe, CICS or Db2. They exist so that changes to the COBOL
(for example a new policy type) can be checked automatically, locally and in
GitHub Actions.

```
test/run.sh                 # build everything and run all tests (first run also builds the image)
test/run.sh -k MOT          # only Motor tests (extra arguments go to pytest)
test/run.sh --shell         # shell in the test container, programs built and DB ready
test/run.sh --setup-only    # build only, no tests
```

You need **Podman** (or Docker; set `CONTAINER_ENGINE=docker` to force it).
Everything else (GnuCOBOL, PostgreSQL, Python) lives in the container image
built from `test/Containerfile`. Results are written to `test/results/junit.xml`.

## How it works

1. **Translate.** `tools/precompile.py` does what the CICS and Db2
   precompilers do on z/OS: it replaces every `EXEC CICS ... END-EXEC` and
   `EXEC SQL ... END-EXEC` block with ordinary COBOL `CALL`s. The GenApp
   sources are not changed; translated copies go to `test/build/cob/`.
2. **Compile.** GnuCOBOL compiles each program into a module
   (`test/build/LGxxxxxx.so`). The compile options match GenApp's z/OS build
   (`base/cntl/cobol.jcl`), in particular the default `TRUNC(STD)` behaviour.
3. **Run.** `test/build/cicsrun` plays the part of CICS for one transaction:
   it loads a program, passes it a COMMAREA, and handles its CICS and SQL
   calls via a small C runtime (`runtime/genapprt.c`):

   | On the mainframe | In the tests |
   |---|---|
   | Db2 tables from `base/cntl/db2cre.jcl` | PostgreSQL tables generated from the same JCL (`tools/db2ddl.py`) |
   | VSAM files `KSDSCUST`, `KSDSPOLY` | one file per record in a temporary directory |
   | TS / TD queues (e.g. `CSMT` error log) | append-only text files |
   | `LINK`, `RETURN`, `ABEND`, `SYNCPOINT` | real calls between the compiled programs; an abend rolls back the database |
   | `GET COUNTER` (named counter server) | reports "not available", so GenApp uses its Db2 fallback |

4. **Test.** `cases/` holds pytest tests. They build a COMMAREA using the
   field names from `base/src/lgcmarea.cpy` (read at run time, so new fields
   are picked up automatically), call a program, and check the return code,
   the COMMAREA, the database rows and the VSAM records.

Any SQL statement or CICS command the harness does not support stops the
build with a clear error. It never silently skips anything.

## Writing tests

```python
def test_example(genapp, db, customer):          # fixtures from cases/conftest.py
    ca = genapp.commarea(request_id="01IMOT", customer_num=customer, policy_num=1)
    r = genapp.call("LGIPOL01", ca)              # optional calen=... for short COMMAREAs
    assert r.abend is None, r.log                # r.log = program output + CICS/SQL trace
    assert r.return_code == "00"
    assert r.ca["CA-M-MAKE"] == "Ford"           # COMMAREA fields by copybook name
    db.execute("SELECT ... FROM motor ...")      # PostgreSQL, same tables as Db2
    genapp.vsam("KSDSPOLY", key)                 # VSAM record bytes, or None
    genapp.queue("TD", "CSMT")                   # lines written to a queue
```

Keyword arguments to `genapp.commarea()` are COMMAREA field names without the
`CA-` prefix, lower case, with `_` for `-`: `m_make` sets `CA-M-MAKE`.

When an assertion on a result fails, pytest shows `r.log`: every CICS
command and SQL statement the programs ran, with SQLCODEs and PostgreSQL
error messages. Start there when debugging.

### Adding a policy type

`cases/test_policies.py` runs add / inquire / update / delete tests for each
entry in `POLICY_TYPES`. A new policy type needs one new entry. Use
`update={}` and `delete=False` until those operations are implemented.
Add extra tests for anything specific to the new type.

## Known GenApp behaviour (not harness bugs)

The harness runs GenApp as written, including a few quirks that also happen
on the mainframe:

- `LGUPDB01` sets return code `02` (timestamp mismatch) or `01` (not found)
  and then resets it to `00` in `CLOSE-PCURSOR`. The data is correctly left
  unchanged, but the caller sees `00` (and `01` ends in abend `LGV3`). The
  tests that expect the correct codes are marked `xfail(strict=True)`. If
  you fix the bug, remove the `GENAPP_UPDATE_RC_BUG` marker.
- `LGUPOL01` sets `99` for an unknown request ID but still calls `LGUPDB01`.
- `LGAPOL01` and `LGDPOL01` only check for the 28-byte COMMAREA header; the
  per-policy-type length check is in `LGAPDB01`.
- Commercial policies cannot be updated.

## Limits

- Not covered: 3270 screens (`lgtestp*`, BMS maps), web services
  (`lgwebst5`, `wsa*`), `lgsetup`, `lgastat1`, and the browse-based
  `lgicvs01` / `lgipvs01`. See the `EXCLUDE` list in `build.sh`.
- PostgreSQL is not Db2. Common SQL, SQLCODEs (`100`, `-803`, `-530`,
  `-811`, ...) and the Db2 timestamp format are emulated, but Db2-specific
  behaviour such as locking, isolation levels and EBCDIC sorting is not.
- Passing these tests is strong evidence, not proof, that a change works on
  z/OS. Run the real build and an integration test on the mainframe before
  production.
