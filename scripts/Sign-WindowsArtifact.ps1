function Find-EDBSignTool {
    param([string]$Requested)

    if ($Requested) {
        $ResolvedRequested = if ([System.IO.Path]::IsPathRooted($Requested)) {
            [System.IO.Path]::GetFullPath($Requested)
        } else {
            [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $Requested))
        }
        if (-not (Test-Path -LiteralPath $ResolvedRequested -PathType Leaf)) {
            throw "Requested signtool.exe was not found: $ResolvedRequested"
        }
        return (Resolve-Path -LiteralPath $ResolvedRequested).Path
    }

    $Command = Get-Command "signtool.exe" -ErrorAction SilentlyContinue
    if ($Command) {
        return $Command.Source
    }

    $CandidateRoots = @()
    if (${env:ProgramFiles(x86)}) {
        $CandidateRoots += (Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\bin")
    }
    if ($env:ProgramFiles) {
        $CandidateRoots += (Join-Path $env:ProgramFiles "Windows Kits\10\bin")
    }

    foreach ($Root in $CandidateRoots) {
        if (-not (Test-Path -LiteralPath $Root -PathType Container)) {
            continue
        }
        $Candidate = Get-ChildItem -LiteralPath $Root -Recurse -Filter "signtool.exe" -File -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match "\\x64\\signtool\.exe$" } |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($Candidate) {
            return $Candidate.FullName
        }
    }

    return ""
}

function Invoke-EDBWindowsSignature {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [string]$SignTool = "",
        [string]$CertificatePath = "",
        [string]$CertificatePassword = "",
        [string]$CertificateSubject = "",
        [string]$CertificateThumbprint = "",
        [switch]$CertificateAutoSelect,
        [string]$TimestampUrl = "http://timestamp.digicert.com",
        [string]$Description = "ClassIn EDB"
    )

    $ResolvedPath = if ([System.IO.Path]::IsPathRooted($Path)) {
        [System.IO.Path]::GetFullPath($Path)
    } else {
        [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $Path))
    }
    if (-not (Test-Path -LiteralPath $ResolvedPath -PathType Leaf)) {
        throw "Signing target was not found: $ResolvedPath"
    }

    $SignToolPath = Find-EDBSignTool -Requested $SignTool
    if (-not $SignToolPath) {
        throw "signtool.exe was not found. Install the Windows SDK or pass -SignTool with the full signtool.exe path."
    }

    $SignArgs = @("sign", "/fd", "SHA256", "/td", "SHA256")
    if ($TimestampUrl) {
        $SignArgs += @("/tr", $TimestampUrl)
    }
    if ($Description) {
        $SignArgs += @("/d", $Description)
    }

    if ($CertificatePath) {
        $ResolvedCertificatePath = if ([System.IO.Path]::IsPathRooted($CertificatePath)) {
            [System.IO.Path]::GetFullPath($CertificatePath)
        } else {
            [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $CertificatePath))
        }
        if (-not (Test-Path -LiteralPath $ResolvedCertificatePath -PathType Leaf)) {
            throw "Code-signing certificate was not found: $ResolvedCertificatePath"
        }
        $ResolvedCertificatePath = (Resolve-Path -LiteralPath $ResolvedCertificatePath).Path
        $SignArgs += @("/f", $ResolvedCertificatePath)
        if ($CertificatePassword) {
            $SignArgs += @("/p", $CertificatePassword)
        }
    } elseif ($CertificateThumbprint) {
        $SignArgs += @("/sha1", $CertificateThumbprint)
    } elseif ($CertificateSubject) {
        $SignArgs += @("/n", $CertificateSubject)
        if ($CertificateAutoSelect) {
            $SignArgs += "/a"
        }
    } elseif ($CertificateAutoSelect) {
        $SignArgs += "/a"
    } else {
        throw "Windows signing requires -SignCertificatePath, -SignCertificateThumbprint, -SignCertificateSubject, or -SignCertificateAutoSelect."
    }

    $SignArgs += @("/v", $ResolvedPath)
    & $SignToolPath @SignArgs
    if ($LASTEXITCODE -ne 0) {
        throw "signtool sign failed for $ResolvedPath"
    }

    & $SignToolPath verify /pa /v $ResolvedPath
    if ($LASTEXITCODE -ne 0) {
        throw "signtool verify failed for $ResolvedPath"
    }
}

function Invoke-EDBWindowsPackageSigning {
    param(
        [Parameter(Mandatory = $true)][string]$PackagePath,
        [string]$SignTool = "",
        [string]$CertificatePath = "",
        [string]$CertificatePassword = "",
        [string]$CertificateSubject = "",
        [string]$CertificateThumbprint = "",
        [switch]$CertificateAutoSelect,
        [string]$TimestampUrl = "http://timestamp.digicert.com",
        [string]$Description = "ClassIn EDB"
    )

    $ResolvedPackagePath = if ([System.IO.Path]::IsPathRooted($PackagePath)) {
        [System.IO.Path]::GetFullPath($PackagePath)
    } else {
        [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $PackagePath))
    }
    if (-not (Test-Path -LiteralPath $ResolvedPackagePath)) {
        throw "Package path was not found: $ResolvedPackagePath"
    }

    if ((Get-Item -LiteralPath $ResolvedPackagePath).PSIsContainer) {
        $Targets = Get-ChildItem -LiteralPath $ResolvedPackagePath -Recurse -File |
            Where-Object { $_.Extension -in @(".exe", ".dll", ".pyd") } |
            Sort-Object FullName
    } else {
        $Targets = @(Get-Item -LiteralPath $ResolvedPackagePath)
    }

    if (-not $Targets -or $Targets.Count -eq 0) {
        throw "No signable Windows artifacts were found under: $ResolvedPackagePath"
    }

    foreach ($Target in $Targets) {
        Invoke-EDBWindowsSignature `
            -Path $Target.FullName `
            -SignTool $SignTool `
            -CertificatePath $CertificatePath `
            -CertificatePassword $CertificatePassword `
            -CertificateSubject $CertificateSubject `
            -CertificateThumbprint $CertificateThumbprint `
            -CertificateAutoSelect:$CertificateAutoSelect `
            -TimestampUrl $TimestampUrl `
            -Description $Description
    }
}
