import sys


def warn(message: str):
    """Print a warning to stderr in yellow."""
    print(f'\033[33mWarning: {message}\033[0m', file=sys.stderr)
