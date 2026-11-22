"""Run incident-lab as a module: python -m incident_lab list / run <name>."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
