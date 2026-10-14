# -*- coding: utf-8 -*-
"""Versioned fin migrations, independent of AgentScope's public migrations.

The container runs ``python -m finagent.migrate`` (upgrade head) before the API.
Use ``--help`` for maintenance commands. Legacy databases require an explicit,
verified baseline stamp; they are never silently adopted at startup.
"""

import argparse
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from sqlalchemy.exc import SQLAlchemyError

BASELINE = "0001_baseline"


def make_config(script_location: Path | None = None) -> Config:
    config = Config()
    location = script_location or Path(__file__).parent / "migrations"
    # ConfigParser interpolates percent signs, even in filesystem paths.
    config.set_main_option("script_location", str(location.resolve()).replace("%", "%%"))
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation")
    upgrade = commands.add_parser("upgrade", help="Apply revisions (default: head)")
    upgrade.add_argument("revision", nargs="?", default="head")
    commands.add_parser("current", help="Show the fin database revision")
    commands.add_parser("history", help="Show available fin revisions")
    revision = commands.add_parser("revision", help="Create a handwritten migration")
    revision.add_argument("-m", "--message", required=True)
    downgrade = commands.add_parser("downgrade", help="Revert a later revision; baseline is retained")
    downgrade.add_argument("revision")
    stamp = commands.add_parser("stamp-baseline", help="Adopt a verified legacy schema without changing its tables")
    stamp.add_argument(
        "--verified", action="store_true", required=True,
        help="Confirm backup and full schema comparison with a fresh baseline (see README)",
    )
    args = parser.parse_args(argv)
    operation = args.operation or "upgrade"
    config = make_config()
    config.attributes["operation"] = operation
    logging.basicConfig(level=logging.INFO)
    try:
        if operation == "upgrade":
            command.upgrade(config, getattr(args, "revision", "head"))
        elif operation == "current":
            command.current(config, verbose=True)
        elif operation == "history":
            command.history(config, verbose=True)
        elif operation == "revision":
            command.revision(config, message=args.message)
        elif operation == "downgrade":
            command.downgrade(config, args.revision)
        elif operation == "stamp-baseline":
            command.stamp(config, BASELINE)
    except SQLAlchemyError as exc:
        # Driver errors can contain connection details or SQL parameters.
        parser.exit(1, f"fin migration failed ({type(exc).__name__}); check database connectivity and migration prerequisites.\n")
    except (CommandError, RuntimeError) as exc:
        parser.exit(1, f"fin migration failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
