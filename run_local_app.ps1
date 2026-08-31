param(
    [int]$Port = 8765,
    [switch]$NoBrowser,
    [switch]$InstallDeps,
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot
$VenvDir = Join-Path $ProjectRoot ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

# Keep Korean paths and diagnostics intact when output is redirected by CI,
# editors, or support tools. Respect an explicit caller override.
if (-not $env:PYTHONUTF8) {
    $env:PYTHONUTF8 = "1"
}

function Resolve-EDBPythonCommand {
    if ($PythonExe) {
        $ExplicitCommand = Get-Command $PythonExe -ErrorAction SilentlyContinue
        if (-not $ExplicitCommand) {
            throw "Python executable was not found: $PythonExe"
        }
        return @{
            Command = $ExplicitCommand.Source
            PrefixArguments = @()
        }
    }

    if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
        return @{
            Command = $VenvPython
            PrefixArguments = @()
        }
    }

    $PyLauncher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($PyLauncher) {
        return @{
            Command = $PyLauncher.Source
            PrefixArguments = @("-3")
        }
    }

    $SystemPython = Get-Command "python.exe" -ErrorAction SilentlyContinue
    if ($SystemPython) {
        return @{
            Command = $SystemPython.Source
            PrefixArguments = @()
        }
    }

    throw "Python 3 was not found. Install Python 3.11 or newer, or pass -PythonExe with its full path."
}

function Invoke-EDBPythonChecked {
    param(
        [Parameter(Mandatory = $true)] [string[]]$Arguments,
        [Parameter(Mandatory = $true)] [string]$Label
    )

    & $script:PythonCommand @script:PythonPrefixArguments @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE."
    }
}

$ResolvedPython = Resolve-EDBPythonCommand
$PythonCommand = $ResolvedPython.Command
$PythonPrefixArguments = @($ResolvedPython.PrefixArguments)

if ($InstallDeps -and -not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    Write-Host "Creating isolated Python environment at $VenvDir"
    Invoke-EDBPythonChecked -Arguments @("-m", "venv", $VenvDir) -Label "Virtual environment creation"
    if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        throw "Virtual environment was created without the expected Python executable: $VenvPython"
    }
    $PythonCommand = $VenvPython
    $PythonPrefixArguments = @()
}

Invoke-EDBPythonChecked `
    -Arguments @("-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)") `
    -Label "Python 3.11+ version check"

if ($InstallDeps) {
    Invoke-EDBPythonChecked `
        -Arguments @("-m", "pip", "install", "--disable-pip-version-check", "-r", (Join-Path $ProjectRoot "requirements-local.txt")) `
        -Label "Dependency installation"
}

$AppArguments = @("app_server.py", "--host", "127.0.0.1", "--port", $Port)
if (-not $NoBrowser) {
    $AppArguments += "--open-browser"
}

Write-Host "Launching local MVP app on http://127.0.0.1:$Port"
& $PythonCommand @PythonPrefixArguments @AppArguments
$AppExitCode = $LASTEXITCODE
if ($AppExitCode -ne 0) {
    Write-Error "Local app exited with code $AppExitCode." -ErrorAction Continue
}
exit $AppExitCode
