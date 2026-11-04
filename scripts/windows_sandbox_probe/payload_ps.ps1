# Sandbox capability probe payload (PowerShell 7). Same checks as payload_py.py.
$ErrorActionPreference = 'Stop'
$defaultOut = [Console]::OutputEncoding.WebName
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$R = [ordered]@{}
function Rec([string]$name, [scriptblock]$fn) {
    try { $R[$name] = [ordered]@{ ok = $true; value = (& $fn) } }
    catch { $R[$name] = [ordered]@{ ok = $false; error = ($_.Exception.GetType().Name + ': ' + $_.Exception.Message) } }
}
function Connect([string]$h, [int]$p, [int]$ms) {
    $c = [System.Net.Sockets.TcpClient]::new()
    try {
        if (-not $c.ConnectAsync($h, $p).Wait($ms)) { throw 'timeout' }
        'connected'
    } finally { $c.Dispose() }
}
function WriteMarker([string]$dir) {
    $p = Join-Path $dir ("marker_ps_{0}.txt" -f $PID)
    Set-Content -LiteralPath $p -Value 'x' -Encoding utf8
    Remove-Item -LiteralPath $p -ErrorAction SilentlyContinue
    'written'
}
Rec 'version' { $PSVersionTable.PSVersion.ToString() }
Rec 'default_console_output_encoding' { $defaultOut }
Rec 'read_ro' { (Get-Content -LiteralPath (Join-Path $env:PROBE_RO 'input.txt') -Raw).Trim() }
Rec 'read_denied' { (Get-Content -LiteralPath (Join-Path $env:PROBE_DENIED 'secret.txt') -Raw).Trim() }
Rec 'list_user_documents' { @(Get-ChildItem -LiteralPath $env:PROBE_DOCS -Force).Count }
foreach ($k in 'RW_ACL', 'RW_ACL_LOW', 'LOW_ONLY', 'RO', 'DENIED', 'PARENT') {
    $dir = [Environment]::GetEnvironmentVariable("PROBE_$k")
    Rec ("write_" + $k.ToLower()) { WriteMarker $dir }
}
Rec 'module_autoload_get_filehash' { (Get-FileHash -LiteralPath (Join-Path $env:PROBE_RO 'input.txt') -Algorithm SHA256).Hash.Substring(0, 12) }
Rec 'cim_os' { (Get-CimInstance Win32_OperatingSystem).Caption }
Rec 'net_dns' { ([System.Net.Dns]::GetHostAddresses('api.github.com') | Select-Object -First 1).IPAddressToString }
Rec 'net_tcp_ip' { Connect '223.5.5.5' 53 5000 }
Rec 'net_tcp_host' { Connect 'api.github.com' 443 5000 }
Rec 'net_loopback' { Connect '127.0.0.1' ([int]$env:PROBE_LOOPBACK_PORT) 3000 }
Rec 'child_python' {
    $code = 'import sys; sys.path.insert(0, sys.argv[1]); import json, payload_py as p; print(json.dumps(p.token_facts()))'
    $out = & $env:PROBE_PY -c $code $env:PROBE_RO 2>&1
    "rc=$LASTEXITCODE " + (($out | Out-String).Trim())
}
Rec 'child_native_exe' { $o = & "$env:SystemRoot\System32\cmd.exe" /c 'echo native-ok'; "rc=$LASTEXITCODE $o" }
[Console]::Out.WriteLine(($R | ConvertTo-Json -Depth 5 -Compress))
