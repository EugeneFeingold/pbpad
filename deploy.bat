@echo off
rem Deploy pbpad to the Raspberry Pi and restart the service.
rem Run from a Windows command prompt:  deploy.bat
rem
rem macOS/Linux equivalent: deploy.sh -- KEEP THE TWO IN SYNC. Any change to
rem the file list, tar flags, or remote commands must be made in BOTH scripts.
rem tests/test_deploy_parity.py enforces this.
rem
rem Configuration -- your Pi's hostname etc. -- comes from (highest priority first):
rem   1. environment variables: PBPAD_PI / PBPAD_DEST / PBPAD_KEY
rem   2. conf\deploy.conf (copy conf\deploy.conf.example to create it)
rem   3. the built-in defaults below
rem conf\deploy.conf is git-ignored; keep your device's hostname there, not the repo.
setlocal
cd /d "%~dp0"

rem Load conf\deploy.conf if present. `if not defined` means an env var of the
rem same name (set before running) keeps priority; lines starting with # skip.
if exist "conf\deploy.conf" (
    for /f "usebackq eol=# tokens=1* delims==" %%a in ("conf\deploy.conf") do (
        if not defined %%a set "%%a=%%b"
    )
)
if not defined PBPAD_PI   set "PBPAD_PI=pi@raspberrypi.local"
if not defined PBPAD_DEST set "PBPAD_DEST=/home/pi/dev/pbpad"
if not defined PBPAD_KEY  set "PBPAD_KEY=%USERPROFILE%\.ssh\id_pbpad"

set "PI=%PBPAD_PI%"
set "DEST=%PBPAD_DEST%"
set "KEY=%PBPAD_KEY%"
set "TAR=pbpad.tar"

echo == Copying source to %PI%:%DEST%

rem One tarball for everything. Windows cmd pipes mangle binary streams, so we
rem stage a temp tarball and scp it (deploy.sh streams it instead -- same result).
rem   --exclude=__pycache__ : never ship .pyc. Python regenerates it on import,
rem                           and stale root-owned cache files have previously
rem                           caused "Permission denied" on extract.
rem Keep this list identical to deploy.sh. Tests, conftest.py and pytest.ini are
rem deliberately excluded -- they are dev-only and never run on the Pi.
tar --exclude=__pycache__ --exclude=conf/deploy.conf -cf %TAR% main.py log.py store.py splash.py splash.fb scripts/gen_splash.py setup.sh requirements.txt app conf hardware pb ui wifi
if errorlevel 1 goto fail

scp -i "%KEY%" -o BatchMode=yes -o ConnectTimeout=10 %TAR% %PI%:%DEST%/
if errorlevel 1 (del %TAR% 2>nul & goto fail)

rem Remote side runs GNU tar:
rem   --overwrite                  : unlink each target first; without it GNU tar
rem                                  refuses to overwrite files that came from a
rem                                  bsdtar archive ("Cannot open: File exists").
rem   --warning=no-unknown-keyword : silence the SCHILY.fflags headers bsdtar
rem                                  (the default tar on Windows and macOS) writes.
rem   --delay-directory-restore    : THE fix for "Cannot open: Permission denied"
rem                                  when a deploy adds a NEW directory. bsdtar on
rem                                  Windows records dirs as POSIX 555 (the Windows
rem                                  ReadOnly attribute); GNU tar would create the
rem                                  new dir 555 and then be unable to write the
rem                                  files that belong INSIDE it (a 555 dir rejects
rem                                  new entries even from its owner). The
rem                                  pre-extract chmod can't help -- the dir does
rem                                  not exist yet, it is born read-only
rem                                  mid-extract. This flag creates dirs writable
rem                                  during extraction and applies their archived
rem                                  modes only at the very end, so file writes
rem                                  always succeed regardless of archive dir modes.
rem   chmod +x *.sh                : the execute bit does not survive the
rem                                  tarball round-trip from Windows/macOS.
rem   chmod -R u+rwX (BOTH SIDES of the extract) : belt-and-suspenders alongside
rem                                  --delay-directory-restore. Before: fixes any
rem                                  dirs a PRE-fix deploy already left at 555.
rem                                  After: undoes the 555 modes the archive
rem                                  restores at end-of-extract, so the tree is
rem                                  left writable.
rem   chown -R                     : cheap insurance for a genuinely root-owned
rem                                  file; tolerated when sudo is unavailable.
ssh -i "%KEY%" -o BatchMode=yes -o ConnectTimeout=10 %PI% "cd %DEST% && (sudo -n chown -R $(id -un):$(id -gn) . || echo CHOWN-SKIPPED) && chmod -R u+rwX . && tar --overwrite --delay-directory-restore --warning=no-unknown-keyword -xf %TAR% && chmod -R u+rwX . && chmod +x *.sh && rm %TAR%"
if errorlevel 1 (del %TAR% 2>nul & goto fail)
del %TAR%

echo == Restarting pbpad
ssh -i "%KEY%" -o BatchMode=yes -o ConnectTimeout=10 %PI% "sudo systemctl restart pbpad && sleep 3 && systemctl is-active pbpad"
if errorlevel 1 goto fail

echo == Done
exit /b 0

:fail
echo.
echo Deploy FAILED.
exit /b 1
