<#
.SYNOPSIS
  Push the root .env into the backend web app's Azure application settings.

.DESCRIPTION
  backend/app/config.py reads the root .env locally; on Azure that file does
  not exist, so pydantic-settings falls back to environment variables. App
  Service application settings ARE those environment variables, so this script
  is what makes the deployed backend see the same configuration as your machine.

  Values are read straight from .env and handed to az. Nothing is printed --
  only key names and a redacted length are echoed.

  Three keys are overridden rather than copied, because the local values point
  at localhost or at a directory that does not survive a deploy. See SKILL.md.
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup = "HR-Onboarding",
    [string]$ApiApp        = "HR-Onboarding-Portal-API",
    [string]$FrontendUrl   = "https://hr-onboarding-portal.azurewebsites.net",
    [string]$EnvFile       = (Join-Path $PSScriptRoot "..\..\..\..\.env")
)

$ErrorActionPreference = "Stop"

# Windows PowerShell 5.1 wraps anything a native command writes to stderr in an
# ErrorRecord, and az emits a harmless "32-bit Python" warning on every single
# call. Under -ErrorActionPreference Stop that aborts the script even though az
# succeeded, so native calls run with it relaxed and are judged on $LASTEXITCODE.
function Invoke-Az {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try   { & az @Arguments 2>&1 | Out-Null }
    finally { $ErrorActionPreference = $prev }
    if ($LASTEXITCODE -ne 0) { throw "az $($Arguments[0..2] -join ' ') failed (exit $LASTEXITCODE)" }
}

$EnvFile = (Resolve-Path $EnvFile).Path
Write-Host "Reading $EnvFile"

# Parse .env: KEY=VALUE, skipping blanks and # comments. Surrounding quotes are
# stripped -- .env keeps them on some values, but App Service would store them
# as part of the value and the URL would then be malformed.
$settings = [ordered]@{}
foreach ($line in Get-Content $EnvFile) {
    $t = $line.Trim()
    if ($t -eq "" -or $t.StartsWith("#")) { continue }
    $i = $t.IndexOf("=")
    if ($i -lt 1) { continue }
    $key = $t.Substring(0, $i).Trim()
    $val = $t.Substring($i + 1).Trim()
    if ($val.Length -ge 2 -and
        (($val.StartsWith('"') -and $val.EndsWith('"')) -or
         ($val.StartsWith("'") -and $val.EndsWith("'")))) {
        $val = $val.Substring(1, $val.Length - 2)
    }
    $settings[$key] = $val
}

# --- Azure-specific overrides -------------------------------------------------
# DATABASE_URL is NEVER taken from .env. Azure points at Supabase; the root
# .env points at whatever local Postgres the developer is using. Pushing the
# local value would leave the live API dialling 127.0.0.1 inside its own
# container and failing every request -- which is exactly what nearly shipped
# once. The value already on the app is the source of truth; .env is only a
# fallback for a brand-new app that has none yet.
# Same stderr trap as Invoke-Az, but this one needs stdout back, so it relaxes
# the preference inline instead of going through the helper.
$prev = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    $liveDbUrl = (& az webapp config appsettings list --resource-group $ResourceGroup `
                     --name $ApiApp --query "[?name=='DATABASE_URL'].value | [0]" -o tsv 2>$null |
                  Select-Object -First 1)
} finally { $ErrorActionPreference = $prev }
if ($liveDbUrl) { $liveDbUrl = $liveDbUrl.Trim() }

if ($liveDbUrl) {
    $settings["DATABASE_URL"] = $liveDbUrl
    Write-Host "  DATABASE_URL: keeping the value already set on $ApiApp" -ForegroundColor DarkGray
} elseif ($settings["DATABASE_URL"] -match "127\.0\.0\.1|localhost") {
    throw ("$ApiApp has no DATABASE_URL yet and .env points at localhost. " +
           "Set the real one on the app first: az webapp config appsettings set " +
           "-g $ResourceGroup -n $ApiApp --settings DATABASE_URL='<url>'")
}

# CORS and invite links must name the deployed frontend, not localhost.
$settings["FRONTEND_ORIGINS"] = $FrontendUrl
# The referee form link is emailed to a candidate's references -- people
# outside the company, on their own machines. Left at the config default it
# would send them http://127.0.0.1:5500/..., which opens nothing. The %20 is
# the space in the "referee form-portal" directory name and must stay encoded.
$settings["REFEREE_FORM_URL"] = "$FrontendUrl/referee%20form-portal/referee-form.html"
$settings["PORTAL_BASE_URL"]  = $FrontendUrl
# /home is the only writable path that survives a deploy; wwwroot is replaced.
$settings["UPLOAD_DIR"]       = "/home/uploads"
# .env carries the Windows install path for local dev. On Linux that file does
# not exist, so doc_validator falls back to shutil.which() and would still
# work -- but pinning it to where apt puts the binary keeps the app settings
# honest about what the container actually runs.
$settings["TESSERACT_CMD"]    = "/usr/bin/tesseract"
# Without this, Oryx skips pip install and the container starts with no deps.
$settings["SCM_DO_BUILD_DURING_DEPLOYMENT"] = "true"
$settings["ENABLE_ORYX_BUILD"]              = "true"

if (-not $settings["JWT_SECRET_KEY"] -or $settings["JWT_SECRET_KEY"] -eq "change-me-in-env") {
    throw "JWT_SECRET_KEY is missing or still the placeholder in $EnvFile. The backend refuses to start without a real one."
}

Write-Host "`nPushing $($settings.Count) settings to $ApiApp :"
foreach ($k in $settings.Keys) {
    $len = $settings[$k].Length
    Write-Host ("  {0,-38} ({1} chars)" -f $k, $len)
}

# az takes KEY=VALUE pairs; pass them as an argument array so PowerShell does
# not re-split values containing spaces or special characters.
$pairs = @()
foreach ($k in $settings.Keys) { $pairs += "$k=$($settings[$k])" }

Invoke-Az webapp config appsettings set `
    --resource-group $ResourceGroup `
    --name $ApiApp `
    --settings @pairs `
    --output none

Write-Host "`nSettings applied. The app restarts automatically." -ForegroundColor Green
