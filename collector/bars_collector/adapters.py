"""Credential lookup and bounded, read-only vendor requests.

Credentials never enter a snapshot. The worker deadline in runner.py also bounds
DNS resolution and servers that keep a socket alive without finishing a response.
"""

import base64
import http.client
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from . import parsers, toml_subset
from .model import CollectionError, PROVIDERS

MAX_BYTES = 1024 * 1024
CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
CURSOR_KEYCHAIN_SERVICE = "cursor-access-token"
# /usr/bin/security exits with this status when no matching Keychain item exists
# (errSecItemNotFound, "The specified item could not be found in the keychain.").
SECURITY_ITEM_NOT_FOUND = 44
# A missing item means the provider was never signed in here: a setup state, the same
# condition --detect reports as not found.
KEYCHAIN_MISSING = {
    CLAUDE_KEYCHAIN_SERVICE: "Claude Code login was not found. Install Claude Code and sign in.",
    CURSOR_KEYCHAIN_SERVICE: "Cursor login was not found. Install the Cursor CLI and run agent login.",
}
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


def keychain(service):
    try:
        return command(["/usr/bin/security", "find-generic-password", "-s", service, "-w"])
    except ToolFailed as error:
        if error.returncode != SECURITY_ITEM_NOT_FOUND:
            raise  # Denied access, a locked Keychain and similar states remain login_required.
    raise CollectionError(KEYCHAIN_MISSING.get(
        service, "Existing login was not found. Sign in with the provider CLI."), "setup")


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


def read_json(path):
    try:
        with path.open("rb") as file:
            data = file.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError()
        return json.loads(data)
    except FileNotFoundError:
        raise CollectionError("Existing login was not found. Sign in with the provider CLI.", "setup") from None
    except (OSError, ValueError, RecursionError):
        # json raises RecursionError, not ValueError, for very deeply nested input.
        raise CollectionError("Existing login could not be read. Open the provider CLI.", "login_required") from None


def token(value):
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise CollectionError("Existing login is invalid. Open the provider CLI.", "login_required")
    return value


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


def http_json(url, headers, timeout=12):
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


def collect_claude():
    try:
        credentials = json.loads(keychain(CLAUDE_KEYCHAIN_SERVICE))
        access_token = token(credentials["claudeAiOauth"]["accessToken"])
    except (ValueError, KeyError, TypeError, RecursionError):
        raise CollectionError("Claude login could not be read. Open Claude Code.", "login_required") from None
    return parsers.claude(http_json("https://api.anthropic.com/api/oauth/usage", {
        "Authorization": "Bearer " + access_token, "anthropic-beta": "oauth-2025-04-20"}))


def codex_credentials():
    # Use the actual user's login, not a worker's temporary CODEX_HOME.
    credentials = read_json(Path.home() / ".codex" / "auth.json")
    try:
        tokens = credentials["tokens"]
        return token(tokens["access_token"]), token(tokens["account_id"])
    except (KeyError, TypeError):
        raise CollectionError("Codex needs a ChatGPT login for usage data. Run codex login.", "setup") from None


def collect_codex():
    access_token, account_id = codex_credentials()
    return parsers.codex(http_json("https://chatgpt.com/backend-api/wham/usage", {
        "Authorization": "Bearer " + access_token, "ChatGPT-Account-Id": account_id}))


def collect_cursor():
    access_token = token(keychain(CURSOR_KEYCHAIN_SERVICE))
    try:
        part = access_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        subject = token(claims["sub"])
    except (ValueError, KeyError, IndexError, TypeError, RecursionError):
        raise CollectionError("Cursor login could not be read. Run agent login.", "login_required") from None
    # The unverified JWT claim only constructs the vendor's required session cookie.
    cookie = urllib.parse.quote(subject + "::" + access_token, safe="")
    return parsers.cursor(http_json("https://cursor.com/api/usage-summary", {
        "Cookie": "WorkosCursorSessionToken=" + cookie}))


def devin_access_token():
    try:
        path = Path.home() / ".local" / "share" / "devin" / "credentials.toml"
        with path.open("rb") as file:
            data = file.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError()
        credentials = toml_subset.loads(data.decode())
        return token(credentials["windsurf_api_key"])
    except FileNotFoundError:
        raise CollectionError("Devin login was not found. Run devin auth login.", "setup") from None
    except (OSError, ValueError, KeyError, TypeError, UnicodeError):
        raise CollectionError("Devin login could not be read. Run devin auth login.", "login_required") from None


def devin_cli():
    executable = shutil.which("devin")
    if not executable:
        raise CollectionError("Install the Devin CLI to discover its organization.", "setup")
    return executable


def collect_devin():
    access_token = devin_access_token()
    executable = devin_cli()
    try:
        identity = json.loads(command([executable, "cloud", "drs", "whoami"], timeout=15))
        organization = identity["org_id"]
        if not isinstance(organization, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", organization):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise CollectionError("Devin organization could not be read. Open the Devin CLI.", "login_required") from None
    # Do not trust endpoint overrides from the CLI credential file.
    return parsers.devin(http_json(f"https://app.devin.ai/api/{organization}/billing/quota/usage", {
        "Authorization": "Bearer " + access_token, "x-cog-org-id": organization}))


COLLECTORS = {"claude": collect_claude, "codex": collect_codex, "cursor": collect_cursor, "devin": collect_devin}


def _login_found(*lookups):
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


def detect():
    """Report which providers have local login material, without any network request.

    The lookups are the adapters' own. Keychain items are checked for existence only,
    so no secret is read. No credential value is returned or printed.
    """
    found = {
        "claude": keychain_entry_exists(CLAUDE_KEYCHAIN_SERVICE),
        "codex": _login_found(codex_credentials),
        "cursor": keychain_entry_exists(CURSOR_KEYCHAIN_SERVICE),
        "devin": _login_found(devin_access_token, devin_cli),
    }
    return {pid: found[pid] for pid in PROVIDERS}
