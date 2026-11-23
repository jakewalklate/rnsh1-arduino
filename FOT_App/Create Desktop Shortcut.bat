@echo off
setlocal
set "HERE=%~dp0"
powershell -NoProfile -Command ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut(\"$env:USERPROFILE\Desktop\Oscillometry Measurement System.lnk\"); " ^
  "$s.TargetPath = '%HERE%FOT_App.exe'; " ^
  "$s.WorkingDirectory = '%HERE%'; " ^
  "$s.IconLocation = '%HERE%FOT_App.exe'; " ^
  "$s.Description = 'Oscillometry Measurement System'; " ^
  "$s.Save()"
echo.
echo Desktop shortcut created: "Oscillometry Measurement System"
pause
