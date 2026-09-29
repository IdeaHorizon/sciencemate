# ScienceMate —— 内网安装（Windows，不经浏览器，不弹 SmartScreen）
#
# ⚠️ 这个文件必须存成 **UTF-8 with BOM**。Windows 自带的 PowerShell 5.1 读
# 无 BOM 的 .ps1 时按机器的 ANSI 代码页解码 —— 里面的中文字符串会碎成乱码，
# 而乱码里的字节可能正好是引号，于是整个脚本变成语法错误（2026-09-21 实测：
# `Unexpected token` + `Missing closing ')'`，报错位置指向一行好端端的代码）。
# BOM 一摆，5.1 就按 UTF-8 读。和 csc 要 `/codepage:65001` 是同一件事。
#
#   irm <发布目录>/install.ps1 | iex
#
# ## 为什么这条路不会弹「Windows 已保护你的电脑」
#
# SmartScreen 拦的不是「没签名的程序」，是「带着网络来源标记（Mark-of-the-Web）的
# 程序」。那个标记是**浏览器、邮件客户端、解压工具**在保存文件时，通过 Windows 的
# Attachment Manager 写进文件的 `Zone.Identifier` 备用数据流里的。`Invoke-WebRequest`
# 不写 —— 2026-09-21 在 Windows 11 上实测：iwr 和 WebClient 下来的文件都只有
# `:$DATA` 一个流，没有 `Zone.Identifier`。
#
# 所以经这条路装的应用，系统根本不会去问它签没签名。这不是绕过，是 Windows 自己
# 划的线：**文件从哪来，由谁负责**。内网发布目录由我们自己负责，和从共享盘拷过来
# 是一回事。
#
# 这一点很要紧，因为「买张证书就不弹了」在 2024 年 8 月之后**不再成立** ——
# 微软那时把 EV 证书的 SmartScreen 特权从信任根里撤了，现在所有代码签名证书一视同仁，
# 一律靠下载量慢慢攒声誉。一个内部工具攒不到那个量。详见 docs/WINDOWS_CODE_SIGNING.md。
#
# 脚本最后还是会 `Unblock-File` 一次：万一有人先用浏览器下了 Setup.exe、再手动跑
# 这个脚本，那一份是带标记的。
#
# ## 它做什么
#
# 下 SHA256SUMS → 从里面认出安装器叫什么 → 下它 → 核 SHA-256 → 去标记 → 运行。
# 每一步失败就停，不留半个安装。
#
# 环境变量（`irm | iex` 传不了参数，所以配置走环境变量）：
#   AFS_RELEASE_URL   发布目录（打包器已经烧了默认值）
#   AFS_AUTH          私有 Forgejo 的 "user:token"（只读即可）
#   AFS_EDITION       personal（默认）/ pro
#   AFS_INSTALL_DIR   装到哪（默认让向导问）
#   AFS_SILENT        =1 无人值守：不弹向导、装完不启动应用，但快捷方式照建
#   AFS_DRY_RUN       =1 只说要做什么，不做

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$ProgressPreference = 'SilentlyContinue'

# 下面这一行由打包器整行替换（占位符只许出现在这里 —— install.sh 第一版把占位符
# 也写进了判断里，替换后要么语法错、要么把正确的地址当成"没填"拒掉）。
$DefaultUrl = '__RELEASE_URL__'

$Base    = if ($env:AFS_RELEASE_URL) { $env:AFS_RELEASE_URL } else { $DefaultUrl }
$Auth    = $env:AFS_AUTH
$Edition = if ($env:AFS_EDITION) { $env:AFS_EDITION } else { 'personal' }
$Dry     = [bool]$env:AFS_DRY_RUN

function Die($message) { Write-Host "✗ $message" -ForegroundColor Red; exit 1 }
function Say($message) { Write-Host $message }

if (-not $Base -or $Base -eq ('__RELEASE' + '_URL__')) {
    Die "没有发布目录地址。打包时传 --release-url，或运行前 `$env:AFS_RELEASE_URL='http://<内网主机>/afs'"
}
if ([Environment]::OSVersion.Platform -ne 'Win32NT') { Die "这个脚本只装 Windows；macOS 用 install.sh" }
if (-not [Environment]::Is64BitOperatingSystem) { Die "这个包是 x64 的，这台不是 64 位 Windows" }

$Base = $Base.TrimEnd('/')
$Headers = @{}
if ($Auth) {
    $Headers['Authorization'] = 'Basic ' + [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Auth))
}

# 下载用 WebClient，**不用 Invoke-WebRequest**。
#
# 安装包是 350 MB 上下，而 PowerShell 5.1（Windows 自带的那个）的 `iwr -OutFile`
# 处理大文件慢得不像话：2026-09-21 实测，从**本机回环** HTTP 拉这个包，一分多钟
# 才下到一半，还一直占着 CPU —— 真用户从内网拉会等到以为脚本挂了。
# `WebClient.DownloadFile` 拉同一个文件是秒级的。
#
# 它一样不打网络来源标记（同一天实测：iwr 和 WebClient 下来的文件都只有 `:$DATA`
# 一个流），所以换它不影响这条路「不弹 SmartScreen」的前提。
function Fetch($relative, $destination) {
    $url = "$Base/$relative"
    if ($Dry) { Say "  下载 $url → $destination"; return }
    $client = New-Object System.Net.WebClient
    try {
        foreach ($key in $Headers.Keys) { $client.Headers.Add($key, $Headers[$key]) }
        $client.DownloadFile($url, $destination)
    } finally { $client.Dispose() }
}

$work = Join-Path $env:TEMP ("afs-install-" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work -Force | Out-Null
try {
    Say "① 取校验和清单（它同时告诉我们安装器叫什么）"
    $sumsPath = Join-Path $work 'SHA256SUMS'
    Fetch 'SHA256SUMS' $sumsPath
    if ($Dry) { $lines = @() } else { $lines = Get-Content $sumsPath }

    # 安装器的文件名**从清单里认**，不烧进脚本：发布目录里有什么由清单说了算，
    # 两处各写一份的话，改了名字的那一天脚本会去下一个不存在的文件。
    #
    # 不按 AFS_EDITION 去滤：一个发布目录只发一种发行（publish_release.py 有一道闸
    # 守着这条），所以正常情况下清单里就只有一个 `*Setup.exe`，照用即可。按 edition
    # 滤反而会在专业版目录里滤出 0 个 —— 那儿只有 `ScienceMate-Pro-Setup.exe`，而
    # AFS_EDITION 默认是 personal。只有真出现两个时，AFS_EDITION 才有话要说。
    $entries = @()
    foreach ($line in $lines) {
        $parts = $line -split '\s+', 2
        if ($parts.Count -lt 2) { continue }
        $name = $parts[1].Trim()
        if ($name -notlike '*Setup.exe') { continue }
        $entries += [pscustomobject]@{ Sha = $parts[0].Trim().ToLower(); Name = $name }
    }
    if ($entries.Count -gt 1) {
        $wantPro = ($Edition -eq 'pro')
        $entries = @($entries | Where-Object { ($_.Name -like '*-Pro-Setup.exe') -eq $wantPro })
    }
    if (-not $Dry -and $entries.Count -ne 1) {
        Die ("清单里能对上「$Edition 版安装器」的有 " + $entries.Count + " 个，应当恰好 1 个。" +
             "清单：" + (($lines | Where-Object { $_ -like '*Setup.exe' }) -join '; '))
    }
    $setupName = if ($Dry) { 'ScienceMate-Setup.exe' } else { $entries[0].Name }
    $setupPath = Join-Path $work $setupName

    Say "② 下载 $setupName（不经浏览器，文件不会带上网络来源标记）"
    Fetch $setupName $setupPath

    Say "③ 核对 SHA-256"
    if (-not $Dry) {
        $actual = (Get-FileHash -Path $setupPath -Algorithm SHA256).Hash.ToLower()
        if ($actual -ne $entries[0].Sha) {
            Die "校验和对不上：清单说 $($entries[0].Sha)，下下来的是 $actual。没装，删掉了。"
        }
        Say "   $actual ✓"
        # 保险：万一这一份是别处（浏览器）来的，把来源标记去掉。
        Unblock-File -Path $setupPath
    }

    Say "④ 运行安装程序"
    $arguments = @()
    # 静默＝无人值守。装完**不启动应用**：没人在屏幕前，弹出来的窗口没人看；而且
    # 这条路多半跑在别的脚本里，起一个长命进程会让调用方的管道一直等着它 ——
    # 2026-09-21 实测就是这样挂住的，调用方以为安装卡死了，其实早装完了。
    # 快捷方式照建，人回到机器前从桌面或开始菜单打开。
    if ($env:AFS_SILENT) {
        $arguments += '--no-launch'
        $arguments += '--desktop-shortcut'; $arguments += '--start-menu'; $arguments += '--register-uninstall'
    }
    if ($env:AFS_INSTALL_DIR) { $arguments += '--install-dir'; $arguments += $env:AFS_INSTALL_DIR }
    if ($Dry) { Say ("  $setupPath " + ($arguments -join ' ')); exit 0 }
    $proc = if ($arguments.Count -gt 0) {
        Start-Process -FilePath $setupPath -ArgumentList $arguments -Wait -PassThru
    } else {
        Start-Process -FilePath $setupPath -Wait -PassThru
    }
    if ($proc.ExitCode -ne 0) { Die "安装程序退出码 $($proc.ExitCode) —— 没装成。" }
    if ($env:AFS_SILENT) { Say "✓ 装好了。从桌面或开始菜单打开 ScienceMate。" }
    else { Say "✓ 装好了。" }
}
finally {
    # 安装器已经把自己要的东西解到安装目录了，临时副本没有留下的理由。
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
