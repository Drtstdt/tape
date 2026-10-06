import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tape.scripts.helius_verify import _pearson


class TestPearson(unittest.TestCase):
    def test_perfect_positive_correlation(self):
        self.assertAlmostEqual(_pearson([1, 2, 3, 4], [2, 4, 6, 8]), 1.0)

    def test_perfect_negative_correlation(self):
        self.assertAlmostEqual(_pearson([1, 2, 3, 4], [8, 6, 4, 2]), -1.0)

    def test_no_correlation_returns_a_value_not_a_crash(self):
        result = _pearson([1, 2, 3, 4], [5, 1, 5, 1])
        self.assertIsNotNone(result)
        self.assertTrue(-1.0 <= result <= 1.0)

    def test_too_few_points_returns_none(self):
        self.assertIsNone(_pearson([1], [1]))
        self.assertIsNone(_pearson([], []))

    def test_mismatched_lengths_returns_none(self):
        self.assertIsNone(_pearson([1, 2], [1, 2, 3]))

    def test_zero_variance_returns_none_not_a_divide_by_zero_crash(self):
        self.assertIsNone(_pearson([5, 5, 5], [1, 2, 3]))
        self.assertIsNone(_pearson([1, 2, 3], [5, 5, 5]))


if __name__ == "__main__":
    unittest.main()
