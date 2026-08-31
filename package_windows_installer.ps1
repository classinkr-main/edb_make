param(
    [string]$AppName = "ClassInEDBMVP",
    [string]$AppId = "",
    [string]$AppDisplayName = "ClassIn EDB",
    [string]$AppPublisher = "ClassIn EDB",
    [string]$OutputDir = "dist",
    [string]$Version = "",
    [string]$UpdateFeedUrl = "",
    [string]$DownloadUrl = "",
    [string]$ReleaseNotesUrl = "",
    [switch]$Clean,
    [switch]$SkipAppBuild,
    [switch]$InstallPyInstaller,
    [switch]$BundleUpscayl,
    [switch]$Sign,
    [string]$SignTool = "",
    [string]$SignCertificatePath = "",
    [string]$SignCertificatePassword = "",
    [string]$SignCertificateSubject = "",
    [string]$SignCertificateThumbprint = "",
    [switch]$SignCertificateAutoSelect,
    [string]$SignTimestampUrl = "http://timestamp.digicert.com",
    [string]$PythonExe = "",
    [string]$InnoSetupCompiler = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot
. (Join-Path $ProjectRoot "scripts\Sign-WindowsArtifact.ps1")
$ResolvedOutputDir = if ([System.IO.Path]::IsPathRooted($OutputDir)) { $OutputDir } else { Join-Path $ProjectRoot $OutputDir }
$ResolvedOutputDir = [System.IO.Path]::GetFullPath($ResolvedOutputDir)

function Assert-EDBSafeOutputDirectory {
    param(
        [Parameter(Mandatory = $true)] [string]$Path,
        [Parameter(Mandatory = $true)] [string]$ProjectPath,
        [bool]$WillClean = $false
    )

    $TrimChars = [char[]]@([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    $ResolvedPath = [System.IO.Path]::GetFullPath($Path).TrimEnd($TrimChars)
    $ResolvedProject = [System.IO.Path]::GetFullPath($ProjectPath).TrimEnd($TrimChars)
    $ProtectedPaths = @(
        [System.IO.Path]::GetPathRoot($ResolvedPath),
        [Environment]::GetFolderPath("UserProfile"),
        $ResolvedProject
    )
    foreach ($ProtectedPath in $ProtectedPaths) {
        if ($ProtectedPath -and [string]::Equals($ResolvedPath, $ProtectedPath.TrimEnd($TrimChars), [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing unsafe packaging output directory: $ResolvedPath"
        }
    }
    $OutputPrefix = $ResolvedPath + [System.IO.Path]::DirectorySeparatorChar
    if ($ResolvedProject.StartsWith($OutputPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing packaging output directory that contains the project: $ResolvedPath"
    }
    if (($ResolvedPath -split '[\\/]') -contains '.git') {
        throw "Refusing packaging output inside .git: $ResolvedPath"
    }
    $ProjectPrefix = $ResolvedProject + [System.IO.Path]::DirectorySeparatorChar
    if ($ResolvedPath.StartsWith($ProjectPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        $RelativePath = $ResolvedPath.Substring($ProjectPrefix.Length)
        $TopLevel = ($RelativePath -split '[\\/]')[0]
        if ($TopLevel -ne 'dist') {
            throw "Refusing project-internal packaging output outside the exact dist allowlist: $ResolvedPath"
        }
    } elseif (Test-Path -LiteralPath $ResolvedPath -PathType Container) {
        $ExistingEntry = Get-ChildItem -Force -LiteralPath $ResolvedPath | Select-Object -First 1
        $Sentinel = Join-Path $ResolvedPath ".edb-packaging-output"
        if ($ExistingEntry -and -not (Test-Path -LiteralPath $Sentinel -PathType Leaf)) {
            throw "Refusing to clean non-empty unmarked external packaging output: $ResolvedPath"
        }
    }
}

Assert-EDBSafeOutputDirectory -Path $ResolvedOutputDir -ProjectPath $ProjectRoot -WillClean ([bool]$Clean)
if ([string]::IsNullOrWhiteSpace($AppDisplayName)) {
    $AppDisplayName = $AppName
}
if ([string]::IsNullOrWhiteSpace($AppPublisher)) {
    $AppPublisher = $AppDisplayName
}

if (-not $PythonExe) {
    $VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $VenvPython -PathType Leaf) {
        $PythonExe = $VenvPython
    } else {
        $PythonExe = "python"
    }
}

function Find-InnoSetupCompiler {
    param([string]$Requested)

    if ($Requested) {
        $ResolvedRequested = if ([System.IO.Path]::IsPathRooted($Requested)) {
            [System.IO.Path]::GetFullPath($Requested)
        } else {
            [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $Requested))
        }
        if (-not (Test-Path -LiteralPath $ResolvedRequested -PathType Leaf)) {
            throw "Requested Inno Setup compiler was not found: $ResolvedRequested"
        }
        return (Resolve-Path -LiteralPath $ResolvedRequested).Path
    }

    $Candidates = @()
    if ($env:LOCALAPPDATA) {
        $Candidates += (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe")
    }
    $Candidates += @(
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    )

    foreach ($Candidate in $Candidates) {
        if ($Candidate -and (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $Candidate).Path
        }
    }

    $Command = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
    if ($Command) {
        return $Command.Source
    }

    return ""
}

function Get-JsonStringProperty {
    param(
        [Parameter(Mandatory = $true)] [object]$Object,
        [Parameter(Mandatory = $true)] [string[]]$Names
    )

    foreach ($Name in $Names) {
        if ($Object.PSObject.Properties[$Name]) {
            $Value = ([string]$Object.PSObject.Properties[$Name].Value).Trim()
            if ($Value) {
                return $Value
            }
        }
    }
    return ""
}

function Assert-EDBNonEmptyFile {
    param(
        [Parameter(Mandatory = $true)] [string]$Path,
        [Parameter(Mandatory = $true)] [string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label was not created: $Path"
    }
    if ((Get-Item -LiteralPath $Path).Length -le 0) {
        throw "$Label is empty: $Path"
    }
}

function Assert-EDBNativeCommandSucceeded {
    param([Parameter(Mandatory = $true)] [string]$Label)

    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE."
    }
}

function Write-EDBArtifactSummary {
    param(
        [Parameter(Mandatory = $true)] [string]$Path,
        [Parameter(Mandatory = $true)] [string]$Label
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }
    $Item = Get-Item -LiteralPath $Path
    $Hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
    Write-Host "${Label}: $Path"
    Write-Host "$Label size: $($Item.Length) bytes"
    Write-Host "$Label sha256: $Hash"
}

function Read-PackagedUpdateConfig {
    param([Parameter(Mandatory = $true)] [string]$PackageRoot)

    $Candidates = @(
        (Join-Path $PackageRoot "app_update_config.json"),
        (Join-Path $PackageRoot "_internal\app_update_config.json")
    )
    foreach ($Candidate in $Candidates) {
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) {
            try {
                return Get-Content -Raw -LiteralPath $Candidate | ConvertFrom-Json
            } catch {
                throw "Could not read packaged update metadata: $Candidate. $($_.Exception.Message)"
            }
        }
    }
    throw "Packaged update metadata was not found under: $PackageRoot"
}

if (-not $SkipAppBuild) {
    $AppBuildArgs = @{
        AppName = $AppName
        OutputDir = $OutputDir
    }
    if ($AppId) {
        $AppBuildArgs.AppId = $AppId
    }
    if ($Version) {
        $AppBuildArgs.Version = $Version
    }
    if ($UpdateFeedUrl) {
        $AppBuildArgs.UpdateFeedUrl = $UpdateFeedUrl
    }
    if ($DownloadUrl) {
        $AppBuildArgs.DownloadUrl = $DownloadUrl
    }
    if ($ReleaseNotesUrl) {
        $AppBuildArgs.ReleaseNotesUrl = $ReleaseNotesUrl
    }
    if ($Clean) {
        $AppBuildArgs.Clean = $true
    }
    if ($InstallPyInstaller) {
        $AppBuildArgs.InstallPyInstaller = $true
    }
    if ($BundleUpscayl) {
        $AppBuildArgs.BundleUpscayl = $true
    }
    $AppBuildArgs.RequirePyInstaller = $true
    if ($PythonExe) {
        $AppBuildArgs.PythonExe = $PythonExe
    }
    if ($Sign) {
        $AppBuildArgs.Sign = $true
        $AppBuildArgs.SignTool = $SignTool
        $AppBuildArgs.SignCertificatePath = $SignCertificatePath
        $AppBuildArgs.SignCertificatePassword = $SignCertificatePassword
        $AppBuildArgs.SignCertificateSubject = $SignCertificateSubject
        $AppBuildArgs.SignCertificateThumbprint = $SignCertificateThumbprint
        $AppBuildArgs.SignCertificateAutoSelect = $SignCertificateAutoSelect
        $AppBuildArgs.SignTimestampUrl = $SignTimestampUrl
    }
    & (Join-Path $ProjectRoot "package_mvp.ps1") @AppBuildArgs
}

$PackageRoot = Join-Path $ResolvedOutputDir $AppName
$PackageExe = Join-Path $PackageRoot "$AppName.exe"
try {
    Assert-EDBNonEmptyFile -Path $PackageExe -Label "PyInstaller app executable"
} catch {
    throw "PyInstaller app output was not found or is empty: $PackageExe. Build the app first or remove -SkipAppBuild."
}

$PackagedUpdateConfig = Read-PackagedUpdateConfig $PackageRoot
$PackagedAppId = Get-JsonStringProperty $PackagedUpdateConfig @("appId", "app_id")
$PackagedVersion = Get-JsonStringProperty $PackagedUpdateConfig "version"
$PackagedUpdateFeedUrl = Get-JsonStringProperty $PackagedUpdateConfig @("updateFeedUrl", "update_feed_url")
$PackagedDownloadUrl = Get-JsonStringProperty $PackagedUpdateConfig @("downloadUrl", "download_url")
$PackagedReleaseNotesUrl = Get-JsonStringProperty $PackagedUpdateConfig @("releaseNotesUrl", "release_notes_url")
$EffectiveInstallerAppId = if ($AppId) { $AppId } else { $PackagedAppId }
$EffectiveInstallerVersion = if ($Version) { $Version } else { $PackagedVersion }
$EffectiveInstallerUpdateFeedUrl = if ($UpdateFeedUrl) { $UpdateFeedUrl } else { $PackagedUpdateFeedUrl }
$EffectiveInstallerDownloadUrl = if ($DownloadUrl) { $DownloadUrl } else { $PackagedDownloadUrl }
$EffectiveInstallerReleaseNotesUrl = if ($ReleaseNotesUrl) { $ReleaseNotesUrl } else { $PackagedReleaseNotesUrl }
if (-not $EffectiveInstallerVersion) {
    throw "Packaged update metadata does not include a version, and -Version was not provided."
}

$VerifierArgs = @(
    (Join-Path $ProjectRoot "scripts\verify_packaged_app.py"),
    $PackageRoot,
    "--expected-app-id",
    $EffectiveInstallerAppId,
    "--expected-app-name",
    $AppName,
    "--expected-version",
    $EffectiveInstallerVersion
)
if ($EffectiveInstallerUpdateFeedUrl) {
    $VerifierArgs += @("--expected-update-feed-url", $EffectiveInstallerUpdateFeedUrl)
}
if ($EffectiveInstallerDownloadUrl) {
    $VerifierArgs += @("--expected-download-url", $EffectiveInstallerDownloadUrl)
}
if ($EffectiveInstallerReleaseNotesUrl) {
    $VerifierArgs += @("--expected-release-notes-url", $EffectiveInstallerReleaseNotesUrl)
}
if ($env:EDB_RELEASE_GIT_COMMIT) {
    $VerifierArgs += @("--expected-git-commit", $env:EDB_RELEASE_GIT_COMMIT)
}
& $PythonExe @VerifierArgs
Assert-EDBNativeCommandSucceeded "Packaged app verification"

if ($Sign -and $SkipAppBuild) {
    Invoke-EDBWindowsPackageSigning `
        -PackagePath $PackageRoot `
        -SignTool $SignTool `
        -CertificatePath $SignCertificatePath `
        -CertificatePassword $SignCertificatePassword `
        -CertificateSubject $SignCertificateSubject `
        -CertificateThumbprint $SignCertificateThumbprint `
        -CertificateAutoSelect:$SignCertificateAutoSelect `
        -TimestampUrl $SignTimestampUrl `
        -Description $AppName
}

$InstallerPath = Join-Path $ResolvedOutputDir "$AppName-Setup.exe"
if (Test-Path -LiteralPath $InstallerPath -PathType Leaf) {
    Remove-Item -Force -LiteralPath $InstallerPath
}

$Iscc = Find-InnoSetupCompiler $InnoSetupCompiler
if (-not $Iscc) {
    throw "Inno Setup 6 compiler(ISCC.exe)를 찾지 못했습니다. https://jrsoftware.org/isinfo.php 에서 설치한 뒤 다시 실행하거나 -InnoSetupCompiler 경로를 지정하세요."
}

$InstallerScript = Join-Path $ProjectRoot "installer\windows\ClassInEDBMVP.iss"
$IsccArgs = @(
    "/DAppName=$AppName",
    "/DAppDisplayName=$AppDisplayName",
    "/DAppPublisher=$AppPublisher",
    "/DSourceDir=$PackageRoot",
    "/DOutputDir=$ResolvedOutputDir",
    "/DAppVersion=$EffectiveInstallerVersion"
)
& $Iscc @IsccArgs $InstallerScript
Assert-EDBNativeCommandSucceeded "Inno Setup compilation"
Assert-EDBNonEmptyFile -Path $InstallerPath -Label "Windows installer"

if ($Sign) {
    Invoke-EDBWindowsSignature `
        -Path $InstallerPath `
        -SignTool $SignTool `
        -CertificatePath $SignCertificatePath `
        -CertificatePassword $SignCertificatePassword `
        -CertificateSubject $SignCertificateSubject `
        -CertificateThumbprint $SignCertificateThumbprint `
        -CertificateAutoSelect:$SignCertificateAutoSelect `
        -TimestampUrl $SignTimestampUrl `
        -Description "$AppName Setup"
}
Write-EDBArtifactSummary -Path $InstallerPath -Label "Windows installer"
Write-Host "Installer complete."
Write-Host "Installer: $InstallerPath"
