[CmdletBinding()]
param(
    [string]$ManifestPath = "",
    [switch]$Force
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ManifestPath)) {
    $scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
    $ManifestPath = Join-Path $scriptRoot "core_tools.json"
}

function Get-PathWithin {
    param(
        [Parameter(Mandatory = $true)][string]$Base,
        [Parameter(Mandatory = $true)][string]$Relative,
        [Parameter(Mandatory = $true)][string]$Description
    )

    if ([string]::IsNullOrWhiteSpace($Relative) -or
        [IO.Path]::IsPathRooted($Relative)) {
        throw "$Description debe ser una ruta relativa: $Relative"
    }
    $baseFull = [IO.Path]::GetFullPath($Base).TrimEnd('\', '/')
    $candidate = [IO.Path]::GetFullPath((Join-Path $baseFull $Relative))
    $prefix = $baseFull + [IO.Path]::DirectorySeparatorChar
    if ($candidate -ne $baseFull -and
        -not $candidate.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Description sale de su directorio permitido: $Relative"
    }
    return $candidate
}

function Get-RequiredText {
    param(
        [Parameter(Mandatory = $true)]$Object,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Context
    )

    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property -or
        [string]::IsNullOrWhiteSpace([string]$property.Value)) {
        throw "Falta $Name en $Context"
    }
    return [string]$property.Value
}

function Get-Sha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = [IO.File]::OpenRead($Path)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
        $stream.Dispose()
    }
}

function Get-ToolFingerprint {
    param([Parameter(Mandatory = $true)]$Tool)

    $identity = [ordered]@{
        id = [string]$Tool.id
        version = [string]$Tool.version
        bootstrap = $Tool.bootstrap
    } | ConvertTo-Json -Depth 20 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($identity)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($algorithm.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
    }
}

function Write-ToolMarker {
    param(
        [Parameter(Mandatory = $true)]$Tool,
        [Parameter(Mandatory = $true)][string]$Target
    )

    $marker = [ordered]@{
        schemaVersion = 1
        id = [string]$Tool.id
        version = [string]$Tool.version
        fingerprint = Get-ToolFingerprint $Tool
    } | ConvertTo-Json -Depth 5
    $encoding = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText((Join-Path $Target ".eap-core-tool.json"), $marker + "`r`n", $encoding)
}

function Test-RequiredFiles {
    param(
        [Parameter(Mandatory = $true)]$Tool,
        [Parameter(Mandatory = $true)][string]$Target
    )

    foreach ($relative in @($Tool.bootstrap.requiredFiles)) {
        $path = Get-PathWithin $Target ([string]$relative) "requiredFiles"
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            return $false
        }
    }
    return $true
}

function Test-ToolValidations {
    param(
        [Parameter(Mandatory = $true)]$Tool,
        [Parameter(Mandatory = $true)][string]$Target
    )

    $validationProperty = $Tool.bootstrap.PSObject.Properties["validation"]
    if ($null -eq $validationProperty) {
        return $true
    }
    foreach ($validation in @($validationProperty.Value)) {
        $type = Get-RequiredText $validation "type" "validation"
        $relative = Get-RequiredText $validation "path" "validation"
        $path = Get-PathWithin $Target $relative "validation.path"
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            return $false
        }
        switch ($type) {
            "command" {
                $arguments = @()
                if ($null -ne $validation.PSObject.Properties["arguments"]) {
                    $arguments = @($validation.arguments | ForEach-Object { [string]$_ })
                }
                try {
                    $output = (& $path @arguments 2>&1 | Out-String)
                    $exitCode = $LASTEXITCODE
                }
                catch {
                    return $false
                }
                if ($exitCode -ne 0) {
                    return $false
                }
                if ($null -ne $validation.PSObject.Properties["expectContains"] -and
                    -not $output.Contains([string]$validation.expectContains)) {
                    return $false
                }
            }
            "fileVersion" {
                $expected = Get-RequiredText $validation "expect" "validation fileVersion"
                $actual = (Get-Item -LiteralPath $path).VersionInfo.ProductVersion
                if ($actual -ne $expected) {
                    return $false
                }
            }
            default {
                throw "Tipo de validacion core no soportado: $type"
            }
        }
    }
    return $true
}

function Test-ToolReady {
    param(
        [Parameter(Mandatory = $true)]$Tool,
        [Parameter(Mandatory = $true)][string]$Target
    )

    if (-not (Test-RequiredFiles $Tool $Target)) {
        return $false
    }
    $fingerprint = Get-ToolFingerprint $Tool
    $markerPath = Join-Path $Target ".eap-core-tool.json"
    if (Test-Path -LiteralPath $markerPath -PathType Leaf) {
        try {
            $marker = Get-Content -LiteralPath $markerPath -Raw | ConvertFrom-Json
            if ($marker.id -eq $Tool.id -and
                $marker.version -eq $Tool.version -and
                $marker.fingerprint -eq $fingerprint) {
                return $true
            }
        }
        catch {
            # Una marca corrupta se trata como una instalacion que debe validarse.
        }
    }
    if (-not (Test-ToolValidations $Tool $Target)) {
        return $false
    }
    Write-ToolMarker $Tool $Target
    return $true
}

function Get-VerifiedArtifact {
    param(
        [Parameter(Mandatory = $true)]$Tool,
        [Parameter(Mandatory = $true)]$Artifact,
        [Parameter(Mandatory = $true)][string]$DownloadRoot
    )

    $fileName = Get-RequiredText $Artifact "fileName" "artifact de $($Tool.id)"
    if ([IO.Path]::GetFileName($fileName) -ne $fileName) {
        throw "fileName no puede contener directorios: $fileName"
    }
    $url = Get-RequiredText $Artifact "url" "artifact de $($Tool.id)"
    $expected = (Get-RequiredText $Artifact "sha256" "artifact de $($Tool.id)").ToLowerInvariant()
    if ($expected -notmatch '^[0-9a-f]{64}$') {
        throw "SHA-256 no valido para $fileName"
    }
    $archive = Join-Path $DownloadRoot $fileName
    $partial = $archive + ".partial"
    New-Item -ItemType Directory -Path $DownloadRoot -Force | Out-Null
    if ((Test-Path -LiteralPath $archive -PathType Leaf) -and
        (Get-Sha256 $archive) -eq $expected) {
        return $archive
    }
    Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
    Write-Host "EAP: descargando $fileName..."
    Invoke-WebRequest -Uri $url -OutFile $partial -UseBasicParsing
    $actual = Get-Sha256 $partial
    if ($actual -ne $expected) {
        Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
        throw "SHA-256 incorrecto para $fileName (esperado $expected, obtenido $actual)"
    }
    [IO.File]::Move($partial, $archive)
    return $archive
}

function Expand-ZipSafely {
    param(
        [Parameter(Mandatory = $true)][string]$Archive,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    $destinationFull = [IO.Path]::GetFullPath($Destination).TrimEnd('\', '/')
    $prefix = $destinationFull + [IO.Path]::DirectorySeparatorChar
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName.Replace('/', [IO.Path]::DirectorySeparatorChar)
            if ([string]::IsNullOrWhiteSpace($name)) {
                continue
            }
            $target = [IO.Path]::GetFullPath((Join-Path $destinationFull $name))
            if (-not $target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
                throw "Entrada ZIP fuera del destino: $($entry.FullName)"
            }
            if ([string]::IsNullOrEmpty($entry.Name)) {
                New-Item -ItemType Directory -Path $target -Force | Out-Null
                continue
            }
            $parent = Split-Path -Parent $target
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
        }
    }
    finally {
        $zip.Dispose()
    }
}

function Install-Artifact {
    param(
        [Parameter(Mandatory = $true)]$Artifact,
        [Parameter(Mandatory = $true)][string]$Archive,
        [Parameter(Mandatory = $true)][string]$Payload
    )

    if ($null -eq $Artifact.PSObject.Properties["install"]) {
        throw "Falta install en el artifact $($Artifact.fileName)"
    }
    $install = $Artifact.install
    $type = Get-RequiredText $install "type" "install de $($Artifact.fileName)"
    $destinationText = "."
    if ($null -ne $install.PSObject.Properties["destination"]) {
        $destinationText = [string]$install.destination
    }
    $destination = Get-PathWithin $Payload $destinationText "install.destination"
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    switch ($type) {
        "zip" {
            if ($null -eq $install.PSObject.Properties["source"]) {
                Expand-ZipSafely $Archive $destination
            }
            else {
                $extractRoot = Join-Path $Payload (".zip." + [Guid]::NewGuid().ToString("N"))
                try {
                    Expand-ZipSafely $Archive $extractRoot
                    $source = Get-PathWithin $extractRoot ([string]$install.source) "install.source"
                    if (-not (Test-Path -LiteralPath $source -PathType Container)) {
                        throw "El ZIP $($Artifact.fileName) no contiene install.source: $($install.source)"
                    }
                    foreach ($item in Get-ChildItem -LiteralPath $source -Force) {
                        $itemTarget = Join-Path $destination $item.Name
                        if (Test-Path -LiteralPath $itemTarget) {
                            throw "El ZIP intenta sobrescribir un archivo ya preparado: $($item.Name)"
                        }
                        Move-Item -LiteralPath $item.FullName -Destination $itemTarget
                    }
                }
                finally {
                    Remove-Item -LiteralPath $extractRoot -Recurse -Force -ErrorAction SilentlyContinue
                }
            }
        }
        "file" {
            $targetName = Get-RequiredText $install "target" "install file de $($Artifact.fileName)"
            if ([IO.Path]::GetFileName($targetName) -ne $targetName) {
                throw "install.target no puede contener directorios: $targetName"
            }
            Copy-Item -LiteralPath $Archive -Destination (Join-Path $destination $targetName)
        }
        "sevenZip" {
            $extractorText = Get-RequiredText $install "executable" "install sevenZip de $($Artifact.fileName)"
            $extractor = Get-PathWithin $Payload $extractorText "install.executable"
            if (-not (Test-Path -LiteralPath $extractor -PathType Leaf)) {
                throw "No existe el extractor declarado para $($Artifact.fileName): $extractorText"
            }
            $output = (& $extractor x $Archive ("-o" + $destination) -y 2>&1 | Out-String)
            if ($LASTEXITCODE -ne 0) {
                throw "No se pudo extraer $($Artifact.fileName) con $extractorText`: $output"
            }
        }
        default {
            throw "Tipo de instalacion core no soportado: $type"
        }
    }
}

function Invoke-PostInstall {
    param(
        [Parameter(Mandatory = $true)]$Tool,
        [Parameter(Mandatory = $true)][string]$Payload
    )

    $postProperty = $Tool.bootstrap.PSObject.Properties["postInstall"]
    if ($null -eq $postProperty) {
        return
    }
    $post = $postProperty.Value
    $type = Get-RequiredText $post "type" "postInstall de $($Tool.id)"
    switch ($type) {
        "pythonPath" {
            $relative = Get-RequiredText $post "path" "postInstall pythonPath"
            $path = Get-PathWithin $Payload $relative "postInstall.path"
            $entries = @($post.entries | ForEach-Object { [string]$_ })
            if ($entries.Count -eq 0) {
                throw "postInstall pythonPath necesita entries"
            }
            $encoding = New-Object Text.ASCIIEncoding
            [IO.File]::WriteAllText($path, ($entries -join "`r`n") + "`r`n", $encoding)
        }
        default {
            throw "Tipo de postInstall core no soportado: $type"
        }
    }
}

function Install-CoreTool {
    param(
        [Parameter(Mandatory = $true)]$Tool,
        [Parameter(Mandatory = $true)][string]$Target,
        [Parameter(Mandatory = $true)][string]$TempRoot,
        [Parameter(Mandatory = $true)][string]$CoreRoot
    )

    $toolId = [string]$Tool.id
    $version = Get-RequiredText $Tool "version" "tool $toolId"
    $artifacts = @($Tool.bootstrap.artifacts)
    if ($artifacts.Count -eq 0) {
        throw "La tool $toolId no declara artifacts"
    }
    $downloadRoot = Join-Path (Join-Path (Join-Path $TempRoot "downloads") $toolId) $version
    $downloads = @()
    foreach ($artifact in $artifacts) {
        $downloads += [pscustomobject]@{
            definition = $artifact
            path = Get-VerifiedArtifact $Tool $artifact $downloadRoot
        }
    }

    $transactionRoot = Join-Path (Join-Path $TempRoot "staging") ("{0}.{1}" -f $toolId, [Guid]::NewGuid().ToString("N"))
    $payload = Join-Path $transactionRoot "payload"
    New-Item -ItemType Directory -Path $payload -Force | Out-Null
    try {
        Write-Host "EAP: instalando $($Tool.displayName) $version..."
        foreach ($download in $downloads) {
            Install-Artifact $download.definition $download.path $payload
        }
        Invoke-PostInstall $Tool $payload
        if (-not (Test-RequiredFiles $Tool $payload)) {
            throw "La instalacion de $toolId no contiene todos los requiredFiles"
        }
        if (-not (Test-ToolValidations $Tool $payload)) {
            throw "La validacion de $toolId ha fallado"
        }
        Write-ToolMarker $Tool $payload

        $backup = Get-PathWithin $CoreRoot (".{0}.backup.{1}" -f $toolId, [Guid]::NewGuid().ToString("N")) "backup core"
        $hasBackup = $false
        if (Test-Path -LiteralPath $Target) {
            Move-Item -LiteralPath $Target -Destination $backup
            $hasBackup = $true
        }
        try {
            Move-Item -LiteralPath $payload -Destination $Target
        }
        catch {
            if ($hasBackup -and -not (Test-Path -LiteralPath $Target)) {
                Move-Item -LiteralPath $backup -Destination $Target
            }
            throw
        }
        if ($hasBackup) {
            Remove-Item -LiteralPath $backup -Recurse -Force
        }
        Write-Host "EAP: $($Tool.displayName) listo en core\$($Tool.directory)."
    }
    finally {
        Remove-Item -LiteralPath $transactionRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

$lockStream = $null
try {
    $ManifestPath = [IO.Path]::GetFullPath($ManifestPath)
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        throw "No se encuentra el manifiesto core: $ManifestPath"
    }
    $coreRoot = Split-Path -Parent $ManifestPath
    $eapRoot = Split-Path -Parent $coreRoot
    $tempRoot = Join-Path $eapRoot "temp\core-bootstrap"
    New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null

    $lockPath = Join-Path $tempRoot "bootstrap.lock"
    $deadline = [DateTime]::UtcNow.AddMinutes(2)
    while ($null -eq $lockStream) {
        try {
            $lockStream = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
        }
        catch [IO.IOException] {
            if ([DateTime]::UtcNow -ge $deadline) {
                throw "Otra instancia de EAP lleva mas de dos minutos preparando core"
            }
            Start-Sleep -Milliseconds 200
        }
    }

    $manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    if ($manifest.schemaVersion -ne 1 -or $null -eq $manifest.tools) {
        throw "core_tools.json no cumple schemaVersion 1"
    }
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    foreach ($tool in @($manifest.tools)) {
        if ($null -eq $tool.PSObject.Properties["bootstrap"]) {
            continue
        }
        $toolId = Get-RequiredText $tool "id" "tool core"
        $directory = Get-RequiredText $tool "directory" "tool $toolId"
        $target = Get-PathWithin $coreRoot $directory "directory de $toolId"
        if (-not $Force -and (Test-ToolReady $tool $target)) {
            continue
        }
        Install-CoreTool $tool $target $tempRoot $coreRoot
    }
}
catch {
    Write-Error "Bootstrap de EAP fallido: $($_.Exception.Message)"
    exit 1
}
finally {
    if ($null -ne $lockStream) {
        $lockStream.Dispose()
    }
}
