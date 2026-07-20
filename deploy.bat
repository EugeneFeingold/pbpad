@echo off
rem Deploy pbpad to the Raspberry Pi and restart the service.
rem Run from a Windows command prompt:  deploy.bat
rem
rem macOS/Linux equivalent: deploy.sh -- KEEP THE TWO IN SYNC. Any change to
rem the file list, tar flags, or remote commands must be made in BOTH scripts.
rem tests/test_deploy_parity.py enforces this.
rem
rem Override the defaults with env vars if needed:
rem   set PBPAD_PI=pi@host
rem   set PBPAD_DEST=/home/pi/dev/pbpad
rem   set PBPAD_KEY=%USERPROFILE%\.ssh\id_pbpad
setlocal
cd /d "%~dp0"

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
tar --exclude=__pycache__ -cf %TAR% main.py config.py log.py store.py splash.py splash.fb gen_splash.py setup.sh requirements.txt hardware pb ui wifi
if errorlevel 1 goto fail

scp -i "%KEY%" -o BatchMode=yes -o ConnectTimeout=10 %TAR% %PI%:%DEST%/
if errorlevel 1 (del %TAR% 2>nul & goto fail)

rem Remote side runs GNU tar:
rem   --overwrite                  : unlink each target first; without it GNU tar
rem                                  refuses to overwrite files that came from a
rem                                  bsdtar archive ("Cannot open: File exists").
rem   --warning=no-unknown-keyword : silence the SCHILY.fflags headers bsdtar
rem                                  (the default tar on Windows and macOS) writes.
rem   chmod +x *.sh                : the execute bit does not survive the
rem                                  tarball round-trip from Windows/macOS.
rem   chmod -R u+rwX (BOTH SIDES of the extract) : the source dirs carry the
rem                                  Windows ReadOnly attribute, which bsdtar
rem                                  faithfully records as POSIX 555. Extracting
rem                                  that makes the package dirs read-only on
rem                                  the Pi, so the NEXT deploy cannot create a
rem                                  new file in them ("Cannot open: Permission
rem                                  denied"). We normalise before (so this
rem                                  extract can write) and after (so the modes
rem                                  the archive just applied don't persist).
rem   chown -R                     : cheap insurance for a genuinely root-owned
rem                                  file; tolerated when sudo is unavailable.
ssh -i "%KEY%" -o BatchMode=yes -o ConnectTimeout=10 %PI% "cd %DEST% && (sudo -n chown -R $(id -un):$(id -gn) . || echo CHOWN-SKIPPED) && chmod -R u+rwX . && tar --overwrite --warning=no-unknown-keyword -xf %TAR% && chmod -R u+rwX . && chmod +x *.sh && rm %TAR%"
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
