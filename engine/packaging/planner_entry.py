"""PyInstaller entry point: one binary for the engine server, the MCP bridge and the CLI."""

import multiprocessing
import sys

from planner_engine.cli import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
