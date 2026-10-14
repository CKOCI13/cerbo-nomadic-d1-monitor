@echo off
setlocal
title Nomadic DC-DC monitor - Cerbo GX setup
cd /d "%~dp0"

set "DRIVER=dbus-nomadic-dcdc"
set "REMOTE=/data/dbus-nomadic-dcdc"
set "SSHOPT=-o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"
set "PACK=tar --exclude=__pycache__ --exclude=state*.json --exclude=ui*.json -cf - %DRIVER%"
set "UNPACK=tar -xf - -C /data && chmod -R u=rwX,go=rX %REMOTE%"

if not exist "%DRIVER%\dbus-nomadic-dcdc.py" (
    echo.
    echo  The folder "%DRIVER%" must be next to this file.
    echo.
    pause
    exit /b 1
)

set "CERBO="
if exist cerbo-ip.txt set /p CERBO=<cerbo-ip.txt
if not defined CERBO call :askip

:menu
cls
echo.
echo  ===========================================================
echo    Nomadic DC-DC monitor  -  Cerbo GX setup
echo    Cerbo address: %CERBO%
echo    Monitor only: nothing is ever sent to the converter
echo  ===========================================================
echo.
echo    1  Edit the settings (config.ini)
echo    2  Find which Cerbo port the converter is on
echo    3  Watch live readings from the converter
echo    4  Install or update the monitor on the Cerbo
echo    5  Show the monitor log
echo.
echo    9  Try it with a pretend converter (simulation)
echo.
echo    6  Change the Cerbo address
echo    7  Fix "REMOTE HOST IDENTIFICATION HAS CHANGED"
echo    8  Uninstall the monitor from the Cerbo
echo    0  Exit
echo.
set "CHOICE="
set /p "CHOICE=  Type a number and press Enter: "
if "%CHOICE%"=="1" goto edit
if "%CHOICE%"=="2" goto scan
if "%CHOICE%"=="3" goto watch
if "%CHOICE%"=="4" goto install
if "%CHOICE%"=="5" goto log
if "%CHOICE%"=="6" goto changeip
if "%CHOICE%"=="7" goto hostkey
if "%CHOICE%"=="8" goto uninstall
if "%CHOICE%"=="9" goto sim
if "%CHOICE%"=="0" exit /b 0
goto menu

:edit
start "" notepad "%DRIVER%\config.ini"
goto menu

:scan
call :header "Looking for the converter on every CAN port"
%PACK% | ssh %SSHOPT% root@%CERBO% "%UNPACK% && python3 -u %REMOTE%/dbus-nomadic-dcdc.py --scan"
goto done

:watch
call :header "Live readings from the converter"
echo  To stop: press Ctrl+C, then type N and press Enter.
echo.
%PACK% | ssh %SSHOPT% root@%CERBO% "%UNPACK% && python3 -u %REMOTE%/dbus-nomadic-dcdc.py --monitor"
goto done

:install
call :header "Installing / updating the monitor"
%PACK% | ssh %SSHOPT% root@%CERBO% "%UNPACK% && sh %REMOTE%/install.sh && (svc -t /service/dbus-nomadic-dcdc 2>/dev/null; echo Starting...; sleep 8; svstat /service/dbus-nomadic-dcdc; echo; tail -n 12 /var/log/dbus-nomadic-dcdc/current 2>/dev/null | tai64nlocal)"
echo.
echo  If you see "up (pid ...)" above, the monitor is running.
goto done

:log
call :header "Monitor log"
echo  To stop: press Ctrl+C, then type N and press Enter.
echo.
ssh -t %SSHOPT% root@%CERBO% "tail -n 40 -F /var/log/dbus-nomadic-dcdc/current | tai64nlocal"
goto done

:sim
call :header "Simulation: a pretend D1 on the Cerbo screen, VRM and web page"
echo  No CAN bus is used at all, and a real installation keeps running.
echo  You'll be asked for the password twice. While this window is open,
echo  "DC-DC D1 (SIMULATED)" appears in the Cerbo's switch pane, and its web
echo  page is at http://%CERBO%:8089
echo  To stop: press Ctrl+C (then type N and press Enter if asked).
echo.
%PACK% | ssh %SSHOPT% root@%CERBO% "%UNPACK%"
ssh -t %SSHOPT% root@%CERBO% "python3 -u %REMOTE%/dbus-nomadic-dcdc.py --simulate"
goto done

:changeip
call :askip
goto menu

:hostkey
ssh-keygen -R %CERBO%
echo.
echo  Done. The next connection will ask nothing and remember the Cerbo again.
goto done

:uninstall
echo.
set "SURE="
set /p "SURE=  Type YES to remove the monitor from the Cerbo: "
if /i not "%SURE%"=="YES" goto menu
call :header "Uninstalling"
ssh %SSHOPT% root@%CERBO% "sh %REMOTE%/uninstall.sh; rm -rf %REMOTE%"
goto done

:done
echo.
echo  -----------------------------------------------------------
pause
goto menu

:header
cls
echo.
echo  %~1
echo  -----------------------------------------------------------
echo  When asked for a password, type the Cerbo's root password and
echo  press Enter. Nothing appears on screen while you type - that's normal.
echo.
exit /b

:askip
echo.
echo  Find the Cerbo's IP address on its screen:
echo  Settings ^> Connectivity ^> Ethernet (or Wi-Fi) ^> IP address
echo.
set "CERBO="
set /p "CERBO=  Type the IP address (for example 192.168.1.50) and press Enter: "
if not defined CERBO goto askip
> cerbo-ip.txt echo %CERBO%
exit /b
