@echo off
REM ---------------------------------------------------------------------
REM  Build the Green Vision APK.
REM
REM  Run this by double-clicking it, or from cmd / PowerShell. It must run
REM  in a normal Windows terminal: Gradle forks a build process and talks
REM  to it over a loopback socket, and a sandboxed shell can block that
REM  fork even where plain loopback works. The symptom is
REM      java.io.IOException: Unable to establish loopback connection
REM  before any compilation starts.
REM ---------------------------------------------------------------------
setlocal

set REPO=%~dp0
set JAVA_HOME=C:\gvsdk\jdk\jdk-17.0.20.1+1
set ANDROID_HOME=C:\gvsdk
set ANDROID_SDK_ROOT=C:\gvsdk
set GRADLE=C:\gvsdk\gradle-8.7\bin\gradle.bat

if not exist "%JAVA_HOME%" (
  echo JDK 17 not found at %JAVA_HOME%
  echo See android\README.md for how to fetch the toolchain.
  pause & exit /b 1
)

echo.
echo [1/2] Baking the five-city bundle...
call "%REPO%.venv\Scripts\python.exe" "%REPO%scripts\build_static.py" --out dist_app ^
  --cities config/city.yaml config/delhi.yaml config/mumbai.yaml ^
           config/bengaluru.yaml config/chennai.yaml
if errorlevel 1 ( echo Bake failed. & pause & exit /b 1 )

echo.
echo [2/2] Building the APK...
cd /d "%REPO%android"
call "%GRADLE%" --no-daemon assembleRelease
echo.

set APK=%REPO%android\app\build\outputs\apk\release\app-release.apk
if exist "%APK%" (
  echo   APK built: %APK%
  for %%A in ("%APK%") do echo   Size: %%~zA bytes
  echo.
  echo   Install on a connected phone with USB debugging on:
  echo     C:\gvsdk\platform-tools\adb.exe install -r "%APK%"
) else (
  echo   No APK produced - see the Gradle output above.
)
echo.
pause
