#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Install hermes-node-client as a Windows Service.

.DESCRIPTION
    Downloads hermes-node-client, installs dependencies, creates
    a Windows Service via NSSM or Task Scheduler fallback.

.PARAMETER GatewayUrl
    WebSocket URL of the Hermes Gateway or API Server.

.PARAMETER Token
    Authentication token for node registration.

.PARAMETER NodeId
    Unique identifier for this node (default: hostname).

.PARAMETER InstallDir
    Installation directory (default: C:\ProgramData\hermes-node-client).

.EXAMPLE
    .\install.ps1 -GatewayUrl "ws://192.168.1.100:8642/ws" -Token "my-secret-token"
#>
param(
    [Parameter(Mandatory=$true)]
    [string]$GatewayUrl,

    [Parameter(Mandatory=$true)]
    [string]$Token,

    [string]$NodeId = $env:COMPUTERNAME,

    [string]$InstallDir = "C:\ProgramData\hermes-node-client"
)

$ErrorActionPreference = "Stop"

Write-Host "=== Hermes Node Client Installer ===" -ForegroundColor Cyan
Write-Host ""

# 1. Check prerequisites
Write-Host "Checking prerequisites..." -ForegroundColor Yellow

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    $python = Get-Command python3 -ErrorAction SilentlyContinue
}
if (-not $python) {
    Write-Error "Python not found. Please install Python 3.9+ from https://python.org"
    exit 1
}
Write-Host "  Python: $($python.Source)" -ForegroundColor Green

$pip = Get-Command pip -ErrorAction SilentlyContinue
if (-not $pip) {
    Write-Error "pip not found"
    exit 1
}

# 2. Create installation directory
Write-Host "Creating installation directory..." -ForegroundColor Yellow
if (-not (Test-Path $InstallDir)) {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
}
$RepoDir = Join-Path $InstallDir "hermes-node-client"

# 3. Clone or update repository
Write-Host "Downloading hermes-node-client..." -ForegroundColor Yellow
if (Test-Path (Join-Path $RepoDir ".git")) {
    Write-Host "  Updating existing installation..." -ForegroundColor Gray
    Set-Location $RepoDir
    git pull
} else {
    if (Test-Path $RepoDir) {
        Remove-Item -Recurse -Force $RepoDir
    }
    git clone https://github.com/goodbaikin/hermes-node-client.git $RepoDir
}

# 4. Install Python dependencies
Write-Host "Installing Python dependencies..." -ForegroundColor Yellow
& $pip.Source install -r (Join-Path $RepoDir "requirements.txt")

# 5. Create configuration
Write-Host "Creating configuration..." -ForegroundColor Yellow
$envContent = @"
HERMES_NODE_ID=$NodeId
HERMES_GATEWAY_URL=$GatewayUrl
HERMES_NODE_TOKEN=$Token
"@
$envContent | Out-File -FilePath (Join-Path $RepoDir ".env") -Encoding UTF8

# 6. Install as Windows Service (NSSM preferred)
Write-Host "Installing Windows Service..." -ForegroundColor Yellow

$nssm = Get-Command nssm -ErrorAction SilentlyContinue
if ($nssm) {
    Write-Host "  Using NSSM..." -ForegroundColor Gray
    $serviceName = "HermesNode-$NodeId"
    & nssm install $serviceName $python.Source (Join-Path $RepoDir "hermes_node_client.py")
    & nssm set $serviceName AppDirectory $RepoDir
    & nssm set $serviceName AppEnvironmentExtra ("HERMES_NODE_ID=$NodeId;HERMES_GATEWAY_URL=$GatewayUrl;HERMES_NODE_TOKEN=$Token")
    & nssm set $serviceName Start SERVICE_AUTO_START
    & nssm start $serviceName
    Write-Host "  Service installed: $serviceName" -ForegroundColor Green
} else {
    # Fallback: Task Scheduler
    Write-Host "  NSSM not found, using Task Scheduler..." -ForegroundColor Yellow
    $taskName = "HermesNode-$NodeId"
    $action = New-ScheduledTaskAction -Execute $python.Source -Argument (Join-Path $RepoDir "hermes_node_client.py") -WorkingDirectory $RepoDir
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType ServiceAccount -RunLevel Highest

    # Environment variables for the task
    $envVars = @{
        HERMES_NODE_ID = $NodeId
        HERMES_GATEWAY_URL = $GatewayUrl
        HERMES_NODE_TOKEN = $Token
    }

    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null
    Start-ScheduledTask -TaskName $taskName
    Write-Host "  Task scheduled: $taskName" -ForegroundColor Green
}

# 7. Install LSP servers (optional)
Write-Host ""
Write-Host "Installing LSP servers..." -ForegroundColor Yellow

# Python
Write-Host "  Python (pyright)..." -ForegroundColor Gray
& $pip.Source install pyright

# C# (csharp-ls)
Write-Host "  C# (csharp-ls)..." -ForegroundColor Gray
$dotnet = Get-Command dotnet -ErrorAction SilentlyContinue
if ($dotnet) {
    & dotnet tool install --global csharp-ls 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "    csharp-ls may already be installed, attempting update..." -ForegroundColor Yellow
        & dotnet tool update --global csharp-ls 2>$null
    }
    # Ensure ~/.dotnet/tools is in PATH
    $dotnetToolsDir = Join-Path $env:USERPROFILE ".dotnet" "tools"
    $currentPath = [Environment]::GetEnvironmentVariable("PATH", "User")
    if ($currentPath -notlike "*$dotnetToolsDir*") {
        [Environment]::SetEnvironmentVariable("PATH", "$currentPath;$dotnetToolsDir", "User")
        Write-Host "    Added $dotnetToolsDir to PATH" -ForegroundColor Green
    }
    Write-Host "    csharp-ls installed/updated" -ForegroundColor Green
} else {
    Write-Warning ".NET SDK not found. Install from https://dotnet.microsoft.com/download to use csharp-ls"
}

Write-Host ""
Write-Host "=== Installation Complete ===" -ForegroundColor Cyan
Write-Host "Node ID: $NodeId" -ForegroundColor White
Write-Host "Gateway: $GatewayUrl" -ForegroundColor White
Write-Host "Install: $RepoDir" -ForegroundColor White
Write-Host ""
Write-Host "To check status:" -ForegroundColor Yellow
Write-Host "  Get-Service HermesNode-*" -ForegroundColor Gray
Write-Host "  Get-ScheduledTask HermesNode-*" -ForegroundColor Gray
