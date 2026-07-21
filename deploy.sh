#!/usr/bin/env bash
# Deploy pbpad to the Raspberry Pi and restart the service.
#
#   ./deploy.sh          (macOS or Linux)
#
# Windows equivalent: deploy.bat -- KEEP THE TWO IN SYNC. Any change to the
# file list, tar flags, or remote commands must be made in BOTH scripts.
# tests/test_deploy_parity.py enforces this.
#
# Configuration -- your Pi's hostname etc. -- comes from (highest priority first):
#   1. environment variables: PBPAD_PI / PBPAD_DEST / PBPAD_KEY
#   2. conf/deploy.conf (copy conf/deploy.conf.example to create it)
#   3. the built-in defaults below
# conf/deploy.conf is git-ignored; keep your device's hostname there, not the repo.
set -euo pipefail

cd "$(dirname "$0")"

# Values already set in the environment win over the config file, so remember
# them before sourcing conf/deploy.conf (which assigns the same PBPAD_* names).
_env_pi="${PBPAD_PI:-}"; _env_dest="${PBPAD_DEST:-}"; _env_key="${PBPAD_KEY:-}"
if [ -f conf/deploy.conf ]; then
    # shellcheck disable=SC1091
    . ./conf/deploy.conf
fi
PI="${_env_pi:-${PBPAD_PI:-pi@raspberrypi.local}}"
DEST="${_env_dest:-${PBPAD_DEST:-/home/pi/dev/pbpad}}"
KEY="${_env_key:-${PBPAD_KEY:-$HOME/.ssh/id_pbpad}}"
SSH_OPTS=(-i "$KEY" -o BatchMode=yes -o ConnectTimeout=10)

# Keep this list identical to deploy.bat. Tests, conftest.py and pytest.ini are
# deliberately excluded -- they are dev-only and never run on the Pi.
PATHS=(main.py log.py store.py splash.py splash.fb scripts/gen_splash.py setup.sh requirements.txt app conf hardware pb ui wifi)

fail() {
    echo
    echo "Deploy FAILED."
    exit 1
}
trap fail ERR

echo "== Copying source to $PI:$DEST"

# One tarball for everything, streamed straight over ssh (no temp file).
#   --exclude=__pycache__ : never ship .pyc. Python regenerates it on import,
#                           and stale root-owned cache files have previously
#                           caused "Permission denied" on extract.
#   COPYFILE_DISABLE=1    : macOS bsdtar otherwise embeds AppleDouble "._*"
#                           metadata files in the archive.
# Remote side runs GNU tar:
#   --overwrite                  : unlink each target first; without it GNU tar
#                                  refuses to overwrite files that came from a
#                                  bsdtar archive ("Cannot open: File exists").
#   --warning=no-unknown-keyword : silence the SCHILY.fflags headers bsdtar
#                                  (the default tar on macOS and Windows) writes.
#   --delay-directory-restore    : THE fix for "Cannot open: Permission denied"
#                                  when a deploy adds a NEW directory. bsdtar on
#                                  Windows records dirs as POSIX 555 (the Windows
#                                  ReadOnly attribute); GNU tar would create the
#                                  new dir 555 and then be unable to write the
#                                  files that belong INSIDE it (a 555 dir rejects
#                                  new entries even from its owner). The
#                                  pre-extract chmod can't help — the dir doesn't
#                                  exist yet, it's born read-only mid-extract.
#                                  This flag creates dirs writable during
#                                  extraction and applies their archived modes
#                                  only at the very end, so file writes always
#                                  succeed regardless of the archive's dir modes
#                                  or which OS built it.
#   sed -i 's/\r$//' *.sh        : strip CR from shell scripts. A Windows editor
#                                  (or git autocrlf) can leave setup.sh with CRLF
#                                  line endings; the Pi's shell then fails with
#                                  "/bin/bash^M: bad interpreter". Normalising on
#                                  the Pi makes the deployed scripts LF no matter
#                                  what bytes the working copy holds.
#   chmod +x *.sh                : the execute bit does not survive the
#                                  tarball round-trip from macOS/Windows.
#   chmod -R u+rwX (BOTH SIDES of the extract) : belt-and-suspenders alongside
#                                  --delay-directory-restore. Before: fixes any
#                                  dirs a PRE-fix deploy already left at 555.
#                                  After: undoes the 555 modes the archive
#                                  restores at end-of-extract, so the tree is
#                                  left writable. Harmless from macOS (755).
#   chown -R                     : cheap insurance for a genuinely root-owned
#                                  file; tolerated when sudo is unavailable.
# The \$( ) below is escaped so the remote shell expands it, not this one.
COPYFILE_DISABLE=1 tar --exclude=__pycache__ --exclude=conf/deploy.conf -cf - "${PATHS[@]}" \
    | ssh "${SSH_OPTS[@]}" "$PI" \
        "cd '$DEST' && (sudo -n chown -R \$(id -un):\$(id -gn) . || echo CHOWN-SKIPPED) && chmod -R u+rwX . && tar --overwrite --delay-directory-restore --warning=no-unknown-keyword -xf - && chmod -R u+rwX . && sed -i 's/\r$//' *.sh && chmod +x *.sh"

echo "== Restarting pbpad"
ssh "${SSH_OPTS[@]}" "$PI" 'sudo systemctl restart pbpad && sleep 3 && systemctl is-active pbpad'

echo "== Done"
