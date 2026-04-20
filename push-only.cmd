@echo off
cd /d "%~dp0"

echo [1/4] Removing stale .git\index.lock (if any)...
if exist ".git\index.lock" del /f /q ".git\index.lock"

echo [2/5] Staging all tracked changes + specific infra files...
REM Include infra + self-modifying files to avoid unstaged pull conflicts.
git add scripts/build_universe.py
git add scripts/refresh_data.py
git add scripts/gen_ticker_pages.py
git add narratives_manual.json
git add watchlist.json
git add .github/workflows/refresh.yml
git add .github/workflows/pages.yml
git add index.html
git add ticker/AAPL.html
git add docs/finnhub-setup.md
git add push-only.cmd
git add .gitignore

echo [3/5] Creating commit (skip if nothing to commit)...
git commit -m "feat: momentum score + rotation flag + change badges + smart cron" 2>nul

echo [4/5] Stashing any remaining unstaged changes (safety net for rebase)...
git stash push -u -m "auto-stash-by-push-only" 2>nul

echo [5/5] Pulling + pushing...
git pull --rebase origin main
git push origin main

echo Restoring stashed changes (if any)...
git stash pop 2>nul

echo.
echo ==========================================================
echo  Done. Now trigger the Refresh workflow manually:
echo  https://github.com/espada4th/us-stock-autopilot/actions
echo.
echo  On first run it will:
echo   1. Build universe.json (~1500 tickers + ASTS from watchlist)
echo   2. Scan all tickers with yfinance (~3-5 min)
echo   3. If FINNHUB_API_KEY secret set: enrich top-50 with news/insider
echo   4. Write top-50 to dataset.json (every ticker now has star rating)
echo   5. Regenerate ticker/*.html pages
echo   6. Commit + push, triggering Deploy workflow
echo.
echo  One-time setup for Finnhub (optional but recommended):
echo   See docs\finnhub-setup.md for signup + GitHub secret instructions
echo ==========================================================
pause
