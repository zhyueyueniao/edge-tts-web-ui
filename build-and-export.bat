@echo off
chcp 65001 >nul
REM ============================================
REM  edge-tts 镜像一键构建并导出
REM  产物：edge-tts-web-ui.tar（tag 固定 latest，不含版本号）
REM  导入：docker load -i edge-tts-web-ui.tar
REM ============================================

set IMAGE=edge-tts-web-ui
set TAG=latest
REM 国内加速：取消下一行注释
REM set PIP_MIRROR=--build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

echo [1/3] 构建镜像 %IMAGE%:%TAG% ...
docker build %PIP_MIRROR% -t %IMAGE%:%TAG% .
if errorlevel 1 goto :fail

echo [2/3] 冒烟测试容器 ...
docker rm -f edge-tts-smoke >nul 2>&1
docker run -d --name edge-tts-smoke -p 15050:5050 %IMAGE%:%TAG%
timeout /t 6 >nul
docker exec edge-tts-smoke python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:5050/health').read().decode())"
docker rm -f edge-tts-smoke >nul

echo [3/3] 导出镜像 ...
docker save -o %IMAGE%.tar %IMAGE%:%TAG%
echo 完成：%IMAGE%.tar
echo 导入命令：docker load -i %IMAGE%.tar
goto :eof

:fail
echo 构建失败
exit /b 1
