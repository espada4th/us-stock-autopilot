@echo off
REM finish-deploy.cmd
REM ------------------
REM Cleans up the stale git index lock and pushes the latest dashboard state
REM to GitHub (main). Double-click or run from cmd in this folder.

cd /d "%~dp0"

echo [1/6] Removing stale .git/index.lock (if any)...
if exist ".git\index.lock" del /f /q ".git\index.lock"

echo [2/6] Refreshing git index so Windows case-sensitivity quirks clear...
git reset >nul 2>&1

echo [3/6] git status (before):
git status --short

echo [4/6] Staging all changes (scripts, workflows, dataset, tickers, index.html, README)...
git add -A

echo [5/6] Creating commit...
git commit -m "refresh: sanitize refresh_data.py + redeploy dashboard" || echo (nothing to commit, continuing)

echo [6/8] Fetching + rebasing on origin/main (pick up any GitHub Actions commits)...
git fetch origin main
git pull --rebase origin main
if errorlevel 1 (
  echo.
  echo REBASE HIT A CONFLICT. Run these manually:
  echo   git status
  echo   git rebase --abort   ^(to bail out^)
  echo   or resolve, then: git rebase --continue
  pause
  exit /b 1
)

echo [7/8] Pushing to origin/main...
git push origin main

echo [8/8] Done.
echo View: https://espada4th.github.io/us-stock-autopilot/
echo Actions: https://github.com/espada4th/us-stock-autopilot/actions
pause
