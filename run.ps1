$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    $taskRuntime = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Get-Command python -ErrorAction SilentlyContinue) { $taskRuntime = 'python' }
    elseif (-not (Test-Path -LiteralPath $taskRuntime)) { throw 'Установите Python 3.11+ и выполните инструкции из README.md.' }
    & $taskRuntime -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось создать виртуальное окружение.' }
    & $taskPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Не удалось установить зависимости.' }
}
& $taskPython -m streamlit run app.py
