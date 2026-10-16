; Apartment Planner - Windows setup (NSIS). Build: makensis windows/installer.nsi  (see scripts/build_windows_installer.sh)
; Per-user install, no admin rights. Unpacks the app, then windows\install.ps1 installs Python (if needed),
; all dependencies, shortcuts and the Claude link.
Unicode true
!define APPNAME "Apartment Planner"
!define COMPANY "IVAI"
!ifndef VERSION
  !define VERSION "1.0.0"
!endif
!define UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\ApartmentPlanner"

Name "${APPNAME}"
OutFile "..\ApartmentPlanner-Setup.exe"
InstallDir "$LOCALAPPDATA\ApartmentPlanner"
RequestExecutionLevel user
SetCompressor /SOLID lzma
Icon "planner.ico"
UninstallIcon "planner.ico"
BrandingText "${APPNAME} ${VERSION}"
VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "${APPNAME}"
VIAddVersionKey "FileDescription" "${APPNAME} Setup"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "CompanyName" "${COMPANY}"
VIAddVersionKey "LegalCopyright" "Apache-2.0"

!include "MUI2.nsh"
!include "LogicLib.nsh"
!define MUI_ICON "planner.ico"
!define MUI_UNICON "planner.ico"
!define MUI_WELCOMEPAGE_TITLE "Install ${APPNAME}"
!define MUI_WELCOMEPAGE_TEXT "Pick a plot on a map and get apartment floor plans checked against DCPR 2034 and NBC 2016.$\r$\n$\r$\nSetup installs Python 3.12 if it is missing, then the planner and its libraries (about 400 MB, internet needed). No administrator rights are needed.$\r$\n$\r$\nClick Install to continue."
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Open ${APPNAME}"
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchApp
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_LICENSE "..\LICENSE"
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Section "Install"
  SetOutPath "$INSTDIR\src"
  RMDir /r "$INSTDIR\src"
  SetOutPath "$INSTDIR\src\engine"
  File /r /x ".venv" /x "__pycache__" /x "*.egg-info" /x "dist" /x "build" /x ".pytest_cache" /x ".ruff_cache" "..\engine\*.*"
  SetOutPath "$INSTDIR\src\windows"
  File "install.ps1"
  File "launch.pyw"
  File "planner.ico"
  SetOutPath "$INSTDIR"
  File "..\LICENSE"

  DetailPrint "Installing Python (if needed), the planner and its libraries..."
  DetailPrint "A console window shows the progress. This takes a few minutes the first time."
  ExecWait 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$INSTDIR\src\windows\install.ps1" -Source "$INSTDIR\src" -InstallDir "$INSTDIR" -Quiet' $0
  ${If} $0 != 0
    MessageBox MB_ICONSTOP "Installation did not finish (code $0).$\r$\nSee %TEMP%\ApartmentPlanner-install.log, check the internet connection and run Setup again."
    Abort
  ${EndIf}

  WriteUninstaller "$INSTDIR\Uninstall.exe"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayName" "${APPNAME}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINSTKEY}" "Publisher" "${COMPANY}"
  WriteRegStr HKCU "${UNINSTKEY}" "DisplayIcon" "$INSTDIR\planner.ico"
  WriteRegStr HKCU "${UNINSTKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINSTKEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoRepair" 1
SectionEnd

Function LaunchApp
  Exec '"$INSTDIR\venv\Scripts\pythonw.exe" "$INSTDIR\launch.pyw"'
FunctionEnd

Section "Uninstall"
  ; stop a running engine
  nsExec::Exec 'powershell.exe -NoProfile -Command "try { $$i = Get-Content \"$$env:USERPROFILE\.apartment-planner\server.json\" | ConvertFrom-Json; Stop-Process -Id $$i.pid -Force } catch {}"'
  Delete "$DESKTOP\Apartment Planner.lnk"
  Delete "$SMPROGRAMS\Apartment Planner.lnk"
  RMDir /r "$INSTDIR\venv"
  RMDir /r "$INSTDIR\app"
  RMDir /r "$INSTDIR\src"
  Delete "$INSTDIR\launch.pyw"
  Delete "$INSTDIR\planner.cmd"
  Delete "$INSTDIR\planner.ico"
  Delete "$INSTDIR\LICENSE"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${UNINSTKEY}"
  ; projects in %USERPROFILE%\.apartment-planner are kept
SectionEnd
