[CmdletBinding()]
param(
    [switch]$Install,
    [switch]$Full,
    [switch]$BuildProvider,
    [switch]$SkipSmoke
)

$ErrorActionPreference = "Stop"
$Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python = Join-Path $Repo ".venv\Scripts\python.exe"

if (-not (Test-Path $Python) -and $Install) {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $pyLauncher) {
        & $pyLauncher.Source -3 -m venv (Join-Path $Repo ".venv")
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
}

if (-not (Test-Path $Python)) {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $pythonCommand) {
        throw "找不到 Python。请先安装 Python 3.10+，或运行: py -3 -m venv .venv"
    }
    $Python = $pythonCommand.Source
}

Push-Location $Repo
try {
    if ($Install) {
        & $Python -m pip install -r requirements.txt
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & $Python -m pip install -e "$Repo\motion-provider[server,test]"
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }

    $args = @("scripts/dev_verify.py")
    if ($Full) { $args += "--full" }
    if ($BuildProvider) { $args += "--build-provider" }
    if (-not $SkipSmoke) { $args += "--smoke" }

    & $Python @args
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
