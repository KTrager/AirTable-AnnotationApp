#!/bin/zsh
# =============================================================================
#  Double-click this file in Finder to open the Airtable Image Annotator.
#
#  What it does, in order:
#    1. Looks for Python on this Mac and starts the app with it.
#       (The annotator runs in your normal web browser, so unlike the
#       Image Downloader it does not need any special window toolkit.)
#    2. If there is no Python at all, it shows a normal Mac dialog offering
#       to install it - via Homebrew when available, otherwise with the
#       official installer from python.org (which you click through once).
# =============================================================================
cd "$(dirname "$0")"

APP="airtable_image_annotator_app.py"

# Start the app with the first Python we can find; returns only if none found.
try_launch() {
  hash -r 2>/dev/null   # notice freshly installed programs
  for PY in python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$PY" >/dev/null 2>&1; then
      exec "$PY" "$APP"
    fi
  done
}

# --- 1) best case: Python is already installed --------------------------------
try_launch

# --- 2) offer to install it ----------------------------------------------------
BUTTON=$(osascript -e 'button returned of (display dialog "This app needs a small free helper (Python) that is not on this Mac yet.\n\nInstall it now? It takes a few minutes." buttons {"Not now","Install"} default button "Install" with title "Airtable Image Annotator")' 2>/dev/null)

if [[ "$BUTTON" == "Install" ]]; then
  if command -v brew >/dev/null 2>&1; then
    echo "Installing with Homebrew (a few minutes)…"
    brew install python@3.13
  else
    PKG="$TMPDIR/python-installer.pkg"
    echo "Downloading Python from python.org…"
    if curl -fL -o "$PKG" "https://www.python.org/ftp/python/3.13.7/python-3.13.7-macos11.pkg"; then
      open -W "$PKG"     # -W = wait here until the installer is closed
      # The python.org build needs its security certificates installed once
      # (otherwise connecting to Airtable fails with an SSL error):
      for CERT in "/Applications/Python 3."*"/Install Certificates.command"; do
        [ -e "$CERT" ] && "$CERT" || true
      done
    else
      echo "Download failed - please install Python from python.org/downloads"
    fi
  fi
  try_launch
fi

osascript -e 'display dialog "Python could not be found or installed.\n\nPlease install it from python.org/downloads and then double-click this file again." buttons {"OK"} with title "Airtable Image Annotator"' 2>/dev/null
