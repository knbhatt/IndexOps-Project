# One-click demo reset for Intelligent IndexOps (Windows PowerShell).
# Usage (from project root):
#   .\demo.ps1                                    # random Count + InjectPct (general testing)
#   .\demo.ps1 -Count 2500 -InjectPct 0.2         # FIXED values for demo day
#   .\demo.ps1 -Count 2000 -InjectPct 0 -InjectType none -SkipInvestigate  # clean baseline
#   .\demo.ps1 -SkipInvestigate

param(
    [ValidateSet("category_mapping_drift", "missing_fields", "duplicate_tickets", "random", "none")]
    [string]$InjectType = "category_mapping_drift",
    # Omit (or pass -1) → random. Pass an explicit value (including 0) → fixed for demo day.
    [double]$InjectPct = -1,
    # -Count is the preferred override alias; -TicketCount kept for compatibility.
    [Alias("Count")]
    [int]$TicketCount = -1,
    [switch]$ReseedTickets,
    [switch]$SkipInvestigate
)

# Detect explicit overrides so demo-day pins are obvious in the log.
# (-Count is an alias of -TicketCount; BoundParameters stores the canonical name.)
$countPinned = $PSBoundParameters.ContainsKey("TicketCount")
$pctPinned = $PSBoundParameters.ContainsKey("InjectPct")

if (-not $countPinned) {
    $TicketCount = Get-Random -Minimum 1500 -Maximum 4201
    Write-Host ("Random -Count = " + $TicketCount) -ForegroundColor DarkYellow
} else {
    if ($TicketCount -lt 1) { throw "-Count/-TicketCount must be >= 1 when pinned" }
    Write-Host ("Fixed -Count = " + $TicketCount) -ForegroundColor Green
}

if (-not $pctPinned) {
    # 9%–31% in 1% steps — always above the 5% alert threshold
    $InjectPct = [math]::Round((Get-Random -Minimum 9 -Maximum 32) / 100.0, 2)
    Write-Host ("Random -InjectPct = " + $InjectPct) -ForegroundColor DarkYellow
} else {
    if ($InjectPct -lt 0) { throw "-InjectPct must be >= 0 when pinned (use 0 for a clean run)" }
    Write-Host ("Fixed -InjectPct = " + $InjectPct) -ForegroundColor Green
}

if ($InjectType -eq "random") {
    $InjectType = @("category_mapping_drift", "missing_fields", "duplicate_tickets")[(Get-Random -Maximum 3)]
    Write-Host ("Random InjectType = " + $InjectType) -ForegroundColor DarkYellow
} elseif ($InjectType -eq "none") {
    $InjectPct = 0
    Write-Host "InjectType=none -> forcing InjectPct=0 (clean run)" -ForegroundColor Green
}

# Continue: native tools (docker/psql) write progress to stderr; Stop would abort on those.
$ErrorActionPreference = "Continue"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

function Write-Step([string]$msg) {
    Write-Host ""
    Write-Host ("==> " + $msg) -ForegroundColor Cyan
}

function Assert-Ok([string]$what) {
    if ($LASTEXITCODE -ne 0) { throw ($what + " failed (exit " + $LASTEXITCODE + ")") }
}

function Wait-PostgresReady([int]$TimeoutSec = 180) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    do {
        Start-Sleep 5
        $ready = $false
        docker exec indexops-postgres pg_isready -U airflow 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) {
            $tbl = (docker exec indexops-postgres psql -U airflow -d airflow -tAc "SELECT to_regclass('public.tickets')" 2>$null | Out-String).Trim()
            if ($tbl -eq "tickets") { return }
        }
        Write-Host "  waiting for Postgres + schema..."
    } while ((Get-Date) -lt $deadline)
    throw "Postgres/schema not ready within timeout"
}

function Wait-OpenSearchReady([int]$TimeoutSec = 180) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    do {
        Start-Sleep 5
        try {
            $h = Invoke-RestMethod "http://localhost:9200/_cluster/health" -TimeoutSec 5
            if ($h.status -eq "yellow" -or $h.status -eq "green") { return }
        } catch { }
        Write-Host "  waiting for OpenSearch..."
    } while ((Get-Date) -lt $deadline)
    throw "OpenSearch not ready within timeout"
}

function Wait-AirflowReady([int]$TimeoutSec = 300) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    do {
        Start-Sleep 8
        $sched = docker inspect -f "{{.State.Status}}" indexops-airflow-scheduler 2>$null
        $init = (docker inspect -f "{{.State.Status}} {{.State.ExitCode}}" indexops-airflow-init 2>$null | Out-String).Trim()
        if ($sched -eq "running" -and $init -match "^exited 0") { return }
        if ($init -match "^exited [1-9]") { throw ("airflow-init failed: " + $init) }
        Write-Host ("  waiting for airflow-init + scheduler (" + $sched + " / " + $init + ")...")
    } while ((Get-Date) -lt $deadline)
    throw "Airflow not ready within timeout"
}

Write-Step "1/6  Checking Docker stack"
$names = @(docker ps --format "{{.Names}}")
$needStart = $false
foreach ($need in @("indexops-postgres", "indexops-opensearch", "indexops-airflow-web", "indexops-airflow-scheduler")) {
    if ($names -notcontains $need) { $needStart = $true; break }
}
if ($needStart) {
    Write-Host "Stack not fully up - starting with docker compose up -d ..." -ForegroundColor Yellow
    docker compose up -d 2>&1 | Out-Host
    Assert-Ok "docker compose up -d"
    Wait-PostgresReady
    Wait-OpenSearchReady
    Wait-AirflowReady
} else {
    Wait-PostgresReady 60
    Wait-OpenSearchReady 60
}
docker ps --format "{{.Names}}: {{.Status}}"

Write-Step "2/6  Wiping demo artifacts (alerts / incidents / steps / remediations / pipeline metrics)"
$sqlTruncate = "TRUNCATE investigation_steps, remediation_actions, incidents, alerts, data_quality_metrics, index_metrics, pipeline_runs RESTART IDENTITY CASCADE;"
docker exec indexops-postgres psql -U airflow -d airflow -v ON_ERROR_STOP=1 -c $sqlTruncate
Assert-Ok "TRUNCATE"

Write-Step ("3/6  Seeding KameronB tickets (count=" + $TicketCount + ") and knowledge_base (5)")
$existingCount = (docker exec indexops-postgres psql -U airflow -d airflow -tAc "SELECT COUNT(*) FROM tickets" | Out-String).Trim()
Write-Host ("tickets before = " + $existingCount)
Write-Host ("Reseeding KameronB (--truncate --count " + $TicketCount + ", seed=" + $TicketCount + ")...") -ForegroundColor Yellow
# Vary sampling with the count so descriptions change between demos too.
python data/generate_tickets.py --count $TicketCount --truncate --seed $TicketCount
Assert-Ok "generate_tickets.py"
$existingCount = (docker exec indexops-postgres psql -U airflow -d airflow -tAc "SELECT COUNT(*) FROM tickets" | Out-String).Trim()
Write-Host ("tickets after seed = " + $existingCount)

$kb = 0
try {
    $kb = [int](Invoke-RestMethod "http://localhost:9200/knowledge_base/_count" -TimeoutSec 10).count
} catch {
    $kb = 0
}
Write-Host ("knowledge_base = " + $kb)
if ($kb -lt 5) {
    Write-Host "Re-indexing knowledge base inside the scheduler container..." -ForegroundColor Yellow
    docker exec indexops-airflow-scheduler python /opt/airflow/data/index_knowledge_base.py
    Assert-Ok "index_knowledge_base.py"
    $kb = [int](Invoke-RestMethod "http://localhost:9200/knowledge_base/_count" -TimeoutSec 10).count
    Write-Host ("knowledge_base after index = " + $kb)
}

Write-Step ("4/6  Setting failure injection: " + $InjectType + " at " + $InjectPct)
docker exec indexops-airflow-scheduler bash -c "airflow variables set inject_failure_type $InjectType >/dev/null; airflow variables set inject_failure_pct $InjectPct >/dev/null; echo set"
Assert-Ok "Airflow Variables set"

$runId = "demo_" + (Get-Date -Format "yyyyMMdd_HHmmss")
Write-Step ("5/6  Triggering DAG run " + $runId)
docker exec indexops-airflow-scheduler bash -c "airflow dags unpause indexops_pipeline >/dev/null; airflow dags trigger indexops_pipeline -r $runId >/dev/null 2>&1; echo triggered"
Assert-Ok "DAG trigger"

$deadline = (Get-Date).AddMinutes(10)
$state = ""
do {
    Start-Sleep 8
    $state = (docker exec indexops-postgres psql -U airflow -d airflow -tAc "SELECT state FROM dag_run WHERE run_id='$runId'" | Out-String).Trim()
    Write-Host ("  dag state: " + $state)
} while ($state -ne "success" -and $state -ne "failed" -and (Get-Date) -lt $deadline)

docker exec indexops-airflow-scheduler bash -c "airflow variables set inject_failure_type none >/dev/null; airflow variables set inject_failure_pct 0 >/dev/null"

if ($state -ne "success") {
    Write-Host ("DAG did not succeed (state=" + $state + "). Aborting.") -ForegroundColor Red
    exit 1
}

$sqlAlert = "SELECT a.alert_id::text || E'|' || a.run_id::text || E'|' || a.severity || E'|' || left(a.message, 80) FROM alerts a JOIN pipeline_runs r ON r.run_id = a.run_id WHERE r.dag_run_id = '" + $runId + "' ORDER BY a.alert_id DESC LIMIT 1;"
$alertRow = (docker exec indexops-postgres psql -U airflow -d airflow -tAc $sqlAlert | Out-String).Trim()
$cleanRun = ($InjectType -eq "none") -or ([double]$InjectPct -eq 0)
$alertId = $null
if (-not $alertRow) {
    if ($cleanRun) {
        Write-Host "Clean run: DAG succeeded with no alert (expected when inject is none/0)." -ForegroundColor Green
    } else {
        Write-Host "DAG succeeded but no alert was raised - check inject settings / thresholds." -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host ("Alert: " + $alertRow) -ForegroundColor Green
    $alertId = ($alertRow -split '\|')[0]
}

Write-Step "6/6  Next steps for the live demo"
Write-Host ""
Write-Host ("  DAG run     : " + $runId + " (" + $state + ")")
if ($alertId) {
    Write-Host ("  Alert id    : " + $alertId)
} else {
    Write-Host "  Alert id    : (none - clean run)"
}
Write-Host ("  Injection   : " + $InjectType + " at " + $InjectPct + " (now reset to none/0)")
Write-Host ""
Write-Host "  Open the dashboard:"
Write-Host "      streamlit run dashboard/app.py"
Write-Host ""
if ($alertId) {
    Write-Host "  Then:"
    Write-Host "    1. Alerts page          - see the new alert"
    Write-Host "    2. Investigate page     - start investigation from that alert"
    Write-Host "    3. Incident and Steps   - watch steps stream in (~2s poll)"
    Write-Host "    4. Approve Remediation  - Approve the pending reindex; mismatch pct drops to ~0"
    Write-Host ""
}

if ((-not $SkipInvestigate) -and $alertId) {
    $ans = Read-Host ("Run the agent investigation now from alert " + $alertId + "? [y/N]")
    if ($ans -match '^[Yy]') {
        Write-Step ("Running investigation (scripts/run_investigation.py --alert " + $alertId + ")")
        python -u scripts/run_investigation.py --alert $alertId
        Write-Host ""
        Write-Host "Open Streamlit -> Approve Remediation to execute the staged fix." -ForegroundColor Green
    }
}

Write-Host ""
Write-Host "Demo ready." -ForegroundColor Green
