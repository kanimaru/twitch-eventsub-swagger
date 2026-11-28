import unittest

from validate import KNOWN_PAYLOAD_ERRORS

SOURCES = {"docs", "twitch-cli", "fixture"}


class KnownPayloadErrorsTest(unittest.TestCase):
    """KNOWN_PAYLOAD_ERRORS is edited by hand; a malformed key would never match and only fail as stale."""

    def test_keys_are_source_type_version_path(self):
        for key, reason in KNOWN_PAYLOAD_ERRORS.items():
            with self.subTest(key=key):
                self.assertIsInstance(key, tuple)
                self.assertEqual(len(key), 4)
                source, subscription_type, version, path = key
                self.assertIn(source, SOURCES)
                self.assertRegex(subscription_type, r"^[a-z_]+(\.[a-z_]+)+$")
                self.assertRegex(version, r"^(\d+|beta)$")
                self.assertRegex(path, r"^(condition|event|events\[\])(\.|$)")
                self.assertNotRegex(path, r"\[\d+\]", "write array indices as []")
                self.assertTrue(isinstance(reason, str) and reason, "give the reason the payload is wrong")


if __name__ == "__main__":
    unittest.main()
