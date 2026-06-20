"""Build a competition submission zip from ogc_solver."""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUBMISSION_ROOT = ROOT / "ogc_solver"
DEFAULT_OUTPUT = ROOT / ".codex_workspace" / "dist" / "ogc_solver_submission.zip"
NATIVE_BINARY = SUBMISSION_ROOT / "ogc_solver" / "cpp_accel" / "ogc_fast_solver"

EXCLUDED_PARTS = {"__pycache__", ".pytest_cache"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".exe"}
EXECUTABLE_NAMES = {
    Path("ogc_solver") / "cpp_accel" / "ogc_fast_solver",
}
MINIMAL_CPP_PATHS = {
    Path("myalgorithm.py"),
    Path("README.md"),
    Path("config") / "default.json",
    Path("config") / "README.md",
    Path("ogc_solver") / "__init__.py",
    Path("ogc_solver") / "solver.py",
    Path("ogc_solver") / "cpp_accelerator.py",
    Path("ogc_solver") / "state.py",
    Path("ogc_solver") / "cpp_accel" / "fast_solver.cpp",
    Path("ogc_solver") / "cpp_accel" / "ogc_fast_solver",
}


def should_include(
    path: Path,
    relative_path: Path,
    *,
    include_windows_exe: bool = False,
    minimal_cpp: bool = False,
) -> bool:
    if any(part in EXCLUDED_PARTS for part in path.parts):
        return False
    if include_windows_exe and path.name == "ogc_fast_solver.exe":
        return True
    if minimal_cpp and relative_path not in MINIMAL_CPP_PATHS:
        return False
    return path.suffix not in EXCLUDED_SUFFIXES


def unix_mode_for_archive(relative_path: Path) -> int:
    if relative_path in EXECUTABLE_NAMES:
        return 0o100755
    return 0o100644


def write_file_with_unix_mode(archive: zipfile.ZipFile, path: Path, relative_path: Path) -> None:
    info = zipfile.ZipInfo.from_file(path, arcname=relative_path.as_posix())
    info.create_system = 3
    info.external_attr = unix_mode_for_archive(relative_path) << 16
    with path.open("rb") as handle:
        archive.writestr(info, handle.read(), compress_type=zipfile.ZIP_DEFLATED)


def build_zip(output: Path, *, include_windows_exe: bool = False, minimal_cpp: bool = False) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(SUBMISSION_ROOT.rglob("*")):
            if not path.is_file():
                continue
            relative_path = path.relative_to(SUBMISSION_ROOT)
            if should_include(
                path,
                relative_path,
                include_windows_exe=include_windows_exe,
                minimal_cpp=minimal_cpp,
            ):
                write_file_with_unix_mode(archive, path, relative_path)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="zip path to create",
    )
    parser.add_argument(
        "--require-native",
        action="store_true",
        help="fail if the bundled Linux C++ solver binary is missing",
    )
    parser.add_argument(
        "--include-windows-exe",
        action="store_true",
        help="include ogc_fast_solver.exe for local Windows smoke-test archives",
    )
    parser.add_argument(
        "--minimal-cpp",
        action="store_true",
        help="include only the C++ solver, Python entrypoint/bridge, and safety fallbacks",
    )
    args = parser.parse_args()

    if not NATIVE_BINARY.is_file():
        message = (
            f"warning: native solver binary is missing: {NATIVE_BINARY}. "
            "Build it on Linux with `python scripts/build_cpp_accelerator.py --submission` "
            "before creating the final competition zip."
        )
        if args.require_native:
            raise SystemExit(message.replace("warning:", "error:"))
        print(message, file=sys.stderr)

    output = build_zip(
        args.output.resolve(),
        include_windows_exe=args.include_windows_exe,
        minimal_cpp=args.minimal_cpp,
    )
    print(output)


if __name__ == "__main__":
    main()
