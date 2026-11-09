#!/bin/bash
#
# Install TeX Live 2025 (official) on Linux and compile the thesis.
#
# Why 2025 and not the newest 2026?  The thesis uses the KU Leuven `kulemt`
# class (template/kulemt.cls, v2.1.0). The babel shipped with the current
# TeX Live 2026 net repo (v26.8, 2026-05-20) introduced a stricter BCP-47
# language layer that is INCOMPATIBLE with kulemt 2.1.0 -- it aborts at
# \begin{document} with "Package text Error: Invalid language in BCP input".
# TeX Live 2025 ships babel v26.3 (2026-02-14), which predates that change and
# compiles kulemt cleanly. (For reference, a Mac with babel 26.4 also works.)
#
# Install layout matches a stock install-tl: /usr/local/texlive/2025/.
# Coexists with any apt texlive; selection is via PATH (tlmgr path add).
#
# Strategy: scheme-small (a few hundred MB) + the exact extra packages the
# thesis loads on top of it -- far smaller than scheme-full (~8 GB) while
# still compiling. Pulls cm-super + tex-gyre (and lmodern via
# collection-fontsrecommended) so the body text is fully vector (Type 1),
# avoiding the jagged/thin Type 3 bitmap fallback.
#
# Requires network access and sudo. Total download is ~700 MB; allow ~20 min.
# If the mirror below is slow or down, replace REPO with any TeX Live 2025
# tlnet-final mirror (see https://ctan.org/mirrors), keeping the
# /2025/tlnet-final path.

set -euo pipefail

TL_YEAR=2025
TL_ROOT="/usr/local/texlive/${TL_YEAR}"
# TeX Live 2025 is superseded by 2026, so it lives in the historic archive,
# not the rolling tlnet. tlnet-final is the frozen end-of-year 2025 repo.
REPO="https://ftp.math.utah.edu/pub/tex/historic/systems/texlive/2025/tlnet-final"

THESIS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../docs/thesis" && pwd)"

# --- prerequisites the installer itself needs ---------------------------------
# perl/wget/tar/xz: run install-tl & unpack. fontconfig: TeX Live font setup.
# poppler-utils: only for the post-build `pdffonts` sanity check (optional).
echo "==> Installing prerequisites..."
sudo apt-get update -y
sudo apt-get install -y --no-install-recommends \
  perl wget tar gzip xz-utils fontconfig poppler-utils

# --- fetch and run install-tl -------------------------------------------------
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
echo "==> Downloading install-tl (TeX Live ${TL_YEAR})..."
wget -q -O "$WORK/install-tl-unx.tar.gz" "${REPO}/install-tl-unx.tar.gz"
tar -xzf "$WORK/install-tl-unx.tar.gz" -C "$WORK"
INSTALLER_DIR="$(find "$WORK" -maxdepth 1 -type d -name 'install-tl-*' | head -n1)"

# Unattended profile: scheme-small, no docs/src trees (smaller, faster).
PROFILE="$WORK/texlive.profile"
cat > "$PROFILE" <<EOF
selected_scheme scheme-small
TEXDIR ${TL_ROOT}
TEXMFLOCAL /usr/local/texlive/texmf-local
TEXMFSYSCONFIG ${TL_ROOT}/texmf-config
TEXMFSYSVAR ${TL_ROOT}/texmf-var
tlpdbopt_install_docfiles 0
tlpdbopt_install_srcfiles 0
tlpdbopt_autobackup 0
EOF

echo "==> Running install-tl (scheme-small)..."
sudo perl "$INSTALLER_DIR/install-tl" \
  --profile "$PROFILE" \
  --repository "$REPO"

# --- put TeX Live on PATH for the rest of this script -------------------------
# install-tl drops binaries under bin/<platform>/ (e.g. x86_64-linux,
# aarch64-linux). Grab whatever single platform dir was created.
TLBIN="$(find "${TL_ROOT}/bin" -maxdepth 1 -mindepth 1 -type d | head -n1)"
if [ -z "${TLBIN}" ] || [ ! -x "${TLBIN}/tlmgr" ]; then
  echo "ERROR: TeX Live binaries not found under ${TL_ROOT}/bin -- install-tl failed?" >&2
  exit 1
fi
export PATH="${TLBIN}:${PATH}"

# --- packages the thesis needs beyond scheme-small ----------------------------
# scheme-small already has: amsmath/amsfonts, graphics, hyperref, xcolor,
# tools (longtable/tabularx), lm (Latin Modern), babel-english.
# Everything below is what thesis.tex additionally loads. Notes:
#   - latexmk is NOT in scheme-small, so it is listed explicitly.
#   - lmodern lives in collection-fontsrecommended (no standalone package in
#     TeX Live 2025), so we do NOT list it by name -- the collection covers it.
#   - caption carries subcaption; collection-latexextra pulls
#     ccaption/enumitem/pdflscape/booktabs/xltabular/ltablex/etc.
#   - unicode-math + lualatex-math + latin-modern-math: required by the
#     matplotlib pgf+lualatex pipeline that produces the figure PDFs
#     (see docs/thesis/figures/scripts/_style.py). Without them every
#     `python fig_*.py` aborts at draw time; the figure scripts then fall
#     back to DejaVu fonts and print a warning.
# tlmgr returns a non-zero exit if any single name is already-present, so we
# guard with `|| true` and verify the real dependencies right after instead.
echo "==> Installing extra packages on top of scheme-small..."
sudo "${TLBIN}/tlmgr" --repository "$REPO" install \
  latexmk \
  collection-latexextra \
  collection-pictures \
  collection-fontsrecommended \
  collection-langeuropean \
  cm-super \
  tex-gyre \
  unicode-math \
  lualatex-math \
  fontspec \
  lm-math || true

# Verify every file the thesis actually needs resolved (so we fail loudly here
# rather than mid-compile). lmodern.sty comes via collection-fontsrecommended,
# subcaption.sty via the caption bundle, sfrm1000.pfb is cm-super, qhvr.pfb is
# tex-gyre (kulemt's required Heros font).
echo "==> Verifying thesis dependencies..."
for f in latexmk pdflatex; do
  command -v "$f" >/dev/null || "${TLBIN}/${f}" --version >/dev/null 2>&1 || \
    { echo "MISSING binary: $f" >&2; exit 1; }
done
for f in ccaption.sty xltabular.sty ltablex.sty enumitem.sty pdflscape.sty \
         booktabs.sty subcaption.sty lmodern.sty pgf.sty tgheros.sty \
         sfrm1000.pfb qhvr.pfb \
         unicode-math.sty lualatex-math.sty fontspec.sty; do
  "${TLBIN}/kpsewhich" "$f" >/dev/null || \
    { echo "MISSING file: $f" >&2; exit 1; }
done
echo "    all dependencies present."

# Symlink binaries into /usr/local/bin and refresh font maps so cm-super /
# tex-gyre Type 1 fonts win over Type 3 bitmap fallback.
sudo "${TLBIN}/tlmgr" path add || true
sudo "${TLBIN}/updmap-sys" --quiet >/dev/null 2>&1 || true
sudo "${TLBIN}/mktexlsr" >/dev/null 2>&1 || true
hash -r

echo
echo "==> Installation complete!"
pdflatex --version | head -n 1
latexmk --version | head -n 1

# --- compile the thesis to verify the install --------------------------------
echo
echo "==> Compiling the thesis (${THESIS_DIR})..."
cd "$THESIS_DIR"
rm -rf .build thesis.pdf
if latexmk; then
  echo
  echo "==> SUCCESS: thesis.pdf built."
  ls -l thesis.pdf
  # Sanity check: a clean install yields fully vector text (no Type 3 bitmap
  # fonts). Report the count; non-zero is a warning, not a hard failure.
  if command -v pdffonts >/dev/null; then
    t3="$(pdffonts thesis.pdf 2>/dev/null | grep -c 'Type 3' || true)"
    if [ "${t3}" -eq 0 ]; then
      echo "    Fonts: all vector (Type 3 bitmap count = 0). Good."
    else
      echo "    WARNING: ${t3} Type 3 (bitmap) fonts found -- text may look jagged." >&2
    fi
  fi
else
  echo
  echo "==> BUILD FAILED. See .build/thesis.log for details." >&2
  exit 1
fi

cat <<EOF

TeX Live ${TL_YEAR} is at ${TL_ROOT}, on PATH via /usr/local/bin.
Rebuild anytime with:  cd docs/thesis && latexmk
EOF
