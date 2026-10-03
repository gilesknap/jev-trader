"""`python -m trader`: the `trader` command."""

import sys

from trader import __version__

__all__ = ["main"]


def main() -> None:
    # Answered here because trader.cli.main() loads config.yaml, which an installed wheel may not have.
    if sys.argv[1:] == ["--version"]:
        print(__version__)
        return
    from trader.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
