"""A strict reader for the small TOML subset used by CLI credential files.

Python 3.9's standard library has no TOML parser; tomllib arrived in 3.11. The Devin
CLI writes a flat file of bare keys and basic strings, so Bars reads only:

- blank lines and ``#`` comments, including a comment after a value;
- ``[table]`` and ``[dotted.table]`` headers made of bare keys;
- ``key = value`` lines with a bare key and one of: a basic ``"string"`` with the
  TOML escapes ``\\b \\t \\n \\f \\r \\" \\\\ \\uXXXX \\UXXXXXXXX``, a decimal integer
  (optional sign and ``_`` separators, no leading zeros), ``true`` or ``false``.

Everything else, including literal and multi-line strings, floats, dates, arrays,
inline tables, quoted or dotted keys, array tables and duplicate keys or tables,
raises TOMLSubsetError. It subclasses ValueError, the same failure path that
tomllib.TOMLDecodeError took in the adapters. Error messages carry line numbers only,
never file content, because the file holds credentials.
"""

import re

__all__ = ["TOMLSubsetError", "loads"]


class TOMLSubsetError(ValueError):
    pass


_BARE_KEY = r"[A-Za-z0-9_-]+"
_HEADER = re.compile(r"\[[ \t]*(" + _BARE_KEY + r"(?:[ \t]*\.[ \t]*" + _BARE_KEY + r")*)[ \t]*\][ \t]*(?:#.*)?")
_KEY_VALUE = re.compile(r"(" + _BARE_KEY + r")[ \t]*=[ \t]*(.*)", re.S)
_INTEGER = re.compile(r"[+-]?(?:0|[1-9](?:_?[0-9])*)")
_TRAILING = re.compile(r"[ \t]*(?:#.*)?", re.S)
_HEX = re.compile(r"[0-9A-Fa-f]+")
# TOML forbids control characters other than tab in comments and basic strings.
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_ESCAPES = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}


def loads(text):
    """Parse ``text`` (a str) into nested dicts, rejecting anything outside the subset."""
    if not isinstance(text, str):
        raise TypeError("Expected str.")
    root = {}
    table = root
    defined_tables = set()
    for number, line in enumerate(text.split("\n"), 1):
        if line.endswith("\r"):
            line = line[:-1]
        if _CONTROL.search(line):
            raise TOMLSubsetError("Control character on line %d." % number)
        line = line.strip(" \t")
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            match = _HEADER.fullmatch(line)
            if line.startswith("[[") or not match:
                raise TOMLSubsetError("Unsupported table header on line %d." % number)
            path = tuple(part.strip(" \t") for part in match.group(1).split("."))
            if path in defined_tables:
                raise TOMLSubsetError("Duplicate table on line %d." % number)
            defined_tables.add(path)
            table = root
            for part in path:
                table = table.setdefault(part, {})
                if not isinstance(table, dict):
                    raise TOMLSubsetError("Table redefines a value on line %d." % number)
            continue
        match = _KEY_VALUE.fullmatch(line)
        if not match:
            raise TOMLSubsetError("Unsupported key or syntax on line %d." % number)
        key, rest = match.groups()
        if key in table:
            raise TOMLSubsetError("Duplicate key on line %d." % number)
        table[key] = _value(rest, number)
    return root


def _value(text, number):
    if text.startswith('"'):
        if text.startswith('"""'):
            raise TOMLSubsetError("Unsupported multi-line string on line %d." % number)
        value, end = _basic_string(text, number)
    else:
        end = len(re.match(r"[^ \t#]*", text).group(0))
        token = text[:end]
        if token in ("true", "false"):
            value = token == "true"
        elif _INTEGER.fullmatch(token):
            value = int(token.replace("_", ""))
        else:
            raise TOMLSubsetError("Unsupported value on line %d." % number)
    if not _TRAILING.fullmatch(text[end:]):
        raise TOMLSubsetError("Unexpected text after value on line %d." % number)
    return value


def _basic_string(text, number):
    """Return the decoded string and the index just past its closing quote."""
    characters = []
    index = 1
    while index < len(text):
        character = text[index]
        if character == '"':
            return "".join(characters), index + 1
        if character != "\\":
            characters.append(character)
            index += 1
            continue
        escape = text[index + 1:index + 2]
        if escape in _ESCAPES:
            characters.append(_ESCAPES[escape])
            index += 2
        elif escape in ("u", "U"):
            length = 4 if escape == "u" else 8
            digits = text[index + 2:index + 2 + length]
            if len(digits) != length or not _HEX.fullmatch(digits):
                raise TOMLSubsetError("Invalid unicode escape on line %d." % number)
            code = int(digits, 16)
            if code > 0x10FFFF or 0xD800 <= code <= 0xDFFF:
                raise TOMLSubsetError("Invalid unicode escape on line %d." % number)
            characters.append(chr(code))
            index += 2 + length
        else:
            raise TOMLSubsetError("Invalid escape on line %d." % number)
    raise TOMLSubsetError("Unterminated string on line %d." % number)
