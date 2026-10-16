"""Credential lookup and bounded, read-only provider requests.

Credentials never enter a snapshot. The worker deadline in the collector also bounds
DNS resolution and servers that keep a socket alive without finishing a response.
"""

import http.client
import json
import os
from pathlib import Path
import re
import selectors
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from . import toml_subset
from .values import CollectionError

MAX_BYTES = 1024 * 1024
# /usr/bin/security exits with this status when no matching Keychain item exists
# (errSecItemNotFound, "The specified item could not be found in the keychain.").
SECURITY_ITEM_NOT_FOUND = 44
# Credential tools and provider CLIs receive only what they need to run as this user.
# Everything else in the caller's environment, such as other services' API keys or
# XDG_*/DEVIN_* overrides that could point a CLI at a different login than the file
# Bars reads, is withheld. Proxy and CA settings pass through for managed networks.
CHILD_ENVIRONMENT = (
    "HOME", "PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE",
    "__CF_USER_TEXT_ENCODING", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
    "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR",
)
# Visible ASCII only: rejects whitespace and control characters before a value can
# reach an HTTP header, cookie or URL.
_TOKEN = re.compile(r"[\x21-\x7e]{1,32768}")


class ToolFailed(CollectionError):
    """A login tool exited with a non-zero status. Only the status is kept, never output."""

    def __init__(self, returncode):
        super().__init__("Existing login is unavailable. Open the provider CLI to sign in.", "login_required")
        self.returncode = returncode


def child_environment():
    return {key: os.environ[key] for key in CHILD_ENVIRONMENT if key in os.environ}


def command(args, timeout=10):
    """Run a login tool from an argument list and return its trimmed standard output.

    No shell, no standard input, an allowlisted environment, output bounded to 1 MiB and
    a deadline that kills the tool. Standard error is discarded and never surfaces.
    """
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=child_environment(), stdin=subprocess.DEVNULL)
        deadline = time.monotonic() + timeout
        output = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise subprocess.TimeoutExpired(args, timeout)
                    chunk = os.read(proc.stdout.fileno(), min(65536, MAX_BYTES + 1 - len(output)))
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > MAX_BYTES:
                        raise CollectionError("Login tool returned an invalid response.")
                proc.wait(timeout=max(0, deadline - time.monotonic()))
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            proc.stdout.close()
    except subprocess.TimeoutExpired:
        raise CollectionError("Credential lookup timed out.") from None
    except OSError:
        raise CollectionError("Required login tool is unavailable.", "setup") from None
    if proc.returncode:
        raise ToolFailed(proc.returncode)
    try:
        return output.decode().strip()
    except UnicodeError:
        raise CollectionError("Login tool returned an invalid response.") from None


def keychain(service, missing="Existing login was not found. Sign in with the provider CLI."):
    """The secret of a generic password item. A missing item is a `setup` state with `missing`
    as its message; denied access, a locked Keychain and similar states are `login_required`."""
    try:
        return command(["/usr/bin/security", "find-generic-password", "-s", service, "-w"])
    except ToolFailed as error:
        if error.returncode != SECURITY_ITEM_NOT_FOUND:
            raise
    raise CollectionError(missing, "setup")


def keychain_entry_exists(service):
    """Whether a generic password item exists, without reading its secret.

    Omitting -w and -g does not request password data. Output is discarded;
    it holds only item attributes. macOS controls any access prompts.
    """
    try:
        return subprocess.run(["/usr/bin/security", "find-generic-password", "-s", service],
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              env=child_environment(), timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


MISSING_LOGIN = "Existing login was not found. Sign in with the provider CLI."
UNREADABLE_LOGIN = "Existing login could not be read. Open the provider CLI."


def read_bytes(path, *, missing=MISSING_LOGIN, unreadable=UNREADABLE_LOGIN):
    """At most 1 MiB of a login file. Missing is `setup`; unreadable is `login_required`."""
    try:
        with Path(path).open("rb") as file:
            data = file.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError()
        return data
    except FileNotFoundError:
        raise CollectionError(missing, "setup") from None
    except (OSError, ValueError):
        raise CollectionError(unreadable, "login_required") from None


def read_json(path, *, missing=MISSING_LOGIN, unreadable=UNREADABLE_LOGIN):
    data = read_bytes(path, missing=missing, unreadable=unreadable)
    try:
        return json.loads(data)
    except (ValueError, RecursionError):
        # json raises RecursionError, not ValueError, for very deeply nested input.
        raise CollectionError(unreadable, "login_required") from None


def read_toml(path, *, missing=MISSING_LOGIN, unreadable=UNREADABLE_LOGIN):
    """The flat TOML subset credential files use; see toml_subset.py for its limits."""
    data = read_bytes(path, missing=missing, unreadable=unreadable)
    try:
        return toml_subset.loads(data.decode())
    except (ValueError, UnicodeError):
        raise CollectionError(unreadable, "login_required") from None


def token(value):
    """Require a visible-ASCII secret before it can reach a header, cookie or URL."""
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise CollectionError("Existing login is invalid. Open the provider CLI.", "login_required")
    return value


def login_found(*lookups):
    """Run local credential lookups; only a `setup` failure means no login exists.

    A `login_required` failure means login material exists but is unreadable or
    expired, so the provider is in use and should stay enabled to show that state.
    """
    try:
        for lookup in lookups:
            lookup()
    except CollectionError as error:
        return error.status != "setup"
    return True


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward authentication to a redirect target, even on the same host.
        # Returning None makes urllib raise HTTPError for every 3xx, including 308,
        # which Python 3.9's redirect handler does not follow at all.
        return None


def opener():
    """One attempt, no redirects, certificate and hostname verification required.

    The explicit default context keeps verification on even if other code replaced
    ssl's module-level HTTPS context factory. TLS 1.2 is the minimum on every
    interpreter; Python 3.9 with LibreSSL would otherwise accept TLS 1.0. Proxies
    from the environment or system settings still apply. A proxy with a trusted
    interception certificate can read request headers.
    """
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return urllib.request.build_opener(NoRedirect, urllib.request.HTTPSHandler(context=context))


def secure_destination(url):
    """True for an `https` URL, or plain `http` to exactly the loopback host 127.0.0.1.

    The URL is parsed, not prefix-matched, so `http://127.0.0.1.example.com/` and
    `http://127.0.0.1@remote.example/` are refused. No credentials may ride in the URL.
    """
    if not isinstance(url, str):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    if parts.username or parts.password or not parts.hostname:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and parts.hostname == "127.0.0.1"


def http_json(url, headers, timeout=12):
    """One HTTPS GET returning a JSON object. Failures become fixed user-safe messages.

    Only `https://` destinations are allowed, plus plain `http://127.0.0.1` for tests and
    local tools, so a credential header never travels in clear text over a network.
    """
    if not secure_destination(url):
        raise CollectionError("Provider request was refused: only https destinations are allowed.")
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Bars/0.1", **headers})
    try:
        with opener().open(request, timeout=timeout) as response:
            body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise CollectionError("Provider response was too large.")
        data = json.loads(body)
        if not isinstance(data, dict):
            raise CollectionError("Provider returned an invalid usage response.")
        return data
    except urllib.error.HTTPError as error:
        code = error.code
        try:
            error.close()
        except Exception:
            pass  # Python 3.9 raises KeyError closing an HTTPError that has no body stream.
        if code in (401, 403):
            raise CollectionError("Login required. Open the provider CLI to refresh its login.", "login_required") from None
        if code == 429:
            raise CollectionError("Provider rate limit reached. Collection will retry later.") from None
        if 300 <= code < 400:
            raise CollectionError("Provider redirected the request; Bars refused to follow it (HTTP %d)." % code) from None
        raise CollectionError("Provider request failed (HTTP %d)." % code) from None
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError):
        raise CollectionError("Provider could not be reached. Collection will retry later.") from None
    except (ValueError, UnicodeError, RecursionError):
        raise CollectionError("Provider returned an invalid usage response.") from None
