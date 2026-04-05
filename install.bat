@echo off
echo ====================================
echo  GameStream - Installation
echo ====================================
echo.

:: Verifier que Python est installe
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERREUR] Python n'est pas installe ou pas dans le PATH.
    echo Telechargez Python sur https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [OK] Python detecte :
python --version
echo.

:: Mettre a jour pip
echo Mise a jour de pip...
python -m pip install --upgrade pip
echo.

:: Installer les dependances
echo Installation des dependances...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERREUR] L'installation a echoue. Verifiez les messages ci-dessus.
    pause
    exit /b 1
)

echo.
echo ====================================
echo  Installation terminee avec succes !
echo ====================================
echo.
echo Vous pouvez maintenant lancer l'application avec : python launch.py
echo.
pause
