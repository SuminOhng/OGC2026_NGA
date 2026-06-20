"""Build the optional native OGC fast solver.

On the competition server target, run this on Ubuntu 24.04 and place the
resulting extensionless binary inside the submission package:

    python scripts/build_cpp_accelerator.py --submission

On Windows this script builds a local smoke-test executable under
``.codex_workspace/cpp`` unless ``--submission`` is provided.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "ogc_solver" / "ogc_solver" / "cpp_accel" / "fast_solver.cpp"
SUBMISSION_BINARY = ROOT / "ogc_solver" / "ogc_solver" / "cpp_accel" / "ogc_fast_solver"
LOCAL_BINARY = ROOT / ".codex_workspace" / "cpp" / (
    "ogc_fast_solver.exe" if platform.system().lower().startswith("win") else "ogc_fast_solver"
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--submission", action="store_true", help="write the bundled submission binary path")
    parser.add_argument("--cxx", default=os.environ.get("CXX", "g++"), help="C++ compiler command")
    args = parser.parse_args()

    if args.submission and platform.system().lower().startswith("win"):
        raise SystemExit("Submission binary must be built on Ubuntu/Linux, not Windows.")

    output = SUBMISSION_BINARY if args.submission else LOCAL_BINARY

    cxx = shutil.which(args.cxx) or args.cxx
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        cxx,
        "-std=c++17",
        "-O3",
        "-DNDEBUG",
        "-pthread",
        str(SOURCE),
        "-o",
        str(output),
    ]
    subprocess.run(command, check=True)
    print(output.resolve())


if __name__ == "__main__":
    main()
