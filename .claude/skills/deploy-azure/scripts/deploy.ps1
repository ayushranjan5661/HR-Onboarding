<#
.SYNOPSIS
  Deploy HR Onboarding to the free (F1) Azure App Service plan.

.PARAMETER Target
  all (default) | backend | frontend

.PARAMETER SkipSettings
  Do not re-push .env to app settings. Use for a code-only redeploy.

.EXAMPLE
  .\deploy.ps1
  .\deploy.ps1 -Target backend
  .\deploy.ps1 -Target frontend -SkipSettings
#>
[CmdletBinding()]
param(
    [ValidateSet("all", "backend", "frontend")]
    [string]$Target = "all",
    [switch]$SkipSettings,
    [string]$ResourceGroup = "HR-Onboarding",
    [string]$ApiApp        = "HR-Onboarding-Portal-API",
    [string]$WebApp        = "HR-Onboarding-Portal"
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
    try   { $azOut = & az @Arguments 2>&1 }
    finally { $ErrorActionPreference = $prev }
    if ($LASTEXITCODE -ne 0) {
        # Surface what az actually said -- swallowing it turns every failure
        # into an unhelpful "exit 1".
        $azOut | Where-Object { $_ -notmatch "UserWarning|cryptography" } |
            ForEach-Object { Write-Host "    az: $_" -ForegroundColor DarkYellow }
        throw "az $($Arguments[0..2] -join ' ') failed (exit $LASTEXITCODE)"
    }
}

function Invoke-AzTolerant {
    <# Same stderr handling, but returns az's exit code instead of throwing.
       For calls whose exit code is not a reliable verdict -- see Publish-Zip. #>
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try   { $azOut = & az @Arguments 2>&1 }
    finally { $ErrorActionPreference = $prev }
    return [pscustomobject]@{
        ExitCode = $LASTEXITCODE
        Output   = ($azOut | Where-Object { $_ -notmatch "UserWarning|cryptography" })
    }
}

$script:ScmToken = $null
function Get-ScmToken {
    <# Kudu accepts an ARM bearer token. These apps have SCM basic auth
       disabled by tenant policy, so this is the only way in. #>
    if ($script:ScmToken) { return $script:ScmToken }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $script:ScmToken = (& az account get-access-token `
                               --resource "https://management.azure.com/" `
                               --query accessToken -o tsv 2>$null | Select-Object -First 1)
    } finally { $ErrorActionPreference = $prev }
    if (-not $script:ScmToken) { throw "could not get an ARM access token; is 'az login' current?" }
    return $script:ScmToken
}

function Get-KuduJson {
    param([string]$AppName, [string]$Path)
    $uri = "https://$($AppName.ToLower()).scm.azurewebsites.net$Path"
    $r = Invoke-WebRequest -Uri $uri -Headers @{ Authorization = "Bearer $(Get-ScmToken)" } `
                           -UseBasicParsing -TimeoutSec 60
    return ($r.Content | ConvertFrom-Json)
}

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..\..")).Path
$Staging  = Join-Path $env:TEMP "hr-onboarding-deploy"

# The backend is NOT deployed as the contents of backend/. app/services/zoho_push.py
# resolves integrations/zoho relative to the repo root (three levels up from
# itself), so wwwroot has to mirror the repo layout:
#
#   /home/site/wwwroot/
#     backend/...          <- the FastAPI app (incl. tessdata/ language packs)
#     integrations/zoho/   <- imported by app/services/zoho_push.py
#     requirements.txt     <- copy; Oryx only looks at the deployment root
#     startup.sh           <- installs tesseract, then execs uvicorn
#
# --app-dir puts <approot>/backend on sys.path so "app.main" resolves from there,
# while zoho_push's ../../.. still lands on <approot>. Deploying only backend/'s
# contents makes hr.py's module-level `from app.services import zoho_push`
# raise ImportError and the container never starts.
#
# --app-dir MUST stay relative. Oryx builds this app with
# CompressDestinationDir=true (see wwwroot/oryx-manifest.toml): wwwroot holds a
# single output.tar.zst, which the platform extracts to a temp directory and
# runs from there. /home/site/wwwroot/backend therefore does not exist at
# runtime -- an absolute --app-dir exits 1 during startup with nothing useful
# in the platform log. A relative path resolves against the extracted app root,
# which is the startup command's working directory. The same applies to
# startup.sh below -- relative, for the same reason.
#
# uvicorn is launched by startup.sh rather than directly, because the built-in
# Python image has no Tesseract and doc_validator.py shells out to it. See that
# script for why the install happens at container start and what it costs.
$StartupCommand = "bash startup.sh"

function Copy-Staged {
    <# robocopy one source tree into the staging layout, minus the noise. #>
    param(
        [string]$SourceDir,
        [string]$DestDir,
        [string[]]$ExcludeDirs  = @(),
        [string[]]$ExcludeFiles = @()
    )
    New-Item -ItemType Directory -Path $DestDir -Force | Out-Null

    # robocopy rather than Copy-Item: it takes exclusion lists directly, and
    # skipping venv/ is the difference between a 2 MB zip and a 200 MB one.
    $rcArgs = @($SourceDir, $DestDir, "/E", "/NFL", "/NDL", "/NJH", "/NJS", "/NP")
    if ($ExcludeDirs)  { $rcArgs += "/XD"; $rcArgs += $ExcludeDirs }
    if ($ExcludeFiles) { $rcArgs += "/XF"; $rcArgs += $ExcludeFiles }
    robocopy @rcArgs | Out-Null
    # robocopy uses 0-7 for success; 8+ is a real failure.
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed copying $SourceDir (exit $LASTEXITCODE)" }
    $global:LASTEXITCODE = 0
}

function New-StageDir {
    param([string]$Name)
    $stage = Join-Path $Staging $Name
    if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
    New-Item -ItemType Directory -Path $stage -Force | Out-Null
    return $stage
}

function New-ZipFromDir {
    param([string]$StageDir, [string]$ZipPath)

    if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }

    # Entries are written by hand with forward slashes on purpose.
    #
    # The ZIP spec requires '/' as the path separator. On Windows PowerShell 5.1
    # BOTH Compress-Archive and [ZipFile]::CreateFromDirectory emit '\' instead,
    # because .NET Framework uses the host separator. Azure accepts the upload
    # and then fails server-side in rsync with
    #   failed to stat "/home/site/wwwroot/hr-portal\dashboard.html": Invalid argument (22)
    # which surfaces to the CLI as an unexplained HTTP 400 with an empty body.
    # Nothing about the error points at the zip, so this is worth not undoing.
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem

    $fs = [IO.File]::Open($ZipPath, [IO.FileMode]::Create)
    try {
        $zip = New-Object IO.Compression.ZipArchive($fs, [IO.Compression.ZipArchiveMode]::Create)
        # Relative paths come from Resolve-Path -Relative, NOT from trimming the
        # staging path off FullName by length. $env:TEMP is the 8.3 short form
        # (C:\Users\INDIRA~1\...) while Get-ChildItem returns the long form, so a
        # length-based Substring silently eats the wrong number of characters and
        # every entry lands under a truncated directory -- "frontend" became "tend".
        # The deploy still succeeds; the site just 404s on everything but index.html.
        Push-Location $StageDir
        try {
            foreach ($f in Get-ChildItem -Recurse -File -Force) {
                # Strip exactly the leading ".\" that -Relative prepends. NOT
                # TrimStart('.','\'), which also eats the dot of a dotfile and
                # turns .env.example into env.example.
                $rel = ((Resolve-Path -LiteralPath $f.FullName -Relative) `
                           -replace '^\.\\', '').Replace('\', '/')
                $entry = $zip.CreateEntry($rel, [IO.Compression.CompressionLevel]::Optimal)
                $out   = $entry.Open()
                $in    = [IO.File]::OpenRead($f.FullName)
                try { $in.CopyTo($out) } finally { $in.Dispose(); $out.Dispose() }
            }
        } finally { Pop-Location; $zip.Dispose() }
    } finally { $fs.Dispose() }

    # Cheap guard against the packaging bugs above silently coming back: a
    # backslash in any entry name, or a top-level entry that is not actually in
    # the staged tree.
    $check = [IO.Compression.ZipFile]::OpenRead($ZipPath)
    try {
        $bad = $check.Entries | Where-Object { $_.FullName -match '\\' } | Select-Object -First 1
        if ($bad) { throw "zip entry '$($bad.FullName)' uses a backslash; Azure's rsync will reject it" }

        $roots  = $check.Entries | ForEach-Object { ($_.FullName -split '/')[0] } | Sort-Object -Unique
        # -Force so dotfiles (.env.example) count as legitimate top-level entries.
        $actual = Get-ChildItem -Path $StageDir -Force | Select-Object -ExpandProperty Name
        $stray  = $roots | Where-Object { $_ -notin $actual }
        if ($stray) { throw "zip has unexpected top-level entries: $($stray -join ', ') -- relative paths were computed wrong" }
    } finally { $check.Dispose() }

    $mb = [math]::Round((Get-Item $ZipPath).Length / 1MB, 2)
    Write-Host "  packaged $ZipPath ($mb MB, top level: $($roots -join ', '))"
    return $ZipPath
}

function Publish-Zip {
    <#
      `az webapp deploy`'s exit code is NOT a reliable verdict for this app.
      It has reported failure on four deploys that completely succeeded:

        * "failed because the site failed to start within 10 mins" x3 -- the
          CLI stops tracking after 10 minutes, but a cold Oryx build plus
          pip install plus the apt-get in startup.sh routinely runs longer.
          The platform log then shows "Site started" minutes later.
        * "504.0 GatewayTimeout" -- the HTTP request to Kudu timed out while
          Kudu carried on building server-side.

      Each false failure aborted the script before it set the startup command
      or deployed the frontend, leaving a half-finished deploy to be patched
      up by hand. So: run az, ignore its exit code, and ask Kudu what actually
      happened. Kudu's /api/deployments/latest is the authority.
    #>
    param([string]$AppName, [string]$ZipPath)

    # Kudu stamps deployments in UTC; take the mark before uploading so a
    # stale previous deployment is never mistaken for this one.
    $since = (Get-Date).ToUniversalTime().AddSeconds(-5)

    Write-Host "  uploading to $AppName ..."
    # --clean wipes wwwroot first. Without it a deploy is an incremental rsync,
    # so files left by an earlier bad push stay behind and keep getting served.
    $az = Invoke-AzTolerant webapp deploy --resource-group $ResourceGroup --name $AppName `
                                          --src-path $ZipPath --type zip --clean true --output none
    if ($az.ExitCode -ne 0) {
        Write-Host "  az reported exit $($az.ExitCode); confirming with Kudu before believing it" -ForegroundColor DarkYellow
    }

    Wait-KuduDeployment -AppName $AppName -Since $since
}

function Wait-KuduDeployment {
    <# Kudu DeployStatus: 0 Pending, 1 Building, 2 Deploying, 3 Failed, 4 Success. #>
    param([string]$AppName, [datetime]$Since, [int]$TimeoutSec = 1800)

    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $lastSeen = ""
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 15
        try { $d = Get-KuduJson -AppName $AppName -Path "/api/deployments/latest" }
        catch {
            # Kudu bounces during a deploy; that is not a failure on its own.
            continue
        }

        # Ignore a deployment that predates this upload.
        $received = $null
        if ($d.received_time) { $received = ([datetime]$d.received_time).ToUniversalTime() }
        if ($received -and $received -lt $Since) { continue }

        $state = "$($d.status)/$($d.complete)"
        if ($state -ne $lastSeen) {
            Write-Host "    kudu: status=$($d.status) complete=$($d.complete)"
            $lastSeen = $state
        }

        if ($d.complete -eq $true) {
            if ($d.status -eq 4) { Write-Host "  deployment succeeded" -ForegroundColor Green; return }
            $msg = ($d.status_text, $d.message | Where-Object { $_ } | Select-Object -First 1)
            throw "deployment failed on $AppName (kudu status $($d.status)): $msg"
        }
    }
    throw "deployment on $AppName did not complete within ${TimeoutSec}s"
}

function Wait-HttpOk {
    <# Confirm the site actually serves. On F1 a cold start after a deploy can
       take minutes -- the backend also apt-gets tesseract before uvicorn even
       begins -- so this is generous, and a miss is reported rather than fatal:
       the deploy itself already succeeded by then. #>
    param([string]$Url, [int]$TimeoutSec = 600)

    Write-Host "  waiting for $Url ..."
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 60
            if ($r.StatusCode -eq 200) {
                Write-Host "  serving 200" -ForegroundColor Green
                return $true
            }
        } catch { }
        Start-Sleep -Seconds 15
    }
    Write-Host "  WARNING: no 200 within ${TimeoutSec}s. The deploy succeeded; the app is not up." -ForegroundColor DarkYellow
    Write-Host "           az webapp log tail -g $ResourceGroup -n <app>" -ForegroundColor DarkYellow
    return $false
}

function Wait-ScmSettled {
    <#
      Applying app settings restarts the app AND its SCM (Kudu) container.
      Starting a zip deploy before that settles gets the deployment killed
      half-way with:
        "Deployment has been stopped due to SCM container restart. ...
         Do not perform a management operation and a deployment operation
         in quick succession."
      Kudu reports the deploy as failed, but the app is left on the previous
      version, so this is a wasted ~10 minutes rather than an outage.
    #>
    param([string]$AppName, [int]$TimeoutSec = 180)

    $scm = "https://$($AppName.ToLower()).scm.azurewebsites.net/api/deployments"
    $prev = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $token = (& az account get-access-token --resource "https://management.azure.com/" `
                     --query accessToken -o tsv 2>$null | Select-Object -First 1)
    } finally { $ErrorActionPreference = $prev }

    Write-Host "  waiting for SCM to settle after the restart ..."
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 10
        try {
            $r = Invoke-WebRequest -Uri $scm -Headers @{ Authorization = "Bearer $token" } `
                                   -UseBasicParsing -TimeoutSec 30
            if ($r.StatusCode -eq 200) {
                # Responding is necessary but not sufficient -- give the
                # restart a moment to finish on the app side too.
                Start-Sleep -Seconds 20
                Write-Host "  SCM ready"
                return
            }
        } catch { }
    }
    Write-Host "  WARNING: SCM did not respond within ${TimeoutSec}s; deploying anyway" -ForegroundColor DarkYellow
}

Write-Host "Repo: $RepoRoot"
Write-Host "Target: $Target`n"

# --- app settings -------------------------------------------------------------
# Must run before the backend zip push: Oryx only runs pip install when
# SCM_DO_BUILD_DURING_DEPLOYMENT is already true at upload time.
if (-not $SkipSettings -and $Target -in @("all", "backend")) {
    Write-Host "== app settings ==" -ForegroundColor Cyan
    & (Join-Path $PSScriptRoot "sync-settings.ps1") -ResourceGroup $ResourceGroup -ApiApp $ApiApp
    Wait-ScmSettled -AppName $ApiApp
    Write-Host ""
}

# --- backend ------------------------------------------------------------------
if ($Target -in @("all", "backend")) {
    Write-Host "== backend ==" -ForegroundColor Cyan

    $stage = New-StageDir "backend-stage"
    Copy-Staged -SourceDir (Join-Path $RepoRoot "backend") `
                -DestDir   (Join-Path $stage "backend") `
                -ExcludeDirs  @("venv", "myenv", ".venv", "__pycache__", "uploads", ".git") `
                -ExcludeFiles @(".env", "*.pyc", "*.log")
    Copy-Staged -SourceDir (Join-Path $RepoRoot "integrations") `
                -DestDir   (Join-Path $stage "integrations") `
                -ExcludeDirs  @("__pycache__", "out", ".git") `
                -ExcludeFiles @("*.pyc", "*.log")
    # Oryx only pip installs from a requirements.txt at the deployment root.
    Copy-Item (Join-Path $RepoRoot "backend\requirements.txt") `
              (Join-Path $stage "requirements.txt") -Force

    # startup.sh lives with this skill, not in the repo: installing an OS
    # package at boot is a deployment concern of this free-tier plan, not
    # something the application itself should carry.
    $startupSrc = Join-Path $PSScriptRoot "startup.sh"
    Copy-Item $startupSrc (Join-Path $stage "startup.sh") -Force
    # A CRLF here makes the shebang line `#!/usr/bin/env bash\r`, and the
    # container fails to start with a bare "no such file or directory" that
    # names a binary which plainly exists. Normalise rather than trust git.
    $sh = [IO.File]::ReadAllBytes((Join-Path $stage "startup.sh"))
    $lf = [byte[]]($sh | Where-Object { $_ -ne 13 })
    [IO.File]::WriteAllBytes((Join-Path $stage "startup.sh"), $lf)

    # Code first, startup command second. Setting the uvicorn command on an app
    # that has no code yet makes the container crash-loop, and every restart
    # counts against the plan's WPStopRequests quota (15 on Free). Blowing that
    # puts the whole PLAN into QuotaExceeded -- both apps start returning 403
    # "This web app is stopped" until the quota window resets.
    $zip = New-ZipFromDir -StageDir $stage -ZipPath (Join-Path $Staging "backend.zip")
    Publish-Zip -AppName $ApiApp -ZipPath $zip

    Invoke-Az webapp config set --resource-group $ResourceGroup --name $ApiApp `
                                --startup-file $StartupCommand --output none
    Write-Host "  startup: $StartupCommand"

    # The app is left stopped by a previous failed run, or was never started.
    Invoke-Az webapp start --resource-group $ResourceGroup --name $ApiApp --output none

    Write-Host "  https://$($ApiApp.ToLower()).azurewebsites.net" -ForegroundColor Green
    [void](Wait-HttpOk -Url "https://$($ApiApp.ToLower()).azurewebsites.net/docs")
    Write-Host ""
}

# --- frontend -----------------------------------------------------------------
if ($Target -in @("all", "frontend")) {
    Write-Host "== frontend ==" -ForegroundColor Cyan

    $stage = New-StageDir "frontend-stage"
    Copy-Staged -SourceDir (Join-Path $RepoRoot "frontend") `
                -DestDir   $stage `
                -ExcludeDirs  @(".git", "node_modules") `
                -ExcludeFiles @(".env", "*.log")

    $zip = New-ZipFromDir -StageDir $stage -ZipPath (Join-Path $Staging "frontend.zip")
    Publish-Zip -AppName $WebApp -ZipPath $zip
    Write-Host "  https://$($WebApp.ToLower()).azurewebsites.net" -ForegroundColor Green
    [void](Wait-HttpOk -Url "https://$($WebApp.ToLower()).azurewebsites.net/index.html" -TimeoutSec 180)
    Write-Host ""
}

Write-Host "Done. First request after a deploy is slow -- F1 has no Always On." -ForegroundColor Green
