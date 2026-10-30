[CmdletBinding()]
param(
    [switch]$Posture,
    [switch]$Movement,
    [switch]$DryRun,
    [double]$ActionDelay = 0.25,
    [double]$Hold = 0.0
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$config = Join-Path $root 'config.yaml'
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { Write-Error 'python not found'; exit 2 }
if (-not (Test-Path $config)) { Write-Error "config not found: $config"; exit 2 }
Write-Host 'Before continuing: enter VRChat in a private, safe world and load the Aether avatar.'
Write-Host 'This script does not start VRChat or SteamVR and does not guess installation paths.'
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { $python = (Get-Command python).Source }
$args = @('-m', 'aether.runtime.vrchat_test', '--config', $config, '--action-delay', $ActionDelay, '--hold', $Hold)
if ($Posture) { $args += '--posture' }
if ($Movement) { $args += '--movement' }
if ($DryRun) { $args += '--dry-run' }
Push-Location $root
try { & $python @args; exit $LASTEXITCODE } finally { Pop-Location }
