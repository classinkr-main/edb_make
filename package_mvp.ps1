param(
    [string]$AppName = "ClassInEDBMVP",
    [string]$AppId = "",
    [string]$OutputDir = "dist",
    [string]$Version = "",
    [string]$UpdateFeedUrl = "",
    [string]$DownloadUrl = "",
    [string]$ReleaseNotesUrl = "",
    [switch]$Clean,
    [switch]$Zip,
    [switch]$OneFile,
    [switch]$Console,
    [switch]$InstallPyInstaller,
    [switch]$SkipFrontendBuild,
    [switch]$RequirePyInstaller,
    [switch]$BundleUpscayl,
    [switch]$Sign,
    [string]$SignTool = "",
    [string]$SignCertificatePath = "",
    [string]$SignCertificatePassword = "",
    [string]$SignCertificateSubject = "",
    [string]$SignCertificateThumbprint = "",
    [switch]$SignCertificateAutoSelect,
    [string]$SignTimestampUrl = "http://timestamp.digicert.com",
    [string]$IconPath = "assets\app_icon.ico",
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot
. (Join-Path $ProjectRoot "scripts\Sign-WindowsArtifact.ps1")

if (-not $PythonExe) {
    $VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    if (Test-Path $VenvPython) {
        $PythonExe = $VenvPython
    } else {
        $PythonExe = "python"
    }
}

function Assert-EDBNativeCommandSucceeded {
    param([Parameter(Mandatory = $true)] [string]$Label)

    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE."
    }
}

if ($InstallPyInstaller) {
    & $PythonExe -m pip install --disable-pip-version-check --require-hashes -r (Join-Path $ProjectRoot "requirements-release-bootstrap.lock")
    Assert-EDBNativeCommandSucceeded "Locked release bootstrap installation"
    & $PythonExe -m pip install --disable-pip-version-check --require-hashes --no-build-isolation -r (Join-Path $ProjectRoot "requirements-release.lock")
    Assert-EDBNativeCommandSucceeded "Locked release dependency installation"
}

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

function Remove-EDBPathIfExists {
    param([Parameter(Mandatory = $true)] [string]$Path)

    if (Test-Path $Path) {
        Remove-Item -Recurse -Force $Path
    }
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

function Assert-EDBZipContainsEntry {
    param(
        [Parameter(Mandatory = $true)] [string]$ZipPath,
        [Parameter(Mandatory = $true)] [string]$EntryName
    )

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $Archive = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $Expected = $EntryName.Replace("\", "/")
        $Found = $false
        foreach ($Entry in $Archive.Entries) {
            if ($Entry.FullName.Replace("\", "/") -eq $Expected) {
                $Found = $true
                break
            }
        }
        if (-not $Found) {
            throw "Zip archive is missing expected entry: $EntryName"
        }
    } finally {
        $Archive.Dispose()
    }
}

function Get-EDBJsonStringProperty {
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

$LicenseVerifierArgs = @(
    (Join-Path $ProjectRoot "scripts\verify_release_licenses.py"),
    "--root",
    $ProjectRoot,
    "--require-release-policy",
    "--require-locked-environment",
    "--reject-unlocked-environment"
)
if ($BundleUpscayl) {
    $LicenseVerifierArgs += "--bundle-upscayl"
}
& $PythonExe @LicenseVerifierArgs
Assert-EDBNativeCommandSucceeded "Release license verification"

if ($Clean -and (Test-Path $ResolvedOutputDir)) {
    Remove-Item -Recurse -Force $ResolvedOutputDir
}
New-Item -ItemType Directory -Force -Path $ResolvedOutputDir | Out-Null
Set-Content -LiteralPath (Join-Path $ResolvedOutputDir ".edb-packaging-output") -Value "generated; safe to replace" -Encoding UTF8
$PackageDirPath = Join-Path $ResolvedOutputDir $AppName
$PackageExePath = Join-Path $ResolvedOutputDir "$AppName.exe"
$SourcePackagePath = Join-Path $ResolvedOutputDir "source-package"
$ZipPath = Join-Path $ResolvedOutputDir "$AppName-Portable.zip"
$PortableReadmeName = "EXTRACT_BEFORE_RUNNING.txt"
$PortableReadmePath = Join-Path $ResolvedOutputDir $PortableReadmeName
$WorkPath = Join-Path $ResolvedOutputDir "_pyinstaller_build"
foreach ($StalePath in @($WorkPath, $PackageDirPath, $PackageExePath, $SourcePackagePath, $ZipPath, $PortableReadmePath)) {
    Remove-EDBPathIfExists $StalePath
}
$SpecDir = Join-Path $ResolvedOutputDir "_pyinstaller_spec"
New-Item -ItemType Directory -Force -Path $SpecDir | Out-Null
$BuildUpdateConfig = Join-Path $SpecDir "app_update_config.json"
$ReleaseMetadataDir = Join-Path $ResolvedOutputDir "release-metadata"
$ProjectUpdateConfig = Join-Path $ProjectRoot "app_update_config.json"
$UpdateConfigScript = Join-Path $ProjectRoot "scripts\build_app_update_config.py"
$UpdateConfigEnv = @{
    EDB_PACKAGE_APP_ID = $AppId
    EDB_PACKAGE_APP_NAME = $AppName
    EDB_PACKAGE_APP_VERSION = $Version
    EDB_PACKAGE_UPDATE_FEED_URL = $UpdateFeedUrl
    EDB_PACKAGE_DOWNLOAD_URL = $DownloadUrl
    EDB_PACKAGE_RELEASE_NOTES_URL = $ReleaseNotesUrl
}
$PreviousUpdateConfigEnv = @{}
foreach ($Name in $UpdateConfigEnv.Keys) {
    $PreviousUpdateConfigEnv[$Name] = [Environment]::GetEnvironmentVariable($Name, "Process")
    [Environment]::SetEnvironmentVariable($Name, $UpdateConfigEnv[$Name], "Process")
}
try {
    & $PythonExe $UpdateConfigScript $ProjectUpdateConfig $BuildUpdateConfig
    Assert-EDBNativeCommandSucceeded "app_update_config generation"
} finally {
    foreach ($Name in $PreviousUpdateConfigEnv.Keys) {
        [Environment]::SetEnvironmentVariable($Name, $PreviousUpdateConfigEnv[$Name], "Process")
    }
}
$UpdateConfig = Get-Content -Raw -Path $BuildUpdateConfig | ConvertFrom-Json
$EffectiveAppId = Get-EDBJsonStringProperty -Object $UpdateConfig -Names @("appId")
$EffectiveAppName = Get-EDBJsonStringProperty -Object $UpdateConfig -Names @("appName")
$EffectiveAppVersion = Get-EDBJsonStringProperty -Object $UpdateConfig -Names @("version")
$EffectiveUpdateFeedUrl = Get-EDBJsonStringProperty -Object $UpdateConfig -Names @("updateFeedUrl")
$EffectiveDownloadUrl = Get-EDBJsonStringProperty -Object $UpdateConfig -Names @("downloadUrl")
$EffectiveReleaseNotesUrl = Get-EDBJsonStringProperty -Object $UpdateConfig -Names @("releaseNotesUrl")

$ReleaseMetadataArgs = @(
    (Join-Path $ProjectRoot "scripts\build_release_metadata.py"),
    "build",
    "--root", $ProjectRoot,
    "--output-dir", $ReleaseMetadataDir,
    "--version", $EffectiveAppVersion,
    "--strict-environment"
)
if ($env:EDB_RELEASE_GIT_COMMIT) {
    $ReleaseMetadataArgs += @("--git-commit", $env:EDB_RELEASE_GIT_COMMIT)
}
& $PythonExe @ReleaseMetadataArgs
Assert-EDBNativeCommandSucceeded "Release metadata generation"

function Invoke-EDBPackagedAppVerifier {
    param(
        [Parameter(Mandatory = $true)] [string]$PackageRoot,
        [switch]$SmokeSourceImport
    )

    $VerifierArgs = @(
        (Join-Path $ProjectRoot "scripts\verify_packaged_app.py"),
        $PackageRoot,
        "--expected-app-id",
        $EffectiveAppId,
        "--expected-app-name",
        $EffectiveAppName,
        "--expected-version",
        $EffectiveAppVersion,
        "--source-root",
        $ProjectRoot
    )
    if ($EffectiveUpdateFeedUrl) {
        $VerifierArgs += @("--expected-update-feed-url", $EffectiveUpdateFeedUrl)
    }
    if ($EffectiveDownloadUrl) {
        $VerifierArgs += @("--expected-download-url", $EffectiveDownloadUrl)
    }
    if ($EffectiveReleaseNotesUrl) {
        $VerifierArgs += @("--expected-release-notes-url", $EffectiveReleaseNotesUrl)
    }
    if ($env:EDB_RELEASE_GIT_COMMIT) {
        $VerifierArgs += @("--expected-git-commit", $env:EDB_RELEASE_GIT_COMMIT)
    }
    if ($SmokeSourceImport) {
        $VerifierArgs += "--smoke-source-import"
    }
    & $PythonExe @VerifierArgs
    Assert-EDBNativeCommandSucceeded "Packaged app verification"
}

function Invoke-EDBOneFileHealthSmoke {
    param([Parameter(Mandatory = $true)] [string]$ExecutablePath)

    Assert-EDBNonEmptyFile -Path $ExecutablePath -Label "PyInstaller one-file executable"
    & $PythonExe `
        (Join-Path $ProjectRoot "scripts\smoke_packaged_app.py") `
        $ExecutablePath `
        --startup-timeout 60 `
        --expected-app-id $EffectiveAppId
    Assert-EDBNativeCommandSucceeded "PyInstaller one-file runtime smoke"
}

$NodeCommand = Get-Command node -ErrorAction SilentlyContinue
if (-not $NodeCommand) {
    throw "Node.js is required to build or deterministically verify ui_prototype\app.bundle.js."
}
if (-not $SkipFrontendBuild) {
    & $NodeCommand.Source (Join-Path $ProjectRoot "scripts\build_frontend_bundle.mjs")
    Assert-EDBNativeCommandSucceeded "Frontend bundle build"
}

& $PythonExe (Join-Path $ProjectRoot "scripts\verify_frontend_package.py") --root $ProjectRoot
Assert-EDBNativeCommandSucceeded "Frontend package verification"

$HasPyInstaller = $true
& $PythonExe -m PyInstaller --version 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    $HasPyInstaller = $false
}

$BuiltWithPyInstaller = $false
if ($HasPyInstaller) {
    $BuiltWithPyInstaller = $true
    $ModeArg = if ($OneFile) { "--onefile" } else { "--onedir" }
    $WindowArg = if ($Console) { "--console" } else { "--windowed" }
    $PackageRoot = if ($OneFile) { $PackageExePath } else { $PackageDirPath }
    $ResolvedIconPath = if ([System.IO.Path]::IsPathRooted($IconPath)) { $IconPath } else { Join-Path $ProjectRoot $IconPath }
    $IconArgs = @()
    if (Test-Path $ResolvedIconPath) {
        $IconArgs = @("--icon", $ResolvedIconPath)
    } else {
        Write-Warning "Icon file not found: $ResolvedIconPath"
    }

    $DataItems = @(
        @("ui_prototype\index.html", "ui_prototype"),
        @("ui_prototype\board.html", "ui_prototype"),
        @("ui_prototype\board.css", "ui_prototype"),
        @("ui_prototype\favicon.png", "ui_prototype"),
        @("ui_prototype\reorder.js", "ui_prototype"),
        @("ui_prototype\review_filters.js", "ui_prototype"),
        @("ui_prototype\publish_summary.js", "ui_prototype"),
        @("ui_prototype\publish_guard.js", "ui_prototype"),
        @("ui_prototype\app.bundle.js", "ui_prototype"),
        @("ui_prototype\vendor\react.production.min.js", "ui_prototype\vendor"),
        @("ui_prototype\vendor\react-dom.production.min.js", "ui_prototype\vendor"),
        @("ui_prototype\vendor\fonts\fonts.css", "ui_prototype\vendor\fonts"),
        @("ui_prototype\vendor\fonts\PretendardVariable.woff2", "ui_prototype\vendor\fonts"),
        @("ui_prototype\vendor\fonts\JetBrainsMonoVariable.woff2", "ui_prototype\vendor\fonts"),
        @("ui_prototype\vendor\fonts\CaveatVariable.woff2", "ui_prototype\vendor\fonts"),
        @("ui_prototype\vendor\fonts\OFL-Pretendard.txt", "ui_prototype\vendor\fonts"),
        @("ui_prototype\vendor\fonts\OFL-JetBrainsMono.txt", "ui_prototype\vendor\fonts"),
        @("ui_prototype\vendor\fonts\OFL-Caveat.txt", "ui_prototype\vendor\fonts"),
        @("scripts\render_hwp_with_rhwp_core.mjs", "scripts"),
        @("assets\app_icon.png", "assets")
    )
    if ($BundleUpscayl) {
        $DataItems += ,@("resources\upscayl\LICENSE", "resources\upscayl")
        $DataItems += ,@("resources\upscayl\THIRD_PARTY_NOTICES.md", "resources\upscayl")
        $DataItems += ,@("resources\upscayl\CORRESPONDING_SOURCE.txt", "resources\upscayl")
        $DataItems += ,@("resources\upscayl\models", "resources\upscayl\models")
        $DataItems += ,@("resources\upscayl\win", "resources\upscayl\win")
    }
    $DataArgs = @()
    $DataArgs += @("--add-data", ($BuildUpdateConfig + ";."))
    $DataArgs += @("--add-data", ($ReleaseMetadataDir + ";release_metadata"))
    foreach ($Item in $DataItems) {
        $SourcePath = Join-Path $ProjectRoot $Item[0]
        if (Test-Path $SourcePath) {
            $DataArgs += @("--add-data", ($SourcePath + ";" + $Item[1]))
        }
    }
    $HiddenImportArgs = @(
        "--hidden-import", "preprocess",
        "--hidden-import", "build_mvp_export",
        "--hidden-import", "build_problem_board_edb",
        "--hidden-import", "build_structured_page_json",
        "--hidden-import", "edb_builder",
        "--hidden-import", "page_repair",
        "--hidden-import", "image_reconstruction_backend",
        "--hidden-import", "upscayl_backend"
    )

    $PyInstallerArgs = @(
        "--noconfirm",
        "--clean",
        $ModeArg,
        $WindowArg,
        "--distpath", $ResolvedOutputDir,
        "--specpath", $SpecDir,
        "--workpath", $WorkPath,
        "--name", $AppName
    ) + $DataArgs + $HiddenImportArgs + $IconArgs + @("app_server.py")

    & $PythonExe -m PyInstaller @PyInstallerArgs
    Assert-EDBNativeCommandSucceeded "PyInstaller packaging"

    if ($OneFile) {
        Assert-EDBNonEmptyFile -Path $PackageRoot -Label "PyInstaller one-file executable"
        Invoke-EDBOneFileHealthSmoke -ExecutablePath $PackageRoot
    } else {
        Invoke-EDBPackagedAppVerifier -PackageRoot $PackageRoot
    }
    Remove-EDBPathIfExists $WorkPath
    Write-Host "PyInstaller packaging complete."
} else {
    if ($RequirePyInstaller) {
        throw "PyInstaller is required for this packaging mode. Run with -InstallPyInstaller or install PyInstaller first."
    }
    $PackageRoot = $SourcePackagePath
    New-Item -ItemType Directory -Force -Path $PackageRoot | Out-Null

    $ItemsToCopy = @(
        "app_server.py",
        "bug_reporting.py",
        "build_mvp_export.py",
        "build_problem_board_edb.py",
        "build_structured_page_json.py",
        "image_reconstruction_backend.py",
        "upscayl_backend.py",
        "page_repair.py",
        "pipeline_cache.py",
        "pipeline_router.py",
        "preprocess.py",
        "segment.py",
        "ocr_backend.py",
        "placement_engine.py",
        "layout_template_schema.py",
        "structured_schema.py",
        "user_settings.py",
        "assemble_page.py",
        "edb_builder.py",
        "inspect_edb.py",
        "requirements-local.txt",
        "requirements-release-bootstrap.lock",
        "requirements-release.lock",
        "release\dependency_inventory.json",
        "release\THIRD_PARTY_NOTICES.md",
        "run_local_app.ps1",
        "scripts\render_hwp_with_rhwp_core.mjs",
        "assets\app_icon.png",
        "PACKAGING_MVP.md",
        "ui_prototype\index.html",
        "ui_prototype\board.html",
        "ui_prototype\board.css",
        "ui_prototype\favicon.png",
        "ui_prototype\reorder.js",
        "ui_prototype\review_filters.js",
        "ui_prototype\publish_summary.js",
        "ui_prototype\publish_guard.js",
        "ui_prototype\app.bundle.js",
        "ui_prototype\vendor\react.production.min.js",
        "ui_prototype\vendor\react-dom.production.min.js",
        "ui_prototype\vendor\fonts\fonts.css",
        "ui_prototype\vendor\fonts\PretendardVariable.woff2",
        "ui_prototype\vendor\fonts\JetBrainsMonoVariable.woff2",
        "ui_prototype\vendor\fonts\CaveatVariable.woff2",
        "ui_prototype\vendor\fonts\OFL-Pretendard.txt",
        "ui_prototype\vendor\fonts\OFL-JetBrainsMono.txt",
        "ui_prototype\vendor\fonts\OFL-Caveat.txt"
    )
    if ($BundleUpscayl) {
        $ItemsToCopy += @(
            "resources\upscayl\LICENSE",
            "resources\upscayl\THIRD_PARTY_NOTICES.md",
            "resources\upscayl\CORRESPONDING_SOURCE.txt",
            "resources\upscayl\models",
            "resources\upscayl\win"
        )
    }

    foreach ($Item in $ItemsToCopy) {
        $SourcePath = Join-Path $ProjectRoot $Item
        if (Test-Path $SourcePath) {
            $DestinationPath = Join-Path $PackageRoot $Item
            $DestinationParent = Split-Path -Parent $DestinationPath
            if ($DestinationParent) {
                New-Item -ItemType Directory -Force -Path $DestinationParent | Out-Null
            }
            Copy-Item -Recurse -Force $SourcePath $DestinationPath
        }
    }
    Copy-Item -Force $BuildUpdateConfig (Join-Path $PackageRoot "app_update_config.json")
    Copy-Item -Recurse -Force $ReleaseMetadataDir (Join-Path $PackageRoot "release_metadata")

    Invoke-EDBPackagedAppVerifier -PackageRoot $PackageRoot -SmokeSourceImport
    Write-Warning "PyInstaller is not installed. Created source-package fallback instead."
}

if ($Zip) {
    if ($Sign) {
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

    if (Test-Path $PackageRoot) {
        $PortableReadme = @"
ClassIn EDB portable package

IMPORTANT: Extract this zip completely before running the app.
Do not double-click ClassInEDBMVP.exe while browsing inside the zip. Windows may copy only the launcher to a temporary folder, which prevents _internal\python*.dll from loading.

Recommended distribution: use ClassInEDBMVP-Setup.exe instead of this portable archive.
"@
        Set-Content -LiteralPath $PortableReadmePath -Value $PortableReadme -Encoding UTF8
        try {
            Compress-Archive -Path @($PackageRoot, $PortableReadmePath) -DestinationPath $ZipPath
        } finally {
            Remove-EDBPathIfExists $PortableReadmePath
        }
        Assert-EDBNonEmptyFile -Path $ZipPath -Label "Zip archive"
        Assert-EDBZipContainsEntry -ZipPath $ZipPath -EntryName $PortableReadmeName
        if ($BuiltWithPyInstaller -and $OneFile) {
            Assert-EDBZipContainsEntry -ZipPath $ZipPath -EntryName "$AppName.exe"
        } elseif ($BuiltWithPyInstaller) {
            Assert-EDBZipContainsEntry -ZipPath $ZipPath -EntryName "$AppName/$AppName.exe"
        } elseif (-not $BuiltWithPyInstaller) {
            Assert-EDBZipContainsEntry -ZipPath $ZipPath -EntryName "source-package/app_update_config.json"
        }
        Write-EDBArtifactSummary -Path $ZipPath -Label "Zip archive"
        Write-Host "Zip archive: $ZipPath"
    }
} elseif ($Sign) {
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

if ($BuiltWithPyInstaller -and $OneFile) {
    Write-EDBArtifactSummary -Path $PackageRoot -Label "PyInstaller one-file executable"
}

Write-Host "Packaging complete."
if (Test-Path -LiteralPath $PackageRoot -PathType Leaf) {
    Write-Host "Output file: $PackageRoot"
} else {
    Write-Host "Output folder: $PackageRoot"
}
