"""Run the test suite with canonical paths for its temporary fixtures."""

from pathlib import Path
import sys
import tempfile
import unittest


if __name__ == '__main__':
    tempfile.tempdir = str(Path(tempfile.gettempdir()).resolve())
    loader = unittest.defaultTestLoader
    names = sys.argv[1:]
    suite = loader.loadTestsFromNames(names) if names else loader.discover('tests', pattern='test_*.py')
    result = unittest.TextTestRunner().run(suite)
    raise SystemExit(0 if result.wasSuccessful() else 1)
