# KOKEN - Machine Corpus
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

"""``python -m koken``, and the ``koken`` command the packages install."""

from __future__ import annotations

import sys


def main() -> int:
    from .app import run

    return run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
