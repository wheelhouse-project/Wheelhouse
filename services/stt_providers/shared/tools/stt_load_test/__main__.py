"""Entry point: python -m tools.stt_load_test [--baseline]"""
import sys

from .run import main

if __name__ == '__main__':
    sys.exit(main())
