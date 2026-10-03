#!/bin/sh
# Install or update Bars on this Mac by cloning its source and building it locally.
#
# Download the entire file before running it, as the README's install command does:
#   curl -fsSL <raw URL> -o ~/Downloads/bars-bootstrap.sh && sh ~/Downloads/bars-bootstrap.sh
# Local use: sh scripts/bootstrap.sh [--disable cursor] [--no-start] [--uninstall]
#
# Locally built apps normally have no download quarantine attribute. Local security
# policy can still block them. Requires macOS 14+ and Apple's Command Line Tools;
# Homebrew is not needed. See docs/adr/0005-distribute-by-source-bootstrap.md.
#
# Function definitions defer installation until the final call, so a truncated file
# runs nothing; downloading first adds the guarantee that no partial file is run at all.
set -eu

# The repository the one-liner installs from. BARS_REPO_URL overrides it without editing this file.
DEFAULT_REPO_URL="https://github.com/kenny-linktree/bars.git"
DEFAULT_REF="main"

LABEL="local.bars.collector"
BUNDLE_ID="local.bars.app"
APP="/Applications/Bars.app"
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"

say() { printf '%s\n' "$*"; }
fail() { printf 'Bars: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: bootstrap.sh [--disable ID]... [--enable-all] [--no-start] [--build-only]
       bootstrap.sh --uninstall [--keep-data]

Clones or updates Bars in ~/Library/Application Support/Bars/src, builds it with
Apple's Command Line Tools and installs it. Other options go to scripts/install.py.

  --disable ID   Disable a provider (claude, codex, cursor, devin). Repeatable.
  --enable-all   Enable every provider. By default, providers without a local
                 login are disabled.
  --no-start     Install without starting the scheduled collector now.
  --build-only   Clone or update and build, but do not install.
  --uninstall    Remove Bars, its scheduled collector and its data.
  --keep-data    With --uninstall, keep ~/Library/Application Support/Bars.

Environment: BARS_REPO_URL (repository to clone), BARS_REF (branch, default main).
EOF
}

check_user() {
  [ "$(uname -s)" = Darwin ] || fail "Bars requires macOS."
  [ "$(id -u)" -ne 0 ] || fail "Run this as your own user, not as root or with sudo."
  case ${HOME:-} in
    / | '' | [!/]*) fail "HOME must be your home directory (got '${HOME:-}')." ;;
  esac
  [ -d "$HOME" ] || fail "HOME is not set to an existing directory."
  [ -O "$HOME" ] || fail "HOME must be a directory you own."
}

check_inputs() {
  # Both come from the environment; a leading '-' would be read as a git option.
  case $REPO_URL in
    '' | -*) fail "BARS_REPO_URL is not a repository URL." ;;
  esac
  case $REF in
    '' | -* | *..* | *[!A-Za-z0-9._/-]*) fail "BARS_REF must be a branch name." ;;
  esac
}

# The user's environment must not point git at another repository or work tree:
# fetch_source force-checks out and hard-resets, which would discard work there.
isolate_git() {
  unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR GIT_OBJECT_DIRECTORY \
    GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_NAMESPACE GIT_CEILING_DIRECTORIES \
    GIT_DISCOVERY_ACROSS_FILESYSTEM GIT_PREFIX GIT_CONFIG_COUNT GIT_CONFIG_PARAMETERS
  # A private or mistyped repository must fail instead of asking for a password.
  GIT_TERMINAL_PROMPT=0
  export GIT_TERMINAL_PROMPT
}

check_install_arguments() {
  has_disable=0
  enable_all=0
  while [ "$#" -gt 0 ]; do
    case $1 in
      --disable)
        shift
        [ "$#" -gt 0 ] || fail "--disable needs a provider ID."
        case $1 in
          claude | codex | cursor | devin) has_disable=1 ;;
          *) fail "Unknown provider ID: $1." ;;
        esac ;;
      --enable-all) enable_all=1 ;;
      --no-start | --build-only) ;;
      *) fail "Unknown install option: $1." ;;
    esac
    shift
  done
  [ "$has_disable$enable_all" != 11 ] || fail "--disable and --enable-all cannot be combined."
}

# Every update command names the clone's own repository and work tree explicitly,
# which also overrides any core.worktree setting in its config.
src_git() {
  git --git-dir="$SRC/.git" --work-tree="$SRC" -C "$SRC" "$@"
}

command_line_tools_present() {
  developer_dir=$(xcode-select -p 2>/dev/null) && [ -n "$developer_dir" ] && [ -d "$developer_dir" ]
}

check_macos() {
  version=$(sw_vers -productVersion)
  major=${version%%.*}
  case $major in
    '' | *[!0-9]*) fail "Could not read the macOS version ($version)." ;;
  esac
  [ "$major" -ge 14 ] || fail "Bars requires macOS 14 or later; this Mac runs $version."
}

check_command_line_tools() {
  if command_line_tools_present && /usr/bin/xcrun --find swiftc >/dev/null 2>&1; then
    return 0
  fi
  say "Bars is built on this Mac and needs Apple's Command Line Tools."
  say "Opening the Command Line Tools installer..."
  xcode-select --install >/dev/null 2>&1 || true
  say "When that installation finishes, run this command again."
  say "(If they are already installed, check that 'xcode-select -p' and 'xcrun --find swiftc' work.)"
  exit 1
}

# Prefer the Command Line Tools' Python, which is always present with them. Without
# them, /usr/bin/python3 is only a stub that opens their installer, so skip it.
choose_python() {
  PYTHON=
  for candidate in /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if [ "$candidate" = /usr/bin/python3 ] && ! command_line_tools_present; then
      continue
    fi
    if [ -x "$candidate" ] &&
      "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
      PYTHON=$candidate
      return 0
    fi
  done
  return 1
}

fetch_source() {
  [ ! -L "$SUPPORT" ] || fail "$SUPPORT must be a directory you own, not a link."
  (umask 077 && mkdir -p "$SUPPORT")
  [ -d "$SUPPORT" ] && [ -O "$SUPPORT" ] || fail "$SUPPORT must be a directory you own, not a link."
  chmod 700 "$SUPPORT"
  if [ -d "$SRC/.git" ] && [ ! -L "$SRC" ] && [ ! -L "$SRC/.git" ]; then
    say "Updating the Bars source in $SRC ($REF)..."
    src_git remote set-url origin "$REPO_URL"
    src_git fetch --quiet --prune origin
    src_git checkout --quiet --force -B "$REF" "origin/$REF"
    src_git reset --quiet --hard "origin/$REF"
  elif [ -e "$SRC" ] || [ -L "$SRC" ]; then
    fail "$SRC exists but is not a Git checkout. Move it aside, then run this again."
  else
    say "Cloning Bars from $REPO_URL ($REF) into $SRC..."
    git clone --quiet --branch "$REF" -- "$REPO_URL" "$SRC"
  fi
}

uninstall_inline() {
  keep_data=$1
  say "Removing Bars without its installer (no source checkout or Python was found)."
  if launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1; then
    say "Stopped the scheduled collector ($LABEL)."
  fi
  plist="$HOME/Library/LaunchAgents/$LABEL.plist"
  if [ -e "$plist" ]; then
    rm -f "$plist"
    say "Removed $plist"
  fi
  if [ -d "$APP" ] && [ ! -L "$APP" ]; then
    identifier=$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$APP/Contents/Info.plist" 2>/dev/null || true)
    if [ "$identifier" = "$BUNDLE_ID" ]; then
      pkill -U "$(id -u)" -f "^$APP/Contents/" >/dev/null 2>&1 || true
      # Best effort: let the widget gallery and Launch Services forget the bundle first.
      pluginkit -r "$APP/Contents/PlugIns/BarsWidget.appex" </dev/null >/dev/null 2>&1 || true
      "$LSREGISTER" -u "$APP" </dev/null >/dev/null 2>&1 || true
      if rm -rf "$APP"; then say "Removed $APP"; else say "Could not remove $APP; continuing."; fi
    else
      say "Left $APP in place: it is not a Bars build."
    fi
  fi
  # The app's AppKit preferences. defaults clears cfprefsd's copy; the file may remain.
  prefs_removed=0
  if defaults delete "$BUNDLE_ID" </dev/null >/dev/null 2>&1; then prefs_removed=1; fi
  prefs="$HOME/Library/Preferences/$BUNDLE_ID.plist"
  if [ -L "$prefs" ] || [ -f "$prefs" ]; then
    if rm -f "$prefs"; then prefs_removed=1; fi
  fi
  if [ "$prefs_removed" = 1 ]; then say "Removed the Bars preferences ($BUNDLE_ID)."; fi
  if [ -L "$SUPPORT" ] || { [ -d "$SUPPORT" ] && [ ! -O "$SUPPORT" ]; }; then
    say "Left $SUPPORT in place: it is a link or is not owned by you."
  elif [ -d "$SUPPORT" ]; then
    if [ "$keep_data" = 1 ]; then
      say "Kept $SUPPORT (--keep-data)."
    else
      rm -rf "$SUPPORT"
      say "Removed $SUPPORT"
    fi
  fi
  say "Done. Provider logins were not touched."
}

uninstall() {
  keep_data=0
  for arg in "$@"; do
    case $arg in
      --uninstall) ;;
      --keep-data) keep_data=1 ;;
      *) fail "--uninstall accepts only --keep-data (got $arg)." ;;
    esac
  done
  # Check before either removal path, including an older checkout's installer.
  # A link at the app path is left in place by both paths.
  if [ ! -L "$APP" ] && [ -e "$APP" ] && [ ! -O "$APP" ]; then
    fail "$APP belongs to another user account on this Mac. Uninstall Bars from the account that installed it."
  fi
  if [ ! -L "$SUPPORT" ] && [ -O "$SUPPORT" ] && [ ! -L "$SRC" ] &&
    [ -f "$SRC/scripts/install.py" ] && choose_python; then
    if [ "$keep_data" = 1 ]; then
      "$PYTHON" "$SRC/scripts/install.py" --uninstall --keep-data
    else
      "$PYTHON" "$SRC/scripts/install.py" --uninstall
    fi
    say "Provider logins were not touched."
  else
    uninstall_inline "$keep_data"
  fi
}

main() {
  check_user
  isolate_git
  REPO_URL=${BARS_REPO_URL:-$DEFAULT_REPO_URL}
  REF=${BARS_REF:-$DEFAULT_REF}
  SUPPORT="$HOME/Library/Application Support/Bars"
  SRC="$SUPPORT/src"
  check_inputs

  mode=install
  for arg in "$@"; do
    case $arg in
      -h | --help) usage; return 0 ;;
      --uninstall) mode=uninstall ;;
      --build-only) [ "$mode" = uninstall ] || mode=build ;;
    esac
  done
  if [ "$mode" = uninstall ]; then
    uninstall "$@"
    return 0
  fi
  check_install_arguments "$@"

  check_macos
  check_command_line_tools
  if [ "$mode" = install ]; then
    # The installer copies the app to /Applications. Check before fetching or building so a
    # standard account changes nothing. BARS_APPLICATIONS_DIR exists for the tests only.
    [ -w "${BARS_APPLICATIONS_DIR:-/Applications}" ] || fail "Your account cannot write to /Applications, where Bars installs. Use an administrator account. Nothing was changed."
  fi
  choose_python || fail "Python 3.9 or later was not found, although the Command Line Tools provide it. Reinstall them with 'xcode-select --install'."
  fetch_source

  # Keep every argument except bootstrap's own for the installer (POSIX sh has no arrays).
  for arg in "$@"; do
    shift
    case $arg in
      --build-only) ;;
      *) set -- "$@" "$arg" ;;
    esac
  done

  say "Building Bars with the Command Line Tools (this takes a minute or two)..."
  BARS_BUILD_PYTHON=$PYTHON /bin/bash "$SRC/scripts/build.sh" --toolchain clt >/dev/null
  built="$SRC/.build/native/Release/Bars.app"
  if [ "$mode" = build ]; then
    say "Built $built (not installed)."
    return 0
  fi

  say "Installing with $PYTHON..."
  "$PYTHON" "$SRC/scripts/install.py" --app "$built" --python "$PYTHON" "$@"

  say ""
  say "Bars is installed. Its bar-chart icon is in the menu bar."
  say "Add the widget: right-click the desktop, choose Edit Widgets, search for Bars and add the large widget."
  say ""
  say "To update Bars, run the same command again, or: sh \"$SRC/scripts/bootstrap.sh\""
  say "Options follow the downloaded script's path, or the installed script's path above:"
  say "  --disable cursor   disable a provider (repeatable)    --enable-all   enable every provider"
  say "  --uninstall        remove Bars (add --keep-data to keep its data)"
  say "Providers can also be switched on and off from the Bars dropdown."
}

main "$@" </dev/null
