import unittest

from ubertooth_harness.cancellation import CancellationToken


class CancellationTokenTests(unittest.TestCase):

    def test_starts_active_and_preserves_first_cancellation_reason(self):
        token = CancellationToken()

        self.assertFalse(token.is_cancelled())
        self.assertEqual("", token.reason)
        token.cancel("operator requested")
        token.cancel("later reason")

        self.assertTrue(token.is_cancelled())
        self.assertEqual("operator requested", token.reason)


if __name__ == "__main__":
    unittest.main()
