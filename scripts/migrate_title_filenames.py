#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path

from biblioteca_ideias.storage import MarkdownStore


def snapshot(store: MarkdownStore) -> dict[str, tuple[str, int]]:
    return {
        idea.idea_id: (idea.content_hash, idea.path.stat().st_mtime_ns)
        for idea in store.list_ideas()
    }


def migrate_and_validate(vault: Path, state: Path) -> dict[str, int | bool]:
    store = MarkdownStore(vault, state)
    before = snapshot(store)
    plan = store.plan_title_migration()
    store.apply_title_migration(plan)
    after = snapshot(store)
    return {
        "ideas": len(after),
        "renamed": len(plan),
        "same_ids": before.keys() == after.keys(),
        "same_content_and_timestamps": before == after,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Migra nomes UUID para títulos com validação.")
    parser.add_argument("vault", type=Path)
    parser.add_argument("--state", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--simulate", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    if args.simulate:
        with tempfile.TemporaryDirectory(prefix="biblioteca-migracao-") as temporary:
            copy = Path(temporary) / "cofre"
            shutil.copytree(args.vault, copy, ignore=shutil.ignore_patterns(".git"))
            result = migrate_and_validate(copy, Path(temporary) / "estado")
    else:
        result = migrate_and_validate(args.vault, args.state)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["same_ids"] and result["same_content_and_timestamps"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
