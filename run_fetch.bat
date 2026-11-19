@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
set PY=
where py >nul 2>nul && set PY=py
if not defined PY where python >nul 2>nul && set PY=python
if not defined PY (
  echo 找不到 Python。請到 https://www.python.org/downloads/ 安裝，安裝時勾選 "Add python.exe to PATH"，再重新執行這個檔案。
  echo 找不到 Python > fetch_log.txt
  pause
  exit /b 1
)
echo 安裝 requests 套件...
%PY% -m pip install --quiet --disable-pip-version-check requests
echo 開始抓資料，第一次約 20~40 分鐘，請不要關閉這個視窗。
%PY% fetch_data.py %*
echo.
echo 完成後請用瀏覽器開啟 index.html
pause
