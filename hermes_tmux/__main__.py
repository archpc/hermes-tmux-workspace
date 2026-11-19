"""`python -m hermes_tmux` — the only entry point panes and `up` use.

Deliberately three lines: everything the CLI needs to know about its own
identity (repository, interpreter, session key) is derived from the module
location and the environment, so an install step would add nothing but a
second place for the two to disagree.
"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
