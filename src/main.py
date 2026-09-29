"""CLI for the reproducible dependency-upgrade comparison."""

import argparse
import sys

from .report import terminal_text


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description="ProofRun: investigate dependency upgrades with reproducible tests.")
    commands = cli.add_subparsers(dest="command", required=True)
    upgrade = commands.add_parser("upgrade-demo", help="Compare Pydantic versions on one customer-import behavior.")
    mode = upgrade.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="Use curated source/probe data; requires cached Docker images.")
    mode.add_argument("--prepare", action="store_true", help="Only build the two demo images; downloads pinned dependencies without API keys.")
    upgrade.add_argument("--rebuild", action="store_true", help="Rebuild the two pinned dependency images.")
    return cli


def progress(message: str) -> None:
    print(terminal_text(message), file=sys.stderr, flush=True)


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    from .upgrade_demo import UpgradeError, run_demo
    try:
        return run_demo(offline=args.offline, rebuild=args.rebuild, prepare=args.prepare)
    except (UpgradeError, ValueError) as exc:
        progress(f"Error: {exc}")
        return 2
    except KeyboardInterrupt:
        progress("Interrupted; comparison incomplete.")
        return 130
    except Exception as exc:
        progress(f"Error: unexpected {type(exc).__name__}; comparison incomplete.")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
