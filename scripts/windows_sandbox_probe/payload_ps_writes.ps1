# Which PowerShell write paths work inside an AppContainer, and where the failing ones touch C:\
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$d = $env:PROBE_RW_ACL
$R = [ordered]@{}
function Rec([string]$name, [scriptblock]$fn) {
    try { $R[$name] = [ordered]@{ ok = $true; value = (& $fn) } }
    catch {
        $e = $_.Exception; while ($e.InnerException) { $e = $e.InnerException }
        $st = ($e.StackTrace -split "`n" | Select-Object -First 6 | ForEach-Object { $_.Trim() }) -join ' | '
        $R[$name] = [ordered]@{ ok = $false; error = ($e.GetType().Name + ': ' + $e.Message); stack = $st }
    }
}
Rec 'dotnet_WriteAllText' { [System.IO.File]::WriteAllText((Join-Path $d 'a.txt'), 'x'); 'ok' }
Rec 'Set-Content_LiteralPath' { Set-Content -LiteralPath (Join-Path $d 'b.txt') -Value 'x'; 'ok' }
Rec 'Set-Content_Path' { Set-Content -Path (Join-Path $d 'b2.txt') -Value 'x'; 'ok' }
Rec 'Out-File' { 'x' | Out-File -LiteralPath (Join-Path $d 'c.txt'); 'ok' }
Rec 'redirect_gt' { 'x' > (Join-Path $d 'd.txt'); 'ok' }
Rec 'New-Item_File' { $null = New-Item -ItemType File -Path (Join-Path $d 'e.txt') -Force; 'ok' }
Rec 'Add-Content' { Add-Content -LiteralPath (Join-Path $d 'a.txt') -Value 'y'; 'ok' }
Rec 'New-Item_Directory' { $null = New-Item -ItemType Directory -Path (Join-Path $d 'sub') -Force; 'ok' }
Rec 'Copy-Item' { Copy-Item -LiteralPath (Join-Path $env:PROBE_RO 'input.txt') -Destination (Join-Path $d 'f.txt'); 'ok' }
Rec 'Get-ChildItem' { @(Get-ChildItem -LiteralPath $d).Count }
Rec 'Set-Location_then_relative' { Set-Location -LiteralPath $d; Set-Content -LiteralPath 'g.txt' -Value 'x'; 'ok' }
Rec 'Remove-Item' { Remove-Item -LiteralPath (Join-Path $d 'a.txt'); 'ok' }
Rec 'Get-PSDrive_C' { (Get-PSDrive C).Root }
Rec 'Test-Path_C_root' { Test-Path -LiteralPath 'C:\' }
Rec 'GetFileAttributes_C_root' { [System.IO.File]::GetAttributes('C:\').ToString() }
[Console]::Out.WriteLine(($R | ConvertTo-Json -Depth 5 -Compress))
