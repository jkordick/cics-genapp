# Copilot instructions for GenApp

GenApp is IBM's sample CICS insurance application: COBOL programs using CICS,
Db2 and VSAM. It normally runs on z/OS. This repository also contains an
off-mainframe test harness in `test/` that compiles and runs the programs on
Linux, so changes can be verified without a mainframe.

## Layout

- `base/src/` — COBOL programs and copybooks.
  - Program names: `LG` + action (`A`dd, `I`nquire, `U`pdate, `D`elete) +
    entity (`CUS` customer, `POL` policy) + layer:
    - `01`: business logic, the entry point that validates the COMMAREA
    - `DB01`: Db2 access
    - `VS01`: VSAM access

    Example: `LGAPOL01` → `LGAPDB01` → `LGAPVS01`.
  - `lgcmarea.cpy`: the COMMAREA, the interface for every program. Policy
    types share `CA-POLICY-SPECIFIC` via `REDEFINES` (`CA-ENDOWMENT`,
    `CA-HOUSE`, `CA-MOTOR`, `CA-COMMERCIAL`); each must stay exactly the
    size of that area (pad with a `FILLER`).
  - `lgpolicy.cpy`: Db2 host variables and the `WS-*-LEN` /
    `WS-FULL-*-LEN` length constants used for COMMAREA length checks.
- `base/cntl/db2cre.jcl`: Db2 DDL (tables, indexes, grants, sample data).
  The test harness generates its PostgreSQL schema from this file, so new
  tables must be added here.
- `test/`: off-mainframe test harness. Read `test/README.md` before
  changing or adding tests.

## Conventions when changing COBOL

- Follow the existing code. For a new policy type, copy the **Motor**
  implementation (`01AMOT`, `01IMOT`, `01UMOT`, `01DMOT`) through every
  layer: COMMAREA, copybooks, DDL, and the `POL01` / `PDB01` / `PVS01`
  programs.
- Keep fixed-format COBOL: code in columns 8–72, comments with `*` in
  column 7. Match the surrounding indentation and comment style.
- Return codes: `00` ok, `01` not found, `02` timestamp mismatch,
  `70` unknown customer, `81`/`82` VSAM error, `90` SQL error,
  `98` COMMAREA too short, `99` unknown request ID.
- Don't change behaviour of existing policy types unless the task asks for
  it.

## Testing (required)

Run the tests before you finish, and make sure they pass:

```
test/run.sh
```

- To cover a new policy type, add an entry to `POLICY_TYPES` in
  `test/cases/test_policies.py`. Use `update={}` / `delete=False` if those
  operations are not implemented yet. Add specific tests for anything else in
  the acceptance criteria.
- If a test fails, its output includes `r.log`: every CICS command and SQL
  statement that ran, with SQLCODEs and PostgreSQL error messages.
- If the build reports an unsupported `EXEC CICS` / `EXEC SQL` construct,
  prefer the forms GenApp already uses. Only extend `test/tools/precompile.py`
  or `test/runtime/genapprt.c` if there is no reasonable alternative, and
  say so in the pull request.
- `test/README.md` lists known GenApp quirks (tests marked
  `xfail(strict=True)`). Don't treat those as harness bugs.
