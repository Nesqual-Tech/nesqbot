@echo off
setlocal
rem Azure Trusted Signing shim for Tauri's bundle.windows.signCommand.
rem
rem Tauri hands us one argument: the file to sign. Everything else is fixed by
rem the signing account, so it lives here rather than in tauri.conf.json, where
rem a long quoted command line is its own source of bugs.
rem
rem Why not `certificateThumbprint`: Trusted Signing has no local key. The
rem certificate is minted per request and lives about three days; what keeps a
rem shipped binary valid long after that is the RFC-3161 timestamp, which is
rem why /tr is not optional here the way it nearly is with a long-lived cert.
rem
rem Invoked as `cmd.exe /c ..\scripts\sign-trusted.cmd %1` — backslashes, not
rem forward slashes. cmd.exe reads a leading `../` as a switch, not a path, and
rem answers "'..' is not recognized", which reaches Tauri as the far less
rem helpful "failed to run cmd".
rem
rem The dlib is a downloaded Microsoft tool (Microsoft.Trusted.Signing.Client)
rem and is deliberately not committed. Point NESQ_SIGN_TOOLS at wherever it was
rem unpacked; the default matches the documented setup.

if "%NESQ_SIGN_TOOLS%"=="" set "NESQ_SIGN_TOOLS=C:\nesqsign"

set "DLIB=%NESQ_SIGN_TOOLS%\client\bin\x64\Azure.CodeSigning.Dlib.dll"
set "META=%NESQ_SIGN_TOOLS%\metadata.json"

if not exist "%DLIB%" (
  echo [sign-trusted] Azure.CodeSigning.Dlib.dll not found at "%DLIB%".
  echo [sign-trusted] Refusing to continue: the build must NOT silently produce
  echo [sign-trusted] an unsigned bundle. See scripts\README-signing.md.
  exit /b 1
)
if not exist "%META%" (
  echo [sign-trusted] metadata.json not found at "%META%".
  exit /b 1
)

rem Highest installed SDK wins. Ascending order and plain overwrite, because
rem `if not defined` inside a for body needs delayed expansion to read what the
rem same iteration just set. A wildcard cannot sit in a middle path component
rem of `dir /s`, which is why the version directories are enumerated first.
set "SDKBIN=%ProgramFiles(x86)%\Windows Kits\10\bin"
set "SIGNTOOL=%NESQ_SIGNTOOL%"
if "%SIGNTOOL%"=="" (
  for /f "delims=" %%D in ('dir /b /ad /on "%SDKBIN%\10.*" 2^>nul') do (
    if exist "%SDKBIN%\%%D\x64\signtool.exe" set "SIGNTOOL=%SDKBIN%\%%D\x64\signtool.exe"
  )
)
if "%SIGNTOOL%"=="" (
  echo [sign-trusted] signtool.exe not found under "%SDKBIN%".
  echo [sign-trusted] Set NESQ_SIGNTOOL to its full path.
  exit /b 1
)

echo [sign-trusted] signtool: %SIGNTOOL%
"%SIGNTOOL%" sign /v /fd SHA256 /tr "http://timestamp.acs.microsoft.com" /td SHA256 /dlib "%DLIB%" /dmdf "%META%" %1
exit /b %ERRORLEVEL%
