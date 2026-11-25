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
$existing = docker ps --format "{{.Names}}"
if ($existing -contains "compilecare-keycloak") {
  Write-Host "A temporary compilecare-keycloak container is already running."
  Write-Host "Stop it with: docker stop compilecare-keycloak"
  throw "Stop the temporary Keycloak container, then run the launcher again."
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
