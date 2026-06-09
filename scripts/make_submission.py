"""Build a competition submission zip from ogc_solver."""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUBMISSION_ROOT = ROOT / "ogc_solver"
DEFAULT_OUTPUT = ROOT / ".codex_workspace" / "dist" / "ogc_solver_submission.zip"

EXCLUDED_PARTS = {"__pycache__", ".pytest_cache"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}


def should_include(path: Path) -> bool:
    if any(part in EXCLUDED_PARTS for part in path.parts):
        return False
    return path.suffix not in EXCLUDED_SUFFIXES


def build_zip(output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(SUBMISSION_ROOT.rglob("*")):
            if path.is_file() and should_include(path):
                archive.write(path, path.relative_to(SUBMISSION_ROOT))
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="zip path to create",
    )
    args = parser.parse_args()

    output = build_zip(args.output.resolve())
    print(output)


if __name__ == "__main__":
    main()
