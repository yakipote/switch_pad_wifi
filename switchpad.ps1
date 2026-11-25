# switchpad の起動・停止（Makefile から呼ばれる）
#
#   start    常駐サーバー（キャプチャー＋MCP）を別ウィンドウ（最小化）で起動
#   voice    ↑＋VOICEVOX（Docker）で読み上げ
#   stop     停止（VOICEVOX のコンテナも落とす）
#   restart  stop → start
#   status   ESP32・常駐サーバー・VOICEVOX の状態を表示
#   view     ライブ表示をブラウザで開く
#   probe    カメラ一覧
#   test     1 枚撮って test.jpg に保存
#
# 設定は .env（雛形は .env.example）。環境変数が設定済みならそちらが優先

param([Parameter(Position = 0)][string]$Command = "help")

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$script = ".\switchpad_mcp.py"

# .env を読む（KEY=VALUE。# から始まる行と空行は無視。すでにある環境変数は上書きしない）
if (Test-Path .env) {
    foreach ($line in Get-Content .env -Encoding UTF8) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$' -and $line -notmatch '^\s*#') {
            $name = $Matches[1]
            $value = $Matches[2].Trim('"', "'")
            if (-not [Environment]::GetEnvironmentVariable($name)) {
                [Environment]::SetEnvironmentVariable($name, $value)
            }
        }
    }
}

function Get-Setting([string]$name, [string]$default) {
    $v = [Environment]::GetEnvironmentVariable($name)
    if ($v) { return $v } else { return $default }
}

$env:SWITCHPAD_URL = Get-Setting "SWITCHPAD_URL" "http://switchpad.local"
$env:CAPTURE_NAME = Get-Setting "CAPTURE_NAME" ""
$env:CAPTURE_SHARE_PORT = Get-Setting "CAPTURE_SHARE_PORT" "8765"
$Voicevox = Get-Setting "VOICEVOX" "cpu"
$Port = $env:CAPTURE_SHARE_PORT

function Test-Capture {
    try { return Invoke-RestMethod "http://127.0.0.1:$Port/info" -TimeoutSec 2 } catch { return $null }
}

function Test-Docker {
    # デーモンが止まっていると docker info はすぐ失敗する。
    # Windows PowerShell 5.1 は ErrorActionPreference=Stop だと、外部コマンドの stderr を
    # 致命的エラーにしてしまうので、cmd 経由で出力ごと捨てる
    cmd /c "docker info --format {{.ServerVersion}} >nul 2>&1"
    return ($LASTEXITCODE -eq 0)
}

function Initialize-Docker {
    # make voice 用。Docker が止まっていたら Docker Desktop を起動して、使えるようになるまで待つ
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-Host "docker コマンドが見つかりません。Docker Desktop を入れてください（読み上げなしなら make start）"
        exit 1
    }
    if (Test-Docker) { return }

    $candidates = @(
        "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe",
        "$env:LOCALAPPDATA\Programs\DockerDesktop\Docker Desktop.exe"
    )
    $exe = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $exe) {
        Write-Host "Docker が動いていません。Docker Desktop を起動してから、もう一度 make voice してください"
        exit 1
    }
    Write-Host "Docker が動いていないので、Docker Desktop を起動します…"
    Start-Process $exe
    $wait = [int](Get-Setting "SWITCHPAD_DOCKER_WAIT" "120")
    for ($i = 0; $i -lt $wait; $i += 3) {
        Start-Sleep 3
        if (Test-Docker) { Write-Host "Docker の準備ができました"; return }
    }
    Write-Host "$wait 秒待っても Docker が使えるようになりませんでした。Docker Desktop の画面を確認してください"
    exit 1
}

function Test-ServerAlive($shell) {
    # 起動したウィンドウ（powershell -NoExit）の下で uv / python がまだ動いているか
    $kids = Get-CimInstance Win32_Process -Filter "ParentProcessId=$($shell.Id)" -ErrorAction SilentlyContinue
    return [bool]($kids | Where-Object { $_.Name -match '^(uv|python)' })
}

function Start-Capture([string]$extra) {
    if (Test-Capture) {
        Write-Host "もう起動しています（止めるなら make stop）: http://127.0.0.1:$Port/"
        return
    }
    # 環境変数は子プロセスに引き継がれる（ESP32 のトークンは config.h から読まれる）
    # -NoExit なので、失敗してもウィンドウが残ってログを読める
    $shell = Start-Process powershell -WindowStyle Minimized -WorkingDirectory $PSScriptRoot -PassThru `
        -ArgumentList "-NoProfile", "-NoExit", "-Command", "uv run $script --capture-server $extra"
    Write-Host "起動を待っています…（VOICEVOX は初回だけイメージのダウンロードで数分かかる）"
    # VOICEVOX 付きはエンジンの準備ができてからポートを開くので長めに待つ
    for ($i = 0; $i -lt 150; $i++) {
        $info = Test-Capture
        if ($info) {
            Write-Host "OK: $($info.device_name) $($info.size)  ->  http://127.0.0.1:$Port/  （MCP: /mcp）"
            return
        }
        # 起動直後は uv がまだ立ち上がっていないことがあるので、少し待ってから見張る
        if ($i -ge 3 -and -not (Test-ServerAlive $shell)) {
            Write-Host "常駐サーバーが起動中に終了しました。タスクバーの最小化したウィンドウでエラーを確認してください"
            exit 1
        }
        Start-Sleep 2
    }
    Write-Host "起動しませんでした。最小化したウィンドウのログを見てください"
    exit 1
}

function Show-Status {
    $token = ""
    if (Test-Path config.h) {
        $m = Select-String -Path config.h -Pattern '^#define API_TOKEN "(.*)"'
        if ($m) { $token = $m.Matches[0].Groups[1].Value }
    }
    try {
        $p = Invoke-RestMethod "$env:SWITCHPAD_URL/" -Headers @{ Authorization = "Bearer $token" } -TimeoutSec 3
        Write-Host "ESP32       : OK  usb_ready=$($p.usb_ready) rssi=$($p.rssi)"
    } catch { Write-Host "ESP32       : NG  ($env:SWITCHPAD_URL に届かない。Switch がスリープしていないか、.env の SWITCHPAD_URL を確認)" }
    $c = Test-Capture
    if ($c) { Write-Host "キャプチャー: OK  $($c.device_name) $($c.size)  最新フレーム $($c.last_frame_age_s) 秒前" }
    else { Write-Host "キャプチャー: 停止中（make start で起動）" }
    try {
        $v = Invoke-RestMethod "http://127.0.0.1:50021/version" -TimeoutSec 2
        Write-Host "VOICEVOX    : OK  $v"
    } catch { Write-Host "VOICEVOX    : 停止中（使うなら make voice）" }
}

switch ($Command) {
    "start"   { Start-Capture "" }
    "voice"   { Initialize-Docker; Start-Capture "--voicevox-docker $Voicevox" }
    "stop"    { uv run $script --stop }
    "restart" { uv run $script --stop; Start-Sleep 2; Start-Capture "" }
    "status"  { Show-Status }
    "view"    { Start-Process "http://127.0.0.1:$Port/" }
    "probe"   { uv run $script --probe }
    "test"    { uv run $script --test }
    default   { Get-Content $PSCommandPath -TotalCount 12 | Select-Object -Skip 2 }
}
