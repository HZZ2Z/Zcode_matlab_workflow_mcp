import argparse
import os

from . import engine
from .server import mcp


def main():
    ap = argparse.ArgumentParser(description="MATLAB MCP server (ZCode)")
    ap.add_argument("--workdir", default=os.environ.get("MATLAB_MCP_WORKDIR"),
                    help="MATLAB start directory (default: launch cwd)")
    args = ap.parse_args()
    if args.workdir:
        engine.set_workdir(args.workdir)
    engine.warmup()
    mcp.run()


if __name__ == "__main__":
    main()
