$ErrorActionPreference = "Stop"

$expected = @{
    "src/api/main.py" = "8dec78f0180cac8a9d6051e425c1691ce8ccccb4c41d2628e3680cb32ea71e49"
    "src/api/ai/router.py" = "48ee2bb16f467ccbce5357de8feef8227a174e1dcbad4b04a272a1ad2a4ba465"
    "src/infrastructure/llm_service.py" = "8a8124d678c8ef406457ae7bf28852af0f126bded4afb6c801278e7fca08a501"
    "src/infrastructure/dicom_service.py" = "46054216f88bb3a5f21425be31eeae628fbf5b08cbbd4a3b16b734393bd383d8"
    "src/infrastructure/pdf_service.py" = "cd9357ac95a5475189ba689f626cc7d64a7ed93992b9c031c67d44e434f299b2"
    "src/core/medical_logic.py" = "b42507c7fc55764ad1cd40cf204a0bd7fdbdd863a98abd15897c344e71c77c6e"
    "app/ai/prompt_orchestrator.py" = "f617d379e8f69fb2c03982c4acbb5f26dd0c9c1c3d8d1460f0fa1c361a56022d"
    "app/ai/quality_gate.py" = "d2ada8a63459ed87c4aad2a4d3420b19296bf092658da45d09b5df4c5066b3cc"
    "app/ai/auditor.py" = "4dfc82b45240284afe71e5c9f5c55223fcc5e96c6b25c0c070d23a69d1f5d822"
    "app/ai/clinical_audit_engine.py" = "1a3a7922f64bc31d81a4d24d0871cf1b568f03310d8e46a95cb3497b0990275f"
    "app/ai/attention_booster.py" = "88430d4c8814f533aa51eeaa6c3bacdbac831047eeefaae938106b426c39aca1"
}

$ok = $true

foreach ($path in ($expected.Keys | Sort-Object)) {
    if (-not (Test-Path $path)) {
        Write-Error "MISSING $path"
        $ok = $false
        continue
    }

    $actual = (Get-FileHash -Algorithm SHA256 $path).Hash.ToLowerInvariant()
    if ($actual -ne $expected[$path]) {
        Write-Error "MISMATCH $path expected=$($expected[$path]) actual=$actual"
        $ok = $false
    } else {
        Write-Host "OK $actual $path"
    }
}

if (-not $ok) {
    exit 1
}

Write-Host "Baseline hashes match captured production runtime."
