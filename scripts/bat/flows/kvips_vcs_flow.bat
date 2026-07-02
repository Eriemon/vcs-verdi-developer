@echo off
setlocal EnableExtensions

REM 解析当前批处理入口回溯得到的 skill 根目录，保证从任意工作目录调用都能定位 Python 实现
set "PATH_SKILL_ROOT=%~dp0..\..\.."

REM 调用 kvips_vcs_flow Python 主程序统一执行KVIPS VCS 流程，让参数解析、失败判定与退出码策略只维护在这一份实现里
python "%PATH_SKILL_ROOT%\scripts\python\flows\kvips_vcs_flow.py" %*

REM 把 Python 退出码透传给调用方，便于自动化链路准确判定失败原因
exit /b %ERRORLEVEL%
