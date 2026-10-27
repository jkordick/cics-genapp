#!/usr/bin/env python3
"""Turn test/results/junit.xml into a Markdown summary (for $GITHUB_STEP_SUMMARY)."""
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict


def main(path: str) -> int:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as e:
        print(f"## GenApp tests\n\nNo test results ({e}). See the job log: the build probably failed.")
        return 0

    groups: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    for case in root.iter("testcase"):
        module = case.get("classname", "").rsplit(".", 1)[-1]
        node = case.find("failure")
        if node is None:
            node = case.find("error")
        if node is not None:
            message = (node.get("message") or "").strip()
            status, detail = "❌", message.splitlines()[0][:150] if message else ""
        elif case.find("skipped") is not None:
            status, detail = "⏭️", ""
        else:
            status, detail = "✅", ""
        groups[module].append((status, case.get("name", ""), detail))

    total = sum(len(v) for v in groups.values())
    failed = sum(1 for v in groups.values() for s, _, _ in v if s == "❌")
    if total == 0:
        headline = "❌ no tests ran"
    elif failed == 0:
        headline = "✅ all passed"
    else:
        headline = f"❌ {failed} failed"
    print(f"## GenApp off-mainframe tests: {total} tests, {headline}\n")
    print("COBOL programs from `base/src` were compiled with GnuCOBOL and run against "
          "PostgreSQL with emulated CICS commands. See `test/README.md`.\n")
    for module, cases in sorted(groups.items()):
        print(f"### {module}\n")
        print("| | Test | Details |")
        print("|---|---|---|")
        for status, name, detail in cases:
            detail = detail.replace("|", "\\|")
            print(f"| {status} | `{name}` | {detail} |")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "test/results/junit.xml"))
