#Requires -Version 5.1
param([switch]$NoPause)
<#
    update_Ban.ps1 —— update_Ban.bat 的 PowerShell 转写版

    逻辑：并行运行 scrape_BTN.py 与 scrape_DamnYou.py，
          全部成功后运行 merge_Ban.py 合并数据。

    v2 改进（相对第一版）：
        1. 修复 ExitCode 竞态：PS 5.1 下 Start-Process -PassThru 返回的进程
           若未先访问 .Handle，WaitForExit 后 .ExitCode 可能是 null，
           而 (null -ne 0) 为真 → 抓取成功也会被误判失败、跳过 merge。
           现在启动后立刻 $p.Handle 触碰句柄，并对 null 单独兜底。
        2. 按序输出：两个 worker 的 stdout/stderr 重定向到临时日志，
           等待结束后按 BTN → DamnYou 的固定顺序完整回放，
           不再让并行进程在控制台里抢话筒。
        3. 错误末尾汇总：所有输出之后统一打印失败清单。
        4. 结束不闪退：默认最后 Read-Host 停住等回车（双击/右键运行友好）；
           传 -NoPause 跳过（供计划任务/自动化调用）。

    bat → ps1 概念对照表：
        %~dp0 / cd /d            → $PSScriptRoot / Set-Location
        call :函数名 / goto 标签  → function
        start "" /b 子进程        → Start-Process -NoNewWindow -PassThru
        状态文件 + ping 轮询      → Process.WaitForExit() + ExitCode
        %ERRORLEVEL% / exit /b   → $LASTEXITCODE / exit N
#>

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Find-Python {
    <# 复刻 :find_python：环境变量优先 → .venv → PATH → codex-runtimes 缓存 #>

    # 1) 环境变量 PYTHON_EXE（可配 PYTHON_ARGS），必须能跑通 --version 才算数
    if ($env:PYTHON_EXE) {
        $candidate = $env:PYTHON_EXE
        $resolved = $null
        if (Test-Path -LiteralPath $candidate) {
            $resolved = $candidate
        } else {
            $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
            if ($cmd) { $resolved = $cmd.Source }
        }
        if ($resolved) {
            $extra = @()
            if ($env:PYTHON_ARGS) { $extra = $env:PYTHON_ARGS -split '\s+' }
            & $resolved @extra '--version' *> $null
            if ($LASTEXITCODE -eq 0) {
                return @{ Exe = $resolved; PrefixArgs = $extra }
            }
        }
    }

    # 2) 项目自带 .venv
    $venvPy = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venvPy) {
        return @{ Exe = $venvPy; PrefixArgs = @() }
    }

    # 3) PATH 上的 python.exe / python3.exe / py.exe -3
    foreach ($cand in @(
        @{ Exe = 'python.exe';  Prefix = @() },
        @{ Exe = 'python3.exe'; Prefix = @() },
        @{ Exe = 'py.exe';      Prefix = @('-3') }
    )) {
        $cmd = Get-Command $cand.Exe -ErrorAction SilentlyContinue
        if ($cmd) {
            & $cmd.Source @($cand.Prefix) '--version' *> $null
            if ($LASTEXITCODE -eq 0) {
                return @{ Exe = $cmd.Source; PrefixArgs = @($cand.Prefix) }
            }
        }
    }

    # 4) codex-runtimes 缓存目录里翻找
    $cacheRoot = Join-Path $env:USERPROFILE '.cache\codex-runtimes'
    if (Test-Path -LiteralPath $cacheRoot) {
        foreach ($dir in Get-ChildItem -LiteralPath $cacheRoot -Directory) {
            $pyExe = Join-Path $dir.FullName 'dependencies\python\python.exe'
            if (Test-Path -LiteralPath $pyExe) {
                & $pyExe '--version' *> $null
                if ($LASTEXITCODE -eq 0) {
                    return @{ Exe = $pyExe; PrefixArgs = @() }
                }
            }
        }
    }

    return $null
}

function Invoke-Worker {
    <# 启动一个抓取子进程，stdout/stderr 重定向到临时日志，返回进程+日志路径 #>
    param(
        [hashtable]$Py,
        [string]$ScriptName
    )
    $scriptPath = Join-Path $PSScriptRoot $ScriptName
    $argString = (@($Py.PrefixArgs) + @('"' + $scriptPath + '"', '--verbose')) -join ' '
    $stamp = Get-Date -Format 'HHmmss'
    $outLog = Join-Path $env:TEMP ("update_Ban_{0}_{1}_{2}.out.log" -f $ScriptName, $PID, $stamp)
    $errLog = $outLog -replace '\.out\.log$', '.err.log'

    $p = Start-Process -FilePath $Py.Exe -ArgumentList $argString `
            -WorkingDirectory $PSScriptRoot -NoNewWindow -PassThru `
            -RedirectStandardOutput $outLog -RedirectStandardError $errLog

    # ★ 关键修复：立刻触碰句柄。PS 5.1 不摸 Handle 的话，
    #   WaitForExit 之后 .ExitCode 仍可能是 null（竞态），进而误判失败。
    $null = $p.Handle

    return @{ Name = $ScriptName; Proc = $p; OutLog = $outLog; ErrLog = $errLog }
}

function Show-WorkerLog {
    <# 按序回放某个 worker 的输出日志 #>
    param([hashtable]$W)
    Write-Host ''
    Write-Host ("===== {0} 输出 =====" -f $W.Name)
    foreach ($log in @($W.OutLog, $W.ErrLog)) {
        if ((Test-Path -LiteralPath $log) -and (Get-Item -LiteralPath $log).Length -gt 0) {
            Get-Content -LiteralPath $log -Encoding UTF8 | ForEach-Object { Write-Host $_ }
        }
    }
}

function Invoke-Main {
    <# 主流程，返回最终退出码（0 = 成功）#>
    param([hashtable]$Py)

    Write-Host "Using Python: $($Py.Exe)"
    Write-Host 'Starting scrape_BTN.py and scrape_DamnYou.py in parallel...'

    $wBtn  = Invoke-Worker -Py $Py -ScriptName 'scrape_BTN.py'
    $wDamn = Invoke-Worker -Py $Py -ScriptName 'scrape_DamnYou.py'

    # 等待两个 worker 全部结束（对应 :wait_for_scrapers 轮询循环）
    foreach ($w in @($wBtn, $wDamn)) { $w.Proc.WaitForExit() }

    # 按固定顺序回放输出，不再抢话筒
    Show-WorkerLog -W $wBtn
    Show-WorkerLog -W $wDamn

    # 末尾统一汇总报错（放在所有输出之后，不会被淹没）
    $failed = @()
    foreach ($w in @($wBtn, $wDamn)) {
        $rc = $w.Proc.ExitCode
        if ($null -eq $rc) {
            $failed += ('{0} (exit code 丢失：PS 5.1 ExitCode 竞态)' -f $w.Name)
        } elseif ($rc -ne 0) {
            $failed += ('{0} (exit code {1})' -f $w.Name, $rc)
        }
    }
    if ($failed.Count -gt 0) {
        Write-Host ''
        Write-Host ('[ERROR] 抓取失败汇总 -> ' + ($failed -join '；'))
        Write-Host 'merge_Ban.py was not run.'
        return 1
    }

    Write-Host ''
    Write-Host 'Both scrape tasks completed. Starting merge_Ban.py...'
    & $Py.Exe @($Py.PrefixArgs) (Join-Path $PSScriptRoot 'merge_Ban.py')
    $mergeRc = $LASTEXITCODE
    if ($mergeRc -ne 0) {
        throw "merge_Ban.py failed with exit code $mergeRc."
    }

    Write-Host 'All tasks completed successfully.'
    return 0
}

# --- 入口：单一退出点 + finally 保证收尾 ---
$script:FinalExit = 1
try {
    $py = Find-Python
    if (-not $py) {
        throw 'Python 3 was not found. Set PYTHON_EXE, add Python to PATH, or create .venv.'
    }
    $script:FinalExit = Invoke-Main -Py $py
}
catch {
    Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red
    $script:FinalExit = 1
}
finally {
    # 清理临时日志（尽力而为）
    Get-ChildItem -LiteralPath $env:TEMP -Filter 'update_Ban_*_*.log' -ErrorAction SilentlyContinue |
        Where-Object { $_.LastWriteTime -gt (Get-Date).AddHours(-2) } |
        Remove-Item -Force -ErrorAction SilentlyContinue

    # 双击/右键运行时窗口不再一闪而过；自动化调用传 -NoPause 跳过
    if (-not $NoPause) {
        Write-Host ''
        Read-Host '按 Enter 键关闭窗口' | Out-Null
    }
}
exit $script:FinalExit
