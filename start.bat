@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto fail
)
".venv\Scripts\python.exe" -c "import pyautogui, pytesseract; from PIL import ImageGrab" >nul 2>&1
if errorlevel 1 (
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto fail
)
rem OCR engine for reading Entry/SL/TP. Optional: the scanner still runs without it.
if not exist "%ProgramFiles%\Tesseract-OCR\tesseract.exe" if not exist "%LocalAppData%\Programs\Tesseract-OCR\tesseract.exe" (
  where tesseract >nul 2>&1
  if errorlevel 1 (
    echo Tesseract OCR not found. Trying to install it with winget...
    winget install -e --id UB-Mannheim.TesseractOCR --silent --accept-package-agreements --accept-source-agreements
    if errorlevel 1 echo Could not install Tesseract automatically. Install it manually, then restart.
  )
)
".venv\Scripts\python.exe" majix.py
if errorlevel 1 goto fail
exit /b 0
:fail
echo Setup or execution failed. See the error above.
pause
exit /b 1
