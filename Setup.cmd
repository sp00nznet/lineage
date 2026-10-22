@echo off
rem lineage quick start for Windows. Double-click it. Safe to run again:
rem every step that is already done is skipped. Runs exactly the commands in
rem the README's "Step by step", so the two routes cannot drift apart.
setlocal EnableExtensions
cd /d "%~dp0"
set "LOG=%~dp0setup.log"
echo lineage setup %DATE% %TIME% > "%LOG%"
echo.
echo  lineage quick start
echo  -------------------

rem 1. Python 3.9 or newer. "python" may be the Microsoft Store alias, which
rem    fails this check, so the py launcher is tried first.
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >>"%LOG%" 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >>"%LOG%" 2>&1 && set "PY=python"
if defined PY goto :have_python

echo.
echo  Python 3.9 or newer is needed and was not found.
echo  It can be installed now with winget: Python 3.12 from python.org,
echo  about 25 MB, for this user only, no administrator needed.
choice /c YN /m " Install Python 3.12 now"
if errorlevel 2 (
  echo  Setup stopped. Install Python 3.9+ from https://www.python.org, then run Setup.cmd again.
  exit /b 1
)
winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements >>"%LOG%" 2>&1
if errorlevel 1 (
  echo  Python could not be installed with winget. Install it from https://www.python.org and run Setup.cmd again. Details: %LOG%
  exit /b 1
)
echo  Python installed. Close this window and run Setup.cmd again so Windows picks up the new PATH.
pause
exit /b 0

:have_python
for /f "delims=" %%v in ('%PY% --version') do echo  [ok] %%v

rem 2. Self-test. No test framework needed.
%PY% tests\run.py >>"%LOG%" 2>&1
if errorlevel 1 (
  echo  The self-test failed, so nothing else was run. Details: %LOG%
  exit /b 1
)
echo  [ok] self-test passed

rem 3. The example policy becomes this machine's cached policy.
if not exist var\agent-state\policy.json (
  mkdir var\agent-state 2>nul
  copy /y var\policy.json var\agent-state\policy.json >>"%LOG%" 2>&1
)
echo  [ok] example policy in var\agent-state

rem 4. First evaluation of this machine, offline: no server, nothing sent anywhere.
%PY% -m lineage.agent --once --offline --state var\agent-state
if errorlevel 1 (
  echo  The first evaluation failed. Details: %LOG%
  exit /b 1
)

rem 5. A launcher that does step 4 again and shows the answers.
if not exist Lineage.cmd (
  > Lineage.cmd echo @echo off
  >> Lineage.cmd echo cd /d "%%~dp0"
  >> Lineage.cmd echo %PY% -m lineage.agent --once --offline --state var\agent-state
  >> Lineage.cmd echo %PY% -m json.tool var\agent-state\last-report.json
  >> Lineage.cmd echo pause
)
echo  [ok] Lineage.cmd created: double-click it to evaluate this machine again.
echo.
echo  Done. The full answers are in var\agent-state\last-report.json.
echo  A "failed" item is a check that could not run (BitLocker needs administrator),
echo  reported as an error rather than as a false answer.
pause
exit /b 0
