@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ================================================
echo  农产品溯源平台 - 用户与运营域  本地运行
echo ================================================
echo.

echo [1/3] 检查 Python 与依赖
python -V
python -c "import fastapi, sqlalchemy, jwt, bcrypt, jinja2, uvicorn" >nul 2>&1
if errorlevel 1 (
    echo     缺少依赖，开始安装（需要联网）...
    python -m pip install -r requirements-dev.txt
    if errorlevel 1 (
        echo.
        echo [失败] 依赖安装失败。手动执行：python -m pip install -r requirements-dev.txt
        pause
        exit /b 1
    )
) else (
    echo     依赖已就绪
)
echo.

echo [2/3] 初始化数据库与种子数据（幂等，重复执行不会重复写入）
python -m app.db.seed_cli
if errorlevel 1 (
    echo [失败] 初始化失败
    pause
    exit /b 1
)
echo.

echo [3/3] 启动服务...
echo.
echo   首页    http://127.0.0.1:8000
echo   运营台  http://127.0.0.1:8000/ops        ^<- 截图 5-7、5-8 在这两个页面
echo   监管台  http://127.0.0.1:8000/regulator
echo   农户端  http://127.0.0.1:8000/farmer
echo   接口文档 http://127.0.0.1:8000/docs
echo.
echo   演示账号（口令均为 Passw0rd!）
echo     operator01   平台运营
echo     regulator01  监管人员
echo     farmer01     农户
echo     consumer01   消费者
echo.
echo   关闭服务：按 Ctrl+C，或直接关掉本窗口
echo ================================================
echo.

start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8000/login"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

echo.
echo 服务已停止。
pause
