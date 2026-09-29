"""One-off/idempotent migration for context fields in preview records."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from labeler.context_preview import strip_context_hashtags


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    changed = 0
    fields_changed = 0
    for path in args.directory.glob("*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        record_changed = False
        for field in ("context", "context_backup"):
            if field not in value:
                continue
            cleaned = strip_context_hashtags(value[field])
            if cleaned != value[field]:
                value[field] = cleaned
                fields_changed += 1
                record_changed = True
        if record_changed:
            temp = path.with_suffix(".migration.tmp")
            temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(path)
            changed += 1
    print(f"records_changed={changed} fields_changed={fields_changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
