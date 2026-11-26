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
$NewLocalEnv = $false
if (-not (Test-Path $EnvFile)) {
  $NewLocalEnv = $true
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

# Restore the intended profile claim scope after the earlier local diagnostic workaround.
$envLines = Get-Content $EnvFile
if ($envLines -contains "OIDC_SCOPE=openid") {
  $envContent = Get-Content $EnvFile -Raw
  $envContent = $envContent -replace '(?m)^OIDC_SCOPE=openid$', 'OIDC_SCOPE=openid profile'
  Set-Content -Path $EnvFile -Value $envContent -NoNewline -Encoding ascii
}

function New-LocalSecret {
  $bytes = New-Object byte[] 32
  $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  try { $generator.GetBytes($bytes) } finally { $generator.Dispose() }
  return [BitConverter]::ToString($bytes).Replace("-", "").ToLowerInvariant()
}
$envContent = Get-Content $EnvFile -Raw
foreach ($key in @("KEYCLOAK_ADMIN_PASSWORD", "POSTGRES_PASSWORD")) {
  $line = Get-Content $EnvFile | Where-Object { $_ -match "^$key=" } | Select-Object -First 1
  $value = if ($line) { $line.Substring($key.Length + 1) } else { "" }
  if ($NewLocalEnv -and ([string]::IsNullOrWhiteSpace($value) -or $value.StartsWith("replace-with-"))) {
    $secret = New-LocalSecret
    $envContent = $envContent -replace "(?m)^$key=.*$", "$key=$secret"
    Set-Content -Path $EnvFile -Value $envContent -NoNewline -Encoding ascii
    $envContent = Get-Content $EnvFile -Raw
    $secret = $null
  } elseif ([string]::IsNullOrWhiteSpace($value) -or $value.StartsWith("replace-with-")) {
    throw "$key is still a placeholder in an existing local/.env. Set a private value before starting; credentials are not rotated automatically because existing Docker volumes may already use them."
  } elseif ($key -eq "KEYCLOAK_ADMIN_PASSWORD" -and $value -eq "ChangeThisLocalPassword123!") {
    throw "The existing Keycloak password is the known sample value. Change it in the running Keycloak admin console, then update local/.env before starting."
  } elseif ($value -eq "compilecare-local-only") {
    Write-Warning "Preserving legacy local $key to avoid desynchronizing an existing PostgreSQL volume. Rotate the database role password before updating local/.env."
  }
}

$bootstrapLine = Get-Content $EnvFile | Where-Object { $_ -match '^PLATFORM_BOOTSTRAP_SECRET=' } | Select-Object -First 1
if (-not $bootstrapLine -or $bootstrapLine -match '^PLATFORM_BOOTSTRAP_SECRET=replace-with-') {
  $randomBytes = New-Object byte[] 32
  $randomGenerator = New-Object System.Security.Cryptography.RNGCryptoServiceProvider
  $randomGenerator.GetBytes($randomBytes)
  $randomGenerator.Dispose()
  $bootstrapSecret = [BitConverter]::ToString($randomBytes).Replace("-", "").ToLowerInvariant()
  $envContent = Get-Content $EnvFile -Raw
  if ($envContent -match '(?m)^PLATFORM_BOOTSTRAP_SECRET=.*$') {
    $envContent = $envContent -replace '(?m)^PLATFORM_BOOTSTRAP_SECRET=.*$', "PLATFORM_BOOTSTRAP_SECRET=$bootstrapSecret"
    Set-Content -Path $EnvFile -Value $envContent -NoNewline -Encoding ascii
  } else {
    Add-Content -Path $EnvFile -Value "`nPLATFORM_BOOTSTRAP_SECRET=$bootstrapSecret"
  }
  Write-Host "One-time local platform bootstrap secret (enter it in the first-run workspace setup): $bootstrapSecret"
  $bootstrapSecret = $null
}

$Secrets = Join-Path $Local ".secrets"
New-Item -ItemType Directory -Force -Path $Secrets | Out-Null
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
  $containerInfo = docker inspect compilecare-keycloak | ConvertFrom-Json
  $labels = $containerInfo[0].Config.Labels
  $projectProperty = $labels.PSObject.Properties | Where-Object { $_.Name -eq "com.docker.compose.project" } | Select-Object -First 1
  $project = if ($projectProperty) { $projectProperty.Value } else { "" }
  if ($project -ne "compilecare-local") {
    Write-Host "Removing the earlier standalone compilecare-keycloak container."
    docker rm -f compilecare-keycloak *> $null
    if ($LASTEXITCODE -ne 0) { throw "Could not remove the earlier temporary Keycloak container." }
  }
}

$BaseCompose = Join-Path $Local "docker-compose.yml"
$ObservabilityCompose = Join-Path $Local "docker-compose.observability.yml"
$ComposeArgs = @("--env-file", $EnvFile, "-f", $BaseCompose, "-f", $ObservabilityCompose)

Write-Host "Validating the complete application and observability Compose configuration..."
& docker compose @ComposeArgs config --quiet
if ($LASTEXITCODE -ne 0) { throw "Docker Compose configuration is invalid." }

Write-Host "Pulling runtime images and building the API and Web images..."
& docker compose @ComposeArgs pull --ignore-buildable
if ($LASTEXITCODE -ne 0) { throw "Could not pull one or more runtime images." }
& docker compose @ComposeArgs build --pull api web
if ($LASTEXITCODE -ne 0) { throw "API/Web Docker build failed. Inspect local logs." }

Write-Host "Starting CompileCare and all observability services..."
& docker compose @ComposeArgs up -d --remove-orphans
if ($LASTEXITCODE -ne 0) { throw "Docker Compose failed. Inspect local logs." }

if (Get-Command py -ErrorAction SilentlyContinue) {
  $Python = "py"
  $PythonPrefix = @("-3")
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
  $Python = "python"
  $PythonPrefix = @()
} else {
  throw "Python 3 is required for local Keycloak and Kibana configuration. Install Python 3 and retry."
}

& $Python @PythonPrefix (Join-Path $Local "scripts/configure-keycloak.py") $EnvFile --base-url http://keycloak.localhost:8082
if ($LASTEXITCODE -ne 0) { throw "Could not reconcile local Keycloak OIDC scopes. Check the admin credentials and Keycloak logs." }

Write-Host "Waiting for all nine services and their local endpoints..."
$Ready = $false
for ($i = 0; $i -lt 180; $i++) {
  try {
    Invoke-WebRequest http://localhost:8081/ -UseBasicParsing -TimeoutSec 3 | Out-Null
    Invoke-WebRequest http://localhost:8080/ -UseBasicParsing -TimeoutSec 3 | Out-Null
    Invoke-WebRequest http://keycloak.localhost:8082/realms/compilecare/.well-known/openid-configuration -UseBasicParsing -TimeoutSec 3 | Out-Null
    Invoke-WebRequest http://localhost:9090/-/ready -UseBasicParsing -TimeoutSec 3 | Out-Null
    Invoke-WebRequest http://localhost:16686/ -UseBasicParsing -TimeoutSec 3 | Out-Null
    Invoke-WebRequest http://localhost:9200/_cluster/health -UseBasicParsing -TimeoutSec 3 | Out-Null
    Invoke-WebRequest http://localhost:8889/metrics -UseBasicParsing -TimeoutSec 3 | Out-Null
    Invoke-WebRequest http://localhost:5601/api/status -UseBasicParsing -TimeoutSec 3 | Out-Null
    $Ready = $true
    break
  } catch {
    Start-Sleep -Seconds 2
  }
}
if (-not $Ready) {
  & docker compose @ComposeArgs ps
  & docker compose @ComposeArgs logs --no-color --tail=150
  throw "Timed out waiting for the complete CompileCare stack. Inspect the logs above."
}

& docker compose @ComposeArgs exec -T postgres pg_isready -U compilecare -d compilecare
if ($LASTEXITCODE -ne 0) { throw "PostgreSQL is not accepting connections." }

Invoke-WebRequest http://localhost:8081/openapi/v1.json -UseBasicParsing -TimeoutSec 10 | Out-Null
Invoke-WebRequest http://localhost:8081/metrics -UseBasicParsing -TimeoutSec 10 | Out-Null

Write-Host "Provisioning the CompileCare Logs Kibana data view..."
& $Python @PythonPrefix (Join-Path $Local "scripts/provision-kibana-data-view.py") --base-url http://localhost:5601
if ($LASTEXITCODE -ne 0) { throw "Could not provision the Kibana data view." }

Write-Host ""
Write-Host "CompileCare is ready with the full nine-service observability stack."
Write-Host "  Workspace:     http://localhost:8080"
Write-Host "  API/OpenAPI:   http://localhost:8081/openapi/v1.json"
Write-Host "  Keycloak:      http://keycloak.localhost:8082"
Write-Host "  Prometheus:    http://localhost:9090"
Write-Host "  Jaeger:        http://localhost:16686"
Write-Host "  Kibana:        http://localhost:5601"
Write-Host "  Elasticsearch: http://localhost:9200"
Write-Host ""
Write-Host "Use synthetic test data only. No production patient data or credentials."
Start-Process "http://localhost:8080"
