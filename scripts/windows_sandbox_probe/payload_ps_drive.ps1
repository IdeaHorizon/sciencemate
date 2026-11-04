# PowerShell provider operations inside an AppContainer when the run root is a drive root (subst).
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$root = $env:PROBE_DRIVE_ROOT   # e.g. "Q:\"
$R = [ordered]@{ cwd_at_start = (Get-Location).Path }
function Rec([string]$name, [scriptblock]$fn) {
    try { $R[$name] = [ordered]@{ ok = $true; value = (& $fn) } }
    catch { $e = $_.Exception; while ($e.InnerException) { $e = $e.InnerException }
            $R[$name] = [ordered]@{ ok = $false; error = ($e.GetType().Name + ': ' + $e.Message) } }
}
Rec 'Set-Location_root' { Set-Location -LiteralPath $root; (Get-Location).Path }
Rec 'relative_Set-Content' { Set-Content -LiteralPath 'r1.txt' -Value 'x'; 'ok' }
Rec 'relative_redirect' { 'y' > 'r2.txt'; 'ok' }
Rec 'mkdir_and_cd' { $null = New-Item -ItemType Directory -Path 'sub' -Force; Set-Location sub; (Get-Location).Path }
Rec 'dotdot_relative_write' { Set-Content -LiteralPath '..\r3.txt' -Value 'z'; Set-Location ..; 'ok' }
Rec 'Test-Path_root' { Test-Path -LiteralPath $root }
Rec 'Test-Path_relative' { Test-Path -LiteralPath 'r1.txt' }
Rec 'Resolve-Path' { (Resolve-Path 'r1.txt').Path }
Rec 'Get-Item_dot' { (Get-Item .).FullName }
Rec 'Rename-Item' { Rename-Item -LiteralPath 'r2.txt' -NewName 'r2b.txt'; 'ok' }
Rec 'Move-Item' { Move-Item -LiteralPath 'r2b.txt' -Destination 'sub\r2c.txt'; 'ok' }
Rec 'Remove-Item_file' { Remove-Item -LiteralPath 'r1.txt'; 'ok' }
Rec 'Remove-Item_dir_recurse' { Remove-Item -LiteralPath 'sub' -Recurse; 'ok' }
Rec 'Get-ChildItem_recurse' { @(Get-ChildItem -Recurse).Count }
Rec 'native_cmd_del' { & "$env:SystemRoot\System32\cmd.exe" /c 'echo q> r4.txt && del r4.txt && echo deleted'; "rc=$LASTEXITCODE" }
Rec 'python_relative' { & $env:PROBE_PY -c "import os; open('r5.txt','w').write('x'); os.remove('r5.txt'); print(os.getcwd())" }
Rec 'escape_attempt_outside_drive' { Set-Content -LiteralPath $env:PROBE_ESCAPE -Value 'x'; 'WROTE OUTSIDE' }
[Console]::Out.WriteLine(($R | ConvertTo-Json -Depth 5 -Compress))
