"""Command line for the harvester. Example:

    uv run --env-file .env python -m harvester --region Niederbayern [--dry-run] [--live]
"""
import argparse
import logging
import os
import sys
from pathlib import Path

from . import pipeline

log = logging.getLogger("harvester")


def main(argv=None) -> int:
    """Read the command-line options and the settings, then run the pipeline.

    Args:
        argv: the options as a list; None means "take them from the command line". Tests pass a list.

    Returns:
        The exit code: 0 ok, 1 if the portal or the database step failed, 2 if a setting is missing,
        130 if stopped with Ctrl+C.
    """
    ap = argparse.ArgumentParser(prog="harvester", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", help="Regierungsbezirk, e.g. Niederbayern")
    ap.add_argument("--district", help="Landkreis, e.g. Kelheim")
    ap.add_argument("--ags", nargs="+", help="explicit municipality keys")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--portal-file", type=Path, help="use a saved gemeinden.json instead of the live portal")
    ap.add_argument("--max-pages", type=int, default=100, help="pages per municipality, 0 = no limit")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between requests to the same host")
    ap.add_argument("--workers", type=int, default=8, help="municipalities crawled in parallel (hosts stay polite)")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--out-dir", type=Path, default=Path("data/runs"))
    ap.add_argument("--dry-run", action="store_true", help="scrape + write run files, skip the database")
    ap.add_argument("--live", action="store_true", help="show the run step by step in the browser, then the dashboard")
    ap.add_argument("--port", type=int, default=8765, help="port of the --live page (localhost only)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    # settings come from the environment (.env), never from the code
    ua = os.environ.get("HARVESTER_USER_AGENT")
    if not ua:
        log.error("HARVESTER_USER_AGENT is not set (needs a contact address, see .env.example)")
        return 2
    db_url = os.environ.get("DATABASE_URL")
    if not args.dry_run and not db_url:
        log.error("DATABASE_URL is not set (or use --dry-run)")
        return 2
    try:
        return pipeline.run(args, ua, db_url)
    except KeyboardInterrupt:
        log.error("stopped with Ctrl+C; this run was not stored")
        return 130


if __name__ == "__main__":
    sys.exit(main())
