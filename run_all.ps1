# Ontology + analytics agent + autonomous control + sandbox: one pipeline, CI-ready.
# ASCII-only on purpose: Windows PowerShell 5.1 mis-decodes UTF-8 .ps1 without BOM.
$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
Set-Location $PSScriptRoot

$steps = @(
    @{ dir = 'scripts'; name = 'build_analytics_db.py'; desc = 'build analytics SQLite fixture (24 wells x 90d)' },
    @{ dir = 'scripts'; name = 'analytics_agent.py';    desc = 'ontology-grounded analytics agent (expect pass)' },
    @{ dir = 'scripts'; name = 'decision_agent.py';     desc = 'on/off well decision agent + gate (expect pass)' },
    @{ dir = 'scripts'; name = 'closed_loop.py';        desc = 'feedback loop: measure, downgrade, calibrate (expect pass)' },
    @{ dir = 'scripts'; name = 'control_api.py';        desc = 'autonomous control: dispatch/ack/rollback/audit (expect pass)' },
    @{ dir = 'sim';     name = 'run_production.py';     desc = 'sandbox: 3-day two-policy end-to-end comparison (expect pass)' },
    @{ dir = 'scripts'; name = 'scale_test.py';         desc = 'scale test: decision throughput at 1k/5k/10k wells' },
    @{ dir = 'scripts'; name = 'fault_predict.py';      desc = 'fault prediction: trend + step + data quality' },
    @{ dir = 'scripts'; name = 'dispatch.py';           desc = 'work orders + auto dispatch + escalation' },
    @{ dir = 'sim';     name = 'shift_demo.py';         desc = 'shift loop: predict -> dispatch -> AI report -> guardrail' },
    @{ dir = '.';       name = 'demo.py';               desc = 'build the final self-contained HTML demo' },
    @{ dir = 'scripts'; name = 'build.py';              desc = 'OWL 2 RL materialize + competency queries' },
    @{ dir = 'scripts'; name = 'validate.py';           desc = 'SHACL on product data (seeded defects, expect fail)' },
    @{ dir = 'scripts'; name = 'consistency.py';        desc = 'three-layer consistency check (expect fail)' },
    @{ dir = 'scripts'; name = 'well_sim.py';           desc = 'legacy: oilfield baseline simulation' },
    @{ dir = 'scripts'; name = 'intermittent.py';       desc = 'legacy: intermittent-production engine' },
    @{ dir = 'scripts'; name = 'well_validate.py';      desc = 'legacy: decision compliance gate (expect pass)' },
    @{ dir = 'scripts'; name = 'unmanned_audit.py';     desc = 'legacy: unmanned-readiness audit (expect findings)' },
    @{ dir = 'scripts'; name = 'l4_gate.py';            desc = 'legacy: L4 gate + fault injection (expect pass)' },
    @{ dir = 'scripts'; name = 'dataflow.py';           desc = 'legacy: latency and capacity budget (expect pass)' }
)

$fail = 0
foreach ($s in $steps) {
    Write-Host ""
    Write-Host ("=== {0}/{1}  --  {2} ===" -f $s.dir, $s.name, $s.desc) -ForegroundColor Cyan
    python "$($s.dir)\$($s.name)"
    if ($LASTEXITCODE -ne 0) {
        Write-Host ("!!! {0} exit code {1}" -f $s.name, $LASTEXITCODE) -ForegroundColor Yellow
        $fail++
    }
}

Write-Host ""
if ($fail -eq 0) { Write-Host "ALL GREEN" -ForegroundColor Green }
else { Write-Host ("{0} step(s) returned non-zero (defect fixtures and audit findings are intentional)" -f $fail) -ForegroundColor Yellow }
exit $fail
