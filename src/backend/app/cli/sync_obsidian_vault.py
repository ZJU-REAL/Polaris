"""Synchronize an auto-idea Obsidian vault into a Polaris project library."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import uuid
from pathlib import Path

from app.core.db import dispose_engine, get_sessionmaker
from app.services.obsidian_vault_sync import sync_obsidian_vault


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vault", type=Path, help="path to the auto-idea Obsidian vault")
    parser.add_argument("--project-id", type=uuid.UUID, required=True)
    parser.add_argument("--library-name", help="defaults to the project name")
    parser.add_argument("--dry-run", action="store_true", help="inspect without changing data")
    parser.add_argument("--overwrite-wiki", action="store_true")
    parser.add_argument("--skip-files", action="store_true", help="do not copy PDF/TXT/figures")
    parser.add_argument("--skip-chunks", action="store_true", help="do not build text chunks")
    parser.add_argument("--batch-size", type=int, default=25)
    return parser


async def _run(args: argparse.Namespace) -> None:
    try:
        async with get_sessionmaker()() as session:
            stats = await sync_obsidian_vault(
                session,
                vault_path=args.vault,
                project_id=args.project_id,
                library_name=args.library_name,
                dry_run=args.dry_run,
                overwrite_wiki=args.overwrite_wiki,
                copy_files=not args.skip_files,
                index_chunks=not args.skip_chunks,
                batch_size=args.batch_size,
            )
            print(json.dumps(stats.as_dict(), ensure_ascii=False, indent=2))
    finally:
        await dispose_engine()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    main()
