"""Print the MCP connector document for the status tools."""

from __future__ import annotations

import argparse
import json
import os
import sys

from hermes_status_board import __version__
from hermes_status_board.connector import connector_document


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="hermes-status-board")
    parser.add_argument("command", choices=("connector",))
    parser.add_argument("--resource", default="")
    args = parser.parse_args(argv)
    resource = args.resource or os.environ.get("HERMES_BRIDGE_RESOURCE", "")
    if not resource:
        print("set HERMES_BRIDGE_RESOURCE or pass --resource", file=sys.stderr)
        raise SystemExit(2)
    try:
        document = connector_document(resource)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    print(f"hermes-status-board {__version__}")
    print(json.dumps(document, indent=2))
