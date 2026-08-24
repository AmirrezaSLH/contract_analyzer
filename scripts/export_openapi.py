#!/usr/bin/env python3
"""Write the OpenAPI document to docs/openapi.json.

That file is the connector specification for assignment §3.3, so it is
committed rather than generated on demand: a third party integrating against it
should be reading the same bytes the reviewer did, and a diff on it is the
review of an API change.

    python scripts/export_openapi.py            # write docs/openapi.json
    python scripts/export_openapi.py --check    # fail if it is out of date
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from contract_analyzer.api.main import create_app  # noqa: E402

TARGET = ROOT / "docs" / "openapi.json"


def document() -> str:
    return json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="verify rather than write")
    parser.add_argument("--out", type=Path, default=TARGET)
    args = parser.parse_args(argv)

    spec = document()
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        if current != spec:
            print(f"{args.out} is out of date: python scripts/export_openapi.py", file=sys.stderr)
            return 1
        print(f"{args.out} is up to date")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(spec, encoding="utf-8")
    operations = sum(len(methods) for methods in json.loads(spec)["paths"].values())
    print(f"wrote {args.out} ({operations} operations)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
