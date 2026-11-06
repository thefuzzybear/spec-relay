"""Detached cleanup entry point with durable progress records."""

import argparse

from .cleanup import CleanupManager
from .runtime import Runtime


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state', required=True)
    parser.add_argument('--cleanup', required=True)
    args = parser.parse_args()
    CleanupManager(Runtime(args.state)).execute(args.cleanup)


if __name__ == '__main__':
    main()
