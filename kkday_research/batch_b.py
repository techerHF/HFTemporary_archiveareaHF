from __future__ import annotations

import sys

import run

run.BATCH_NAME = "B"
run.BATCH_START = 60
run.BATCH_END = 118

if __name__ == "__main__":
    sys.exit(run.main())
