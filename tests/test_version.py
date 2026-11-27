import unittest

from scripts.bump_version import bump


class VersionBumpTests(unittest.TestCase):
    def test_patch_rolls_over_to_minor(self):
        self.assertEqual(bump("1.0.8"), "1.0.9")
        self.assertEqual(bump("1.0.9"), "1.1.0")

    def test_minor_rolls_over_to_major(self):
        self.assertEqual(bump("1.9.9"), "2.0.0")


if __name__ == "__main__":
    unittest.main()
