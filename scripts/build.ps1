# Builds one module at one commit against a staged set of upstream artefacts.
# Stage JSON: {"core": {"tag","url","sha256"}, "deps": {"<module>": {"version","url","sha256"}}}
# Writes <OutDir>/<Module>.zip (files the build added or changed in ProgramData\BHoM\Assemblies)
# and <OutDir>/manifest.json. Exit 0 on success, 1 on build failure, 2 on staging failure.
param(
    [Parameter(Mandatory)] [string] $Module,
    [Parameter(Mandatory)] [string] $Sha,
    [Parameter(Mandatory)] [string] $Stage,
    [Parameter(Mandatory)] [string] $OutDir,
    [string] $Owner = 'BuroHappoldEngineeringSandbox'
)
$ErrorActionPreference = 'Stop'
$stageData = Get-Content $Stage -Raw | ConvertFrom-Json
New-Item -ItemType Directory -Force $OutDir | Out-Null
$asm = "$env:ProgramData\BHoM\Assemblies"

function Write-Manifest([bool] $ok, [string] $version, [string] $reason, $files, [string] $payloadHash) {
    $deps = [ordered]@{}
    foreach ($p in ($stageData.deps.PSObject.Properties | Sort-Object Name)) { $deps[$p.Name] = $p.Value.version }
    [ordered]@{
        module        = $Module
        sha           = $Sha
        ok            = $ok
        reason        = $reason
        version       = $version
        built_against = [ordered]@{ core = $coreTag; deps = $deps }
        payload_sha256 = $payloadHash
        files         = @($files)
    } | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $OutDir 'manifest.json') -Encoding utf8
}

function Get-Sha256([string] $path) { (Get-FileHash $path -Algorithm SHA256).Hash.ToLower() }

function Expand-Verified([string] $url, [string] $expected, [string] $label) {
    $zip = Join-Path $env:RUNNER_TEMP ("stage-" + [guid]::NewGuid().ToString('N') + '.zip')
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    $actual = Get-Sha256 $zip
    if ($actual -ne $expected) { throw "hash mismatch for ${label}: expected $expected, got $actual" }
    Expand-Archive -Path $zip -DestinationPath $asm -Force
    Remove-Item $zip -Force
}

# Version: commit plus a digest of exactly what it was built against, so the same source built
# against different upstreams gets a different version. integrate.py computes the same string.
$coreTag = if ($stageData.core) { $stageData.core.tag } else { $null }
$basis = @("core=$(if ($coreTag) { $coreTag } else { 'none' })") + @($stageData.deps.PSObject.Properties | Sort-Object Name | ForEach-Object { "$($_.Name)=$($_.Value.version)" })
$digest = [System.BitConverter]::ToString([System.Security.Cryptography.SHA256]::Create().ComputeHash([System.Text.Encoding]::UTF8.GetBytes(($basis -join ';')))).Replace('-', '').ToLower().Substring(0, 8)
$version = $Sha.Substring(0, 8) + '-' + $digest

try {
    if (Test-Path $asm) { Remove-Item $asm -Recurse -Force }
    New-Item -ItemType Directory -Force $asm | Out-Null
    if ($stageData.core) { Expand-Verified $stageData.core.url $stageData.core.sha256 'core' }
    foreach ($p in $stageData.deps.PSObject.Properties) { Expand-Verified $p.Value.url $p.Value.sha256 $p.Name }
} catch {
    Write-Host "::error title=staging failed::$($_.Exception.Message)"
    Write-Manifest $false $version "staging: $($_.Exception.Message)" @() ''
    exit 2
}

$before = @{}
Get-ChildItem $asm -File -Recurse | ForEach-Object { $before[$_.FullName] = Get-Sha256 $_.FullName }

$src = "C:\proto-src\$Module"
if (Test-Path $src) { Remove-Item $src -Recurse -Force }
git clone -q "https://github.com/$Owner/$Module.git" $src
if ($LASTEXITCODE -ne 0) { Write-Manifest $false $version 'clone failed' @() ''; exit 2 }
git -C $src checkout -q $Sha
if ($LASTEXITCODE -ne 0) { Write-Manifest $false $version "checkout of $Sha failed" @() ''; exit 2 }
[ordered]@{ sdk = [ordered]@{ version = '8.0.100'; rollForward = 'latestFeature' } } | ConvertTo-Json | Set-Content "C:\proto-src\global.json" -Encoding utf8

$sln = Join-Path $src (($Module -replace '^proto-', '') + '.sln')
dotnet build $sln -c Release --nologo -m
if ($LASTEXITCODE -ne 0) {
    Write-Host "::error title=build failed::$Module at $Sha did not build against $($basis -join ', ')"
    Write-Manifest $false $version 'build failed' @() ''
    exit 1
}

$payload = @(Get-ChildItem $asm -File -Recurse | Where-Object { -not $before.ContainsKey($_.FullName) -or $before[$_.FullName] -ne (Get-Sha256 $_.FullName) })
if ($payload.Count -eq 0) { Write-Manifest $false $version 'build produced no files in Assemblies' @() ''; exit 1 }
$zip = Join-Path $OutDir "$Module.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $payload.FullName -DestinationPath $zip
Write-Manifest $true $version 'ok' ($payload | ForEach-Object { $_.Name } | Sort-Object) (Get-Sha256 $zip)
Write-Host "built $Module $version, $($payload.Count) files"
exit 0
