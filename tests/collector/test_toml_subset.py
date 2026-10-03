import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from bars_collector import adapters, toml_subset
from bars_collector.model import CollectionError
from bars_collector.toml_subset import TOMLSubsetError

# Same shape as the Devin CLI's credentials.toml, with synthetic values.
DEVIN_SHAPE = (
    'windsurf_api_key = "synthetic-key_0123456789"\n'
    'api_server_url = "https://server.example"\n'
    'devin_webapp_host = "app.example"\n'
    'devin_api_url = "https://api.example"\n'
)


class TOMLSubsetTests(unittest.TestCase):
    def test_reads_the_devin_credentials_shape(self):
        self.assertEqual(toml_subset.loads(DEVIN_SHAPE), {
            "windsurf_api_key": "synthetic-key_0123456789",
            "api_server_url": "https://server.example",
            "devin_webapp_host": "app.example",
            "devin_api_url": "https://api.example",
        })

    def test_tables_comments_integers_booleans_and_crlf(self):
        text = ("# leading comment\r\n"
                "\r\n"
                "top = 1 # trailing comment\r\n"
                "[server]\r\n"
                "  port = 8_080\r\n"
                "\tsecure = true\r\n"
                "offset = -12\r\n"
                "zero = +0\r\n"
                "[ server . tls ]  # dotted header\r\n"
                "enabled = false\r\n"
                "[other]\r\n"
                'name = "x # not a comment"\r\n')
        self.assertEqual(toml_subset.loads(text), {
            "top": 1,
            "server": {"port": 8080, "secure": True, "offset": -12, "zero": 0, "tls": {"enabled": False}},
            "other": {"name": "x # not a comment"},
        })

    def test_basic_string_escapes(self):
        value = toml_subset.loads(r'k = "q\" b\\ t\t n\n r\r f\f bs\b ué U\U0001F600 end"')["k"]
        self.assertEqual(value, 'q" b\\ t\t n\n r\r f\f bs\b ué U\U0001F600 end')

    def test_super_table_after_sub_table_is_valid(self):
        self.assertEqual(toml_subset.loads("[a.b]\nx = 1\n[a]\ny = 2\n"), {"a": {"b": {"x": 1}, "y": 2}})

    def test_empty_document(self):
        self.assertEqual(toml_subset.loads(""), {})
        self.assertEqual(toml_subset.loads("# only\n\n"), {})

    def test_rejects_syntax_outside_the_subset(self):
        rejected = [
            "k = 'literal'",                 # literal string
            'k = """multi\nline"""',         # multi-line string
            "k = 1.5",                       # float
            "k = 1979-05-27",                # date
            "k = [1, 2]",                    # array
            "k = { a = 1 }",                 # inline table
            '"quoted" = 1',                  # quoted key
            "a.b = 1",                       # dotted key
            "[[array]]",                     # array of tables
            "[bad",                          # unterminated header
            "[]",                            # empty header
            "k = 1\nk = 2",                  # duplicate key
            "[t]\n[t]",                      # duplicate table
            "k = 1\n[k]",                    # table redefines a value
            'k = "unterminated',             # unterminated string
            r'k = "bad \q escape"',          # invalid escape
            r'k = "\u12"',                   # short unicode escape
            r'k = "\uD800"',                 # surrogate
            r'k = "\U00110000"',             # beyond Unicode
            'k = "a" trailing',              # text after value
            "k = 01",                        # leading zero
            "k = 1__0",                      # doubled separator
            "k = True",                      # booleans are lowercase
            "k =",                           # missing value
            "k",                             # missing equals
            "= 1",                           # missing key
            'k = "tab\x01char"',             # control character
            "k = 1 # bell\x07",              # control character in comment
            'k = "a\rb"',                    # bare carriage return
        ]
        for text in rejected:
            with self.subTest(text=text), self.assertRaises(TOMLSubsetError):
                toml_subset.loads(text)

    def test_errors_are_value_errors_without_file_content(self):
        with self.assertRaises(ValueError) as raised:
            toml_subset.loads('secret = "never-shown" junk')
        self.assertNotIn("never-shown", str(raised.exception))
        self.assertIn("line 1", str(raised.exception))

    def test_bytes_are_rejected_like_tomllib(self):
        with self.assertRaises(TypeError):
            toml_subset.loads(b"k = 1")

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib is unavailable before Python 3.11")
    def test_matches_tomllib_on_supported_input(self):
        import tomllib
        samples = [DEVIN_SHAPE, "a = 1\n[t]\nb = true\nc = \"\\u00e9\\n\"\n[t.u]\nd = -3_000\n",
                   "[a.b]\nx = 1\n[a]\ny = 2\n"]
        for text in samples:
            with self.subTest(text=text):
                self.assertEqual(toml_subset.loads(text), tomllib.loads(text))


class DevinCredentialFileTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        patcher = patch.dict(os.environ, {"HOME": self.home.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path = Path(self.home.name) / ".local/share/devin/credentials.toml"

    def write(self, text):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(text)

    def test_reads_token_from_flat_file(self):
        self.write(DEVIN_SHAPE)
        self.assertEqual(adapters.devin_access_token(), "synthetic-key_0123456789")

    def test_missing_file_needs_setup(self):
        with self.assertRaises(CollectionError) as raised:
            adapters.devin_access_token()
        self.assertEqual(raised.exception.status, "setup")

    def test_unsupported_syntax_needs_login_without_leaking(self):
        for text in ("windsurf_api_key = 'synthetic-literal'\n", "windsurf_api_key = [\"x\"]\n",
                     "[nested]\nwindsurf_api_key = \"synthetic\"\n", "windsurf_api_key = 7\n"):
            with self.subTest(text=text):
                self.write(text)
                with self.assertRaises(CollectionError) as raised:
                    adapters.devin_access_token()
                self.assertEqual(raised.exception.status, "login_required")
                self.assertNotIn("synthetic", json.dumps(str(raised.exception)))


if __name__ == "__main__":
    unittest.main()
