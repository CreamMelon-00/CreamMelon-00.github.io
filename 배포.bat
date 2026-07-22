@echo off
chcp 65001 >nul
cd /d D:\StoryProject

echo [1/3] Draft -^> storypack ...
python tools\inject_episode.py --all
if errorlevel 1 goto :fail

echo.
echo [2/3] git commit ...
git add data resource script
git commit -m "story content update"

echo.
echo [3/3] git push ...
git push
if errorlevel 1 goto :fail

echo.
echo  Done! Site updates in 1-3 minutes.
echo  https://creammelon-00.github.io/
pause
exit /b 0

:fail
echo.
echo  ERROR - check the messages above.
pause
exit /b 1
