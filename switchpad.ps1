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

function Start-Capture([string]$extra) {
    if (Test-Capture) {
        Write-Host "もう起動しています（止めるなら make stop）: http://127.0.0.1:$Port/"
        return
    }
    # 環境変数は子プロセスに引き継がれる（ESP32 のトークンは config.h から読まれる）
    Start-Process powershell -WindowStyle Minimized -WorkingDirectory $PSScriptRoot `
        -ArgumentList "-NoProfile", "-NoExit", "-Command", "uv run $script --capture-server $extra"
    Write-Host "起動を待っています…（VOICEVOX は初回だけ数分かかる）"
    # VOICEVOX 付きはエンジンの準備ができてからポートを開くので長めに待つ
    for ($i = 0; $i -lt 150; $i++) {
        $info = Test-Capture
        if ($info) {
            Write-Host "OK: $($info.device_name) $($info.size)  ->  http://127.0.0.1:$Port/  （MCP: /mcp）"
            return
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
    "voice"   { Start-Capture "--voicevox-docker $Voicevox" }
    "stop"    { uv run $script --stop }
    "restart" { uv run $script --stop; Start-Sleep 2; Start-Capture "" }
    "status"  { Show-Status }
    "view"    { Start-Process "http://127.0.0.1:$Port/" }
    "probe"   { uv run $script --probe }
    "test"    { uv run $script --test }
    default   { Get-Content $PSCommandPath -TotalCount 12 | Select-Object -Skip 2 }
}
