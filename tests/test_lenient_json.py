import json
import unittest

from validate import lenient_json


class LenientJsonTest(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(lenient_json('{"a": [1, 2]}'), {"a": [1, 2]})

    def test_strips_line_comments(self):
        self.assertEqual(lenient_json('{\n  "a": 1 // the answer\n}'), {"a": 1})

    def test_keeps_comment_markers_inside_strings(self):
        self.assertEqual(lenient_json('{"url": "https://example.com"}'), {"url": "https://example.com"})

    def test_keeps_escaped_quotes_inside_strings(self):
        self.assertEqual(lenient_json(r'{"text": "say \"hi\" // not a comment"}'),
                         {"text": 'say "hi" // not a comment'})

    def test_drops_trailing_commas(self):
        self.assertEqual(lenient_json('{"a": [1, 2,], "b": 3,}'), {"a": [1, 2], "b": 3})

    def test_adds_missing_closing_braces(self):
        self.assertEqual(lenient_json('{"a": {"b": {"c": 1'), {"a": {"b": {"c": 1}}})

    def test_raises_when_it_cannot_repair(self):
        with self.assertRaises(json.JSONDecodeError):
            lenient_json('{"a": ')


if __name__ == "__main__":
    unittest.main()
