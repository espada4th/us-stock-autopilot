@echo off
cd /d "%~dp0"

echo ============================================================
echo  FINISH PUSH - attach main branch to detached HEAD + push
echo ============================================================
echo.

echo [1/4] Checking state (should be detached HEAD on momentum commit)...
git log -1 --format="HEAD at: %%h %%s"
echo.

echo [2/4] Force-moving local 'main' branch to current HEAD...
for /f "delims=" %%s in ('git rev-parse HEAD') do set HEADSHA=%%s
echo   HEAD sha: %HEADSHA%
git branch -f main %HEADSHA%
if errorlevel 1 goto :fail

echo [3/4] Checking out main (attaches HEAD to branch)...
git checkout main
if errorlevel 1 goto :fail
git log --oneline -3

echo [4/4] Pushing to origin/main (fast-forward expected)...
git push origin main
if errorlevel 1 goto :fail

echo.
echo ==========================================================
echo  SUCCESS! Momentum commit pushed.
echo.
echo  Trigger Refresh workflow now:
echo  https://github.com/espada4th/us-stock-autopilot/actions
echo   -^> Click "Refresh dashboard data"
echo   -^> Click "Run workflow"
echo.
echo  After ~5 min, dashboard will show momentum column, rotation
echo  pills, NEW badges, and 3 new KPI cards.
echo ==========================================================
goto :end

:fail
echo.
echo ERROR. Check 'git status' and 'git log --oneline -5' to debug.

:end
echo.
pause
