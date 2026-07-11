#!/usr/bin/env python3
"""Compatibility entrypoint for the packaged Basecamp Bench CLI."""

from basecamp_bench.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
