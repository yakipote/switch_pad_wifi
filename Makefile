# switchpad の起動・停止（中身は switchpad.ps1）
#
#   make start      常駐サーバー（キャプチャー＋MCP）を別ウィンドウで起動
#   make voice      ↑＋VOICEVOX（Docker）で読み上げ
#   make stop       停止（VOICEVOX のコンテナも落とす）
#   make restart    stop → start
#   make status     ESP32・常駐サーバー・VOICEVOX の状態を表示
#   make view       ライブ表示をブラウザで開く
#   make probe      カメラ一覧
#   make test       1 枚撮って test.jpg に保存
#
# 設定は .env（雛形は .env.example）。その場だけ変えるなら
#   make start SWITCHPAD_URL=http://192.168.1.50 CAPTURE_NAME=GC311
# （Windows の make は cmd でレシピを動かすので、処理は PowerShell スクリプトに置いている）

# コマンドラインで渡された値を環境変数として switchpad.ps1 に渡す（.env より優先）
export SWITCHPAD_URL CAPTURE_NAME CAPTURE_SHARE_PORT VOICEVOX

PS := powershell -NoProfile -ExecutionPolicy Bypass -File switchpad.ps1

TARGETS := start voice stop restart status view probe test
.PHONY: help $(TARGETS)

help:
	@$(PS) help

$(TARGETS):
	@$(PS) $@
