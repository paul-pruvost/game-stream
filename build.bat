@echo off
echo ====================================
echo  GameStream - Build EXE
echo ====================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Python introuvable dans le PATH.
    pause
    exit /b 1
)

:: Verifier que PyInstaller est installe
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo Installation de PyInstaller...
    python -m pip install pyinstaller -q
)

echo Construction de GameStream.exe...
python -m PyInstaller GameStream.spec --noconfirm

if errorlevel 1 (
    echo.
    echo [ERREUR] La compilation a echoue.
    pause
    exit /b 1
)

echo.
echo ====================================
echo  GameStream.exe cree dans dist\
echo ====================================
echo.
pause
