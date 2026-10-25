# SPDX-License-Identifier: MIT
"""Run the image tests (tests/image/test_*.py) and print one PASS, FAIL or SKIP line per test.

    python3 -I -B tests/image/run.py
"""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


class Result(unittest.TestResult):
    def name(self, test):
        return test.id().split(".", 1)[-1]

    def addSuccess(self, test):
        super().addSuccess(test)
        print(f"PASS  image/{self.name(test)}", flush=True)

    def report(self, word, test, text):
        print(f"{word}  image/{self.name(test)}", flush=True)
        for line in text.rstrip().splitlines()[-15:]:
            print(f"      {line}")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.report("FAIL", test, self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self.report("FAIL", test, self._exc_info_to_string(err, test))

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.report("FAIL", subtest, self._exc_info_to_string(err, subtest))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        print(f"SKIP  image/{self.name(test)}: {reason}", flush=True)


def main():
    suite = unittest.defaultTestLoader.discover(str(HERE), pattern="test_*.py", top_level_dir=str(HERE))
    result = Result()
    suite.run(result)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
