$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "../..")).Path
$Local = Join-Path $Root "local"
Set-Location $Root
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw "Docker Desktop is required." }
docker info *> $null
if ($LASTEXITCODE -ne 0) { throw "Start Docker Desktop and retry." }
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { throw "Install GitHub CLI." }
gh auth status *> $null
if ($LASTEXITCODE -ne 0) { throw "Authenticate with gh auth login." }

$Api = Join-Path (Split-Path $Root -Parent) "CompileCare.Api"
$Web = Join-Path (Split-Path $Root -Parent) "CompileCare.Web"
if (-not (Test-Path (Join-Path $Api ".git"))) { gh repo clone CompileCare/CompileCare.Api $Api; if ($LASTEXITCODE -ne 0) { throw "Could not clone CompileCare.Api." } }
if (-not (Test-Path (Join-Path $Web ".git"))) { gh repo clone CompileCare/CompileCare.Web $Web; if ($LASTEXITCODE -ne 0) { throw "Could not clone CompileCare.Web." } }
$env:COMPILECARE_API_PATH = $Api
$env:COMPILECARE_WEB_PATH = $Web

$EnvFile = Join-Path $Local ".env"
if (-not (Test-Path $EnvFile)) {
  Copy-Item (Join-Path $Local ".env.example") $EnvFile
  $authority = Read-Host "Development OIDC Authority URL [http://keycloak.localhost:8082/realms/compilecare]"
  if ([string]::IsNullOrWhiteSpace($authority)) { $authority = "http://keycloak.localhost:8082/realms/compilecare" }
  $audience = Read-Host "API Audience [compilecare-api]"; if ([string]::IsNullOrWhiteSpace($audience)) { $audience = "compilecare-api" }
  $client = Read-Host "Web Client ID [compilecare-web]"; if ([string]::IsNullOrWhiteSpace($client)) { $client = "compilecare-web" }
  $scope = Read-Host "OIDC Scope [openid profile]"; if ([string]::IsNullOrWhiteSpace($scope)) { $scope = "openid profile" }
  $content = Get-Content $EnvFile -Raw
  $content = $content -replace '(?m)^OIDC_AUTHORITY=.*$', "OIDC_AUTHORITY=$authority"
  $content = $content -replace '(?m)^OIDC_AUDIENCE=.*$', "OIDC_AUDIENCE=$audience"
  $content = $content -replace '(?m)^OIDC_CLIENT_ID=.*$', "OIDC_CLIENT_ID=$client"
  $content = $content -replace '(?m)^OIDC_SCOPE=.*$', "OIDC_SCOPE=$scope"
  Set-Content -Path $EnvFile -Value $content -NoNewline -Encoding ascii
}
$bootstrapLine = Get-Content $EnvFile | Where-Object { $_ -match '^PLATFORM_BOOTSTRAP_SECRET=' } | Select-Object -First 1
if (-not $bootstrapLine -or $bootstrapLine -match '^PLATFORM_BOOTSTRAP_SECRET=replace-with-') {
  $randomBytes = New-Object byte[] 32
  [System.Security.Cryptography.RandomNumberGenerator]::Fill($randomBytes)
  $bootstrapSecret = [Convert]::ToHexString($randomBytes).ToLowerInvariant()
  $envContent = Get-Content $EnvFile -Raw
  if ($envContent -match '(?m)^PLATFORM_BOOTSTRAP_SECRET=.* New-Item -ItemType Directory -Force -Path $Secrets | Out-Null
$CredentialFile = Join-Path $Secrets "nuget-source-credentials"
if (-not (Test-Path $CredentialFile)) {
  $username = Read-Host "GitHub username [Shatru-compile]"; if ([string]::IsNullOrWhiteSpace($username)) { $username = "Shatru-compile" }
  $secure = Read-Host "GitHub PAT (read:packages)" -AsSecureString
  $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
  try { $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
  if ([string]::IsNullOrWhiteSpace($token)) { throw "GitHub Packages token required." }
  Set-Content -Path $CredentialFile -Value "Username=$username;Password=$token;ValidAuthenticationTypes=Basic" -NoNewline -Encoding ascii
  $token = $null
}
$allContainers = docker ps -a --format "{{.Names}}"
if ($allContainers -contains "compilecare-keycloak") {
  $format = '{{ index .Config.Labels "com.docker.compose.project" }}'
  $project = docker inspect --format $format compilecare-keycloak
  if ($project -ne "compilecare-local") {
    Write-Host "Removing the earlier standalone compilecare-keycloak container."
    docker rm -f compilecare-keycloak *> $null
    if ($LASTEXITCODE -ne 0) { throw "Could not remove the earlier temporary Keycloak container." }
  }
}
docker compose --env-file $EnvFile -f (Join-Path $Local "docker-compose.yml") up --build -d
if ($LASTEXITCODE -ne 0) { throw "Docker Compose failed. Inspect local logs." }
for ($i=0; $i -lt 90; $i++) {
  try {
    Invoke-WebRequest http://localhost:8081/ -UseBasicParsing -TimeoutSec 2 | Out-Null
    Invoke-WebRequest http://localhost:8080/ -UseBasicParsing -TimeoutSec 2 | Out-Null
    Start-Process "http://localhost:8080"
    Write-Host "CompileCare is running. Local identity provider: http://keycloak.localhost:8082"
    exit 0
  } catch { Start-Sleep -Seconds 2 }
}
docker compose --env-file $EnvFile -f (Join-Path $Local "docker-compose.yml") ps
throw "Startup timeout. Inspect local logs."
) {
    $envContent = $envContent -replace '(?m)^PLATFORM_BOOTSTRAP_SECRET=.* New-Item -ItemType Directory -Force -Path $Secrets | Out-Null
$CredentialFile = Join-Path $Secrets "nuget-source-credentials"
if (-not (Test-Path $CredentialFile)) {
  $username = Read-Host "GitHub username [Shatru-compile]"; if ([string]::IsNullOrWhiteSpace($username)) { $username = "Shatru-compile" }
  $secure = Read-Host "GitHub PAT (read:packages)" -AsSecureString
  $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
  try { $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
  if ([string]::IsNullOrWhiteSpace($token)) { throw "GitHub Packages token required." }
  Set-Content -Path $CredentialFile -Value "Username=$username;Password=$token;ValidAuthenticationTypes=Basic" -NoNewline -Encoding ascii
  $token = $null
}
$allContainers = docker ps -a --format "{{.Names}}"
if ($allContainers -contains "compilecare-keycloak") {
  $project = docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' compilecare-keycloak
  if ($project -ne "compilecare-local") {
    Write-Host "Removing the earlier standalone compilecare-keycloak container."
    docker rm -f compilecare-keycloak *> $null
    if ($LASTEXITCODE -ne 0) { throw "Could not remove the earlier temporary Keycloak container." }
  }
}
docker compose --env-file $EnvFile -f (Join-Path $Local "docker-compose.yml") up --build -d
if ($LASTEXITCODE -ne 0) { throw "Docker Compose failed. Inspect local logs." }
for ($i=0; $i -lt 90; $i++) {
  try {
    Invoke-WebRequest http://localhost:8081/ -UseBasicParsing -TimeoutSec 2 | Out-Null
    Invoke-WebRequest http://localhost:8080/ -UseBasicParsing -TimeoutSec 2 | Out-Null
    Start-Process "http://localhost:8080"
    Write-Host "CompileCare is running. Local identity provider: http://keycloak.localhost:8082"
    exit 0
  } catch { Start-Sleep -Seconds 2 }
}
docker compose --env-file $EnvFile -f (Join-Path $Local "docker-compose.yml") ps
throw "Startup timeout. Inspect local logs."
, "PLATFORM_BOOTSTRAP_SECRET=$bootstrapSecret"
    Set-Content -Path $EnvFile -Value $envContent -NoNewline -Encoding ascii
  } else {
    Add-Content -Path $EnvFile -Value "`nPLATFORM_BOOTSTRAP_SECRET=$bootstrapSecret"
  }
  Write-Host "One-time local platform bootstrap secret (enter it in the first-run workspace setup): $bootstrapSecret"
  $bootstrapSecret = $null
}
$Secrets = Join-Path $Local ".secrets"; New-Item -ItemType Directory -Force -Path $Secrets | Out-Null
$CredentialFile = Join-Path $Secrets "nuget-source-credentials"
if (-not (Test-Path $CredentialFile)) {
  $username = Read-Host "GitHub username [Shatru-compile]"; if ([string]::IsNullOrWhiteSpace($username)) { $username = "Shatru-compile" }
  $secure = Read-Host "GitHub PAT (read:packages)" -AsSecureString
  $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
  try { $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
  if ([string]::IsNullOrWhiteSpace($token)) { throw "GitHub Packages token required." }
  Set-Content -Path $CredentialFile -Value "Username=$username;Password=$token;ValidAuthenticationTypes=Basic" -NoNewline -Encoding ascii
  $token = $null
}
$allContainers = docker ps -a --format "{{.Names}}"
if ($allContainers -contains "compilecare-keycloak") {
  $project = docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' compilecare-keycloak
  if ($project -ne "compilecare-local") {
    Write-Host "Removing the earlier standalone compilecare-keycloak container."
    docker rm -f compilecare-keycloak *> $null
    if ($LASTEXITCODE -ne 0) { throw "Could not remove the earlier temporary Keycloak container." }
  }
}
docker compose --env-file $EnvFile -f (Join-Path $Local "docker-compose.yml") up --build -d
if ($LASTEXITCODE -ne 0) { throw "Docker Compose failed. Inspect local logs." }
for ($i=0; $i -lt 90; $i++) {
  try {
    Invoke-WebRequest http://localhost:8081/ -UseBasicParsing -TimeoutSec 2 | Out-Null
    Invoke-WebRequest http://localhost:8080/ -UseBasicParsing -TimeoutSec 2 | Out-Null
    Start-Process "http://localhost:8080"
    Write-Host "CompileCare is running. Local identity provider: http://keycloak.localhost:8082"
    exit 0
  } catch { Start-Sleep -Seconds 2 }
}
docker compose --env-file $EnvFile -f (Join-Path $Local "docker-compose.yml") ps
throw "Startup timeout. Inspect local logs."
