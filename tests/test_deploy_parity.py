"""deploy.bat and deploy.sh must stay feature-equivalent.

The project is developed on both Windows and macOS, so the two deploy scripts
are maintained in parallel. These tests fail loudly if one is edited without
the other — the file list is the part most likely to drift (a new source file
added to one script and forgotten in the other deploys a broken tree).
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAT = os.path.join(ROOT, "deploy.bat")
SH = os.path.join(ROOT, "deploy.sh")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def bat_paths():
    """File list from deploy.bat's `tar ... -cf %TAR% <paths>` line."""
    m = re.search(r"^tar .*-cf %TAR% (.+)$", read(BAT), re.MULTILINE)
    assert m, "could not find the tar line in deploy.bat"
    return m.group(1).split()


def sh_paths():
    """File list from deploy.sh's `PATHS=( ... )` array."""
    m = re.search(r"^PATHS=\((.*?)\)", read(SH), re.MULTILINE | re.DOTALL)
    assert m, "could not find PATHS=(...) in deploy.sh"
    return m.group(1).split()


def test_file_lists_match():
    assert bat_paths() == sh_paths()


def test_file_list_entries_exist():
    for entry in sh_paths():
        assert os.path.exists(os.path.join(ROOT, entry)), \
            f"deploy list references missing path: {entry}"


def test_tests_are_not_deployed():
    # Dev-only files must never ship to the Pi.
    for entry in sh_paths():
        assert entry not in ("tests", "conftest.py", "pytest.ini",
                             "requirements-dev.txt")


@pytest.mark.parametrize("fragment", [
    "--exclude=__pycache__",          # never ship stale bytecode
    "--overwrite",                    # GNU tar must unlink before writing
    "--warning=no-unknown-keyword",   # silence bsdtar SCHILY.fflags headers
    "chmod +x *.sh",                  # execute bit lost in the round-trip
    "chown -R",                       # reclaim root-owned files before extract
    "id -un",                         # ...as the connecting user, not hardcoded
    "chmod -R u+rwX",                 # undo 555 dirs from Windows ReadOnly attr
    "systemctl restart pbpad",        # restart the service
    "systemctl is-active pbpad",      # and verify it came back
    "BatchMode=yes",                  # never hang on an interactive prompt
    "ConnectTimeout=10",
])
def test_both_scripts_share_behavior(fragment):
    assert fragment in read(BAT), f"deploy.bat missing: {fragment}"
    assert fragment in read(SH), f"deploy.sh missing: {fragment}"


@pytest.mark.parametrize("var", ["PBPAD_PI", "PBPAD_DEST", "PBPAD_KEY"])
def test_both_support_env_overrides(var):
    assert var in read(BAT), f"deploy.bat missing env override: {var}"
    assert var in read(SH), f"deploy.sh missing env override: {var}"


def test_both_load_the_config_file():
    # Settings should come from a git-ignored deploy.conf, not just env vars.
    assert "deploy.conf" in read(BAT), "deploy.bat does not read deploy.conf"
    assert "deploy.conf" in read(SH), "deploy.sh does not read deploy.conf"


def test_config_example_is_committed():
    # The sample must exist so users know the format; the real one is ignored.
    assert os.path.exists(os.path.join(ROOT, "deploy.conf.example"))


def test_real_config_is_gitignored():
    gitignore = read(os.path.join(ROOT, ".gitignore"))
    assert re.search(r"^deploy\.conf$", gitignore, re.MULTILINE), \
        "deploy.conf must be git-ignored so a real hostname is never committed"


def test_both_report_failure():
    assert "Deploy FAILED." in read(BAT)
    assert "Deploy FAILED." in read(SH)


def test_sh_has_shebang_and_strict_mode():
    text = read(SH)
    assert text.startswith("#!")
    assert "set -euo pipefail" in text


def test_sh_disables_macos_applddouble():
    # macOS bsdtar embeds "._*" AppleDouble files unless this is set.
    assert "COPYFILE_DISABLE=1" in read(SH)


def test_each_points_at_the_other():
    assert "deploy.sh" in read(BAT)
    assert "deploy.bat" in read(SH)


@pytest.mark.parametrize("path,text", [("deploy.bat", None), ("deploy.sh", None)])
def test_chmod_runs_on_both_sides_of_extract(path, text):
    """The permission normalise must happen BEFORE the extract (so it can write
    into an already-read-only dir) AND AFTER (so the 555 modes the archive just
    applied don't break the next deploy). One or the other is not enough."""
    body = read(os.path.join(ROOT, path))
    line = next(ln for ln in body.splitlines()
                if "tar --overwrite" in ln and "chmod -R u+rwX" in ln)
    before, _, after = line.partition("tar --overwrite")
    assert "chmod -R u+rwX" in before, f"{path}: missing chmod before extract"
    assert "chmod -R u+rwX" in after, f"{path}: missing chmod after extract"
