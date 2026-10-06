import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tape.env import load_dotenv


class TestLoadDotenv(unittest.TestCase):
    def setUp(self):
        # Never let a stray `.env` in a dev environment bleed into these
        # tests, and always restore whatever was there afterwards.
        self._saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def _write(self, tmpdir: str, content: str) -> Path:
        p = Path(tmpdir) / ".env"
        p.write_text(content)
        return p

    def test_missing_file_is_a_silent_noop(self):
        os.environ.pop("DOES_NOT_EXIST_KEY", None)
        self.assertEqual(load_dotenv("/no/such/path/.env"), 0)

    def test_parses_a_simple_key_value_line(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ.pop("PROBE_TEST_KEY", None)
            p = self._write(d, "PROBE_TEST_KEY=abc123\n")
            self.assertEqual(load_dotenv(p), 1)
            self.assertEqual(os.environ["PROBE_TEST_KEY"], "abc123")

    def test_strips_quotes_and_whitespace(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ.pop("PROBE_TEST_KEY", None)
            p = self._write(d, '  PROBE_TEST_KEY = "abc 123"  \n')
            load_dotenv(p)
            self.assertEqual(os.environ["PROBE_TEST_KEY"], "abc 123")

    def test_skips_blank_lines_and_comments(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ.pop("PROBE_TEST_KEY", None)
            p = self._write(d, "\n# a comment\nPROBE_TEST_KEY=abc123\n\n# trailing\n")
            self.assertEqual(load_dotenv(p), 1)

    def test_existing_environment_variable_wins(self):
        """A shell/CI-set variable must never be silently overwritten by a
        stray .env file -- that's the standard dotenv contract and the only
        safe default."""
        with tempfile.TemporaryDirectory() as d:
            os.environ["PROBE_TEST_KEY"] = "from_shell"
            p = self._write(d, "PROBE_TEST_KEY=from_dotenv\n")
            set_count = load_dotenv(p)
            self.assertEqual(os.environ["PROBE_TEST_KEY"], "from_shell")
            self.assertEqual(set_count, 0)

    def test_empty_file_sets_nothing(self):
        """The exact situation this test exists for: tape/.env exists on
        disk but is zero bytes -- must behave like "no file", not error."""
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "")
            self.assertEqual(load_dotenv(p), 0)

    def test_line_with_no_equals_sign_is_ignored_not_fatal(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._write(d, "this is not a valid line\nPROBE_TEST_KEY=ok\n")
            os.environ.pop("PROBE_TEST_KEY", None)
            self.assertEqual(load_dotenv(p), 1)


if __name__ == "__main__":
    unittest.main()
