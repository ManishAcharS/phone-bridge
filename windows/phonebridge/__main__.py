"""``python -m phonebridge`` - identical to the ``phonebridge`` console script.

Handy when the package is only importable (no script on PATH yet), which is
exactly the situation after ``pip install -e windows`` in a fresh virtualenv.
"""

from __future__ import annotations

import sys

from .cli.main import main

if __name__ == "__main__":
    sys.exit(main())
