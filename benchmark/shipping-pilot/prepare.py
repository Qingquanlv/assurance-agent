"""Materialize the pinned SUT source and apply the reviewable pilot patch."""

import argparse
import subprocess
from pathlib import Path


BASELINE = "cc5f05e9148a831d786ac7658bc26bf3bc54ea77"
PILOT = Path(__file__).resolve().parent
COMPONENTS = ("app", "web", "run.py", "pyproject.toml", "uv.lock", ".python-version", "LICENSE", "README.md")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Local vue-fastapi-admin Git checkout")
    parser.add_argument("--destination", type=Path, default=PILOT.parent / "vue-fastapi-admin")
    args = parser.parse_args()
    destination = args.destination.resolve()
    if any((destination / name).exists() for name in COMPONENTS):
        parser.error("Destination already contains SUT files; refusing to overwrite them")
    source = args.source.resolve()
    archive = subprocess.run(
        ["git", "-C", str(source), "archive", BASELINE, *COMPONENTS], check=True, capture_output=True
    ).stdout
    destination.mkdir(parents=True, exist_ok=True)
    subprocess.run(["tar", "-x", "-C", str(destination)], input=archive, check=True)
    # --no-index makes this independent of the enclosing Assurance repository.
    subprocess.run(
        ["git", "apply", "--no-index", "--check", str(PILOT / "shipping.patch")],
        cwd=destination,
        check=True,
    )
    subprocess.run(["git", "apply", "--no-index", str(PILOT / "shipping.patch")], cwd=destination, check=True)
    print(f"Prepared {destination} from {BASELINE}")


if __name__ == "__main__":
    main()
