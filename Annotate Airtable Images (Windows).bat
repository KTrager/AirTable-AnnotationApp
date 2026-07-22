@echo off
rem ===========================================================================
rem  Double-click this file to open the Airtable Image Annotator on Windows.
rem  It looks for Python and starts the app with it; the app itself opens
rem  in your normal web browser.
rem ===========================================================================
cd /d "%~dp0"

where py >nul 2>nul && ( py -3 airtable_image_annotator_app.py & goto :eof )
where python3 >nul 2>nul && ( python3 airtable_image_annotator_app.py & goto :eof )
where python >nul 2>nul && ( python airtable_image_annotator_app.py & goto :eof )

echo Python was not found on this computer.
echo.
echo Please install it first:
echo   1. Go to  https://www.python.org/downloads/
echo   2. Click the big yellow Download button and run the installer.
echo   3. IMPORTANT: tick "Add python.exe to PATH" on the first screen.
echo   4. Then double-click this file again.
echo.
pause
