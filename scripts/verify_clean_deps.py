from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import tomllib
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def project_dependencies() -> list[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return list(data["project"]["dependencies"])


def verify_exact_pins() -> None:
    loose = [req for req in project_dependencies() if "==" not in req]
    if loose:
        raise SystemExit(f"Project dependencies must be exact pins for clean CI resolution: {loose}")


def run_clean_install() -> None:
    with tempfile.TemporaryDirectory(prefix="job-seeker-deps-") as tmp_dir:
        builder = venv.EnvBuilder(with_pip=True, clear=True)
        builder.create(tmp_dir)
        python = Path(tmp_dir) / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "-e", str(ROOT)], check=True)
        subprocess.run([str(python), "-m", "pip", "check"], check=True)
        frozen = subprocess.check_output([str(python), "-m", "pip", "freeze"], text=True)
        print("Clean dependency verification passed in isolated venv.")
        for requirement in project_dependencies():
            print(f"PIN {requirement}")
        print("FREEZE-SAMPLE")
        for line in frozen.splitlines()[:30]:
            print(line)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify pyproject dependencies resolve cleanly from exact pins.")
    parser.add_argument("--install", action="store_true", help="Create an isolated venv, install the project, and run pip check.")
    args = parser.parse_args()
    verify_exact_pins()
    if args.install:
        run_clean_install()
    else:
        print("Exact dependency pin check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
