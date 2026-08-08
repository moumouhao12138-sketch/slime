param(
    [string]$DockerBinary = "C:\Program Files\Docker\Docker\resources\bin\docker.exe",
    [string]$Image = "slime-cairn-kali:0.0.21",
    [string]$BaseImage = "docker.1ms.run/kalilinux/kali-rolling",
    [string]$KaliAptMirror = "https://mirrors.ustc.edu.cn/kali/",
    [string]$KaliAptVerifyPeer = "false",
    [string]$InstallNativeAgents = "true",
    [string]$InstallReferenceAssets = "false",
    [string]$LabNetwork = "slime-cairn-lab",
    [int]$WaitSeconds = 180
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Desktop = "C:\Program Files\Docker\Docker\Docker Desktop.exe"

if (-not (Test-Path -LiteralPath $DockerBinary)) {
    throw "Docker CLI not found: $DockerBinary"
}

if (-not (Get-Process -Name "Docker Desktop" -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath $Desktop -WindowStyle Hidden
}

$deadline = (Get-Date).AddSeconds($WaitSeconds)
do {
    & $DockerBinary info --format "{{.ServerVersion}}" *> $null
    if ($LASTEXITCODE -eq 0) {
        break
    }
    Start-Sleep -Seconds 3
} while ((Get-Date) -lt $deadline)

if ($LASTEXITCODE -ne 0) {
    throw "Docker Engine did not become ready within $WaitSeconds seconds"
}

& $DockerBinary build `
    --progress=plain `
    --build-arg "BASE_IMAGE=$BaseImage" `
    --build-arg "KALI_APT_MIRROR=$KaliAptMirror" `
    --build-arg "KALI_APT_VERIFY_PEER=$KaliAptVerifyPeer" `
    --build-arg "INSTALL_NATIVE_AGENTS=$InstallNativeAgents" `
    --build-arg "INSTALL_REFERENCE_ASSETS=$InstallReferenceAssets" `
    --tag $Image `
    (Join-Path $ProjectRoot "worker")
if ($LASTEXITCODE -ne 0) {
    throw "Failed to build $Image"
}

$existingNetwork = & $DockerBinary network ls --filter "name=^$LabNetwork$" --format "{{.Name}}"
if ($LASTEXITCODE -ne 0) {
    throw "Failed to query Docker networks"
}
if ($existingNetwork -notcontains $LabNetwork) {
    & $DockerBinary network create --driver bridge --internal $LabNetwork
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create isolated lab network $LabNetwork"
    }
}

& $DockerBinary run --rm --network none --read-only --cap-drop ALL `
    --security-opt no-new-privileges $Image `
    python3 -c "import json,shutil; d=json.load(open('/opt/slime-cairn/tools.json')); print('tools',len(d),'agents',{n:bool(shutil.which(n)) for n in ('codex','claude','pi')})"
if ($LASTEXITCODE -ne 0) {
    throw "Kali image smoke test failed"
}

Write-Host "Docker/Kali ready: $Image"

