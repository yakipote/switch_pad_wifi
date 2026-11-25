# switchpad — Claude に Nintendo Switch 2 を遊ばせる

Claude が Switch 2 の画面を見て、コントローラーを操作します。

```
Claude ──MCP(HTTP)──▶ 常駐サーバー ──Wi-Fi──▶ ESP32-S3 ──USB──▶ Switch 2
        127.0.0.1:8765/mcp   ▲                                    │ HDMI
                             └──────── キャプチャーボード（GC311G2）◀┘
```

- **ESP32-S3**（`switch_pad_wifi.ino`）：Switch からは USB コントローラーに見える。Wi-Fi の HTTP API でボタンを押す
- **常駐サーバー**（`switchpad_mcp.py --capture-server`、`make start` で起動）：これ 1 つで全部やる。キャプチャーボードの映像を取り込み続け、MCP サーバー（`/mcp`）、ブラウザのライブ表示、読み上げを担当する

細かい仕様やハマりどころは [HANDOFF.md](HANDOFF.md) にあります。

---

## 1. 最初に 1 回だけやること

### 1-1. 必要なもの

**ハードウェア**

| もの | 説明 |
|---|---|
| ESP32-S3 ボード | USB（OTG）が使えるもの。動作確認はスイッチサイエンス「ESPr Developer S3 Type-C」（ESP32-S3-WROOM-1-N16R8）。2.4GHz の Wi-Fi が必要 |
| USB ケーブル | ESP32-S3 の USB 端子 ↔ Switch 2（ドック）の USB |
| キャプチャーボード（または Web カメラ） | Switch 2 の HDMI を PC に取り込む。動作確認は AVerMedia GC311G2。テレビの画面を Web カメラで撮る形でも動く（後述の 1-3） |

**PC（Windows）のソフト**

| もの | 入れ方 |
|---|---|
| [uv](https://docs.astral.sh/uv/) | `winget install astral-sh.uv` |
| make | `winget install ezwinports.make`（入れたら PowerShell を開き直す） |
| [Arduino IDE](https://www.arduino.cc/en/software) | ESP32 に書き込むとき |
| [Node.js](https://nodejs.org/) | Claude Desktop から使うとき（`npx`） |
| Docker Desktop | 読み上げ（VOICEVOX）を使うときだけ |

### 1-2. ESP32-S3 にファームウェアを書き込む

**Arduino IDE の準備（最初の 1 回）**

1. 「ファイル → 基本設定 → 追加のボードマネージャの URL」に次を足す
   ```
   https://espressif.github.io/arduino-esp32/package_esp32_index.json
   ```
2. 「ツール → ボード → ボードマネージャ」で **esp32（by Espressif Systems）** をインストール（4.0.0 で動作確認）
3. 「ツール → ライブラリを管理」で **ArduinoJson**（by Benoit Blanchon、7.x）をインストール

**設定ファイルを作る**

4. このフォルダの `config.example.h` をコピーして `config.h` にし、次を書き換える（`config.h` は git に入らない）
   - `WIFI_SSID` / `WIFI_PASSWORD`：家の Wi-Fi（**2.4GHz**。ESP32-S3 は 5GHz に非対応）
   - `API_TOKEN`：長いランダム文字列。PowerShell なら `[guid]::NewGuid().ToString("N")` で作れる

**書き込む**

5. `switch_pad_wifi.ino` を Arduino IDE で開き、「ツール」メニューを次のようにする

   | 項目 | 値 |
   |---|---|
   | ボード | ESP32S3 Dev Module |
   | USB Mode | USB-OTG (TinyUSB) |
   | USB CDC On Boot | **Disabled**（有効だと Switch での認識が不安定になる） |
   | Flash Size | 16MB (128Mb)（ボードに合わせる） |
   | PSRAM | OPI PSRAM（ボードに合わせる） |

6. ESP32-S3 を PC につなぎ、**書き込みモード**にする：BOOT（FLASH）ボタンを押したまま RESET を押して離し、そのあと BOOT を離す
7. 「ツール → シリアルポート」で出てきた COM ポートを選び、「書き込み」
8. 書き込みが終わったら RESET を押す。コントローラーとして動き始めるので **COM ポートは消える（正常）**。書き直すときは、もう一度 6 からやる

**Switch 2 につなぐ**

9. ESP32-S3 を Switch 2 のドックの USB につなぐ。LED が点滅して Wi-Fi につながる
10. ESP32 の IP を調べる：`ping switchpad.local` で表示される IP か、ルーターの接続機器一覧で確認。ルーターで IP を固定（DHCP 予約）しておくと楽
11. ブラウザや PowerShell で `http://<ESP32 の IP>/` を開き、状態が JSON で返れば OK（`API_TOKEN` を設定した場合は 401 になるのが正常。`make status` で確認できる）

Switch からは「汎用ライセンスコントローラー」として見えます（HORI のポッ拳コントローラーを名乗っているため）。

### 1-3. PC 側の設定（.env）

`.env.example` をコピーして `.env` にし、ESP32 の IP などを書きます（`.env` は git に入らない）。

```ini
SWITCHPAD_URL=http://<ESP32 の IP>
CAPTURE_NAME=GC311
```

`CAPTURE_NAME` はキャプチャーボードの名前の一部です。`make probe` で一覧が出ます。設定したら `make start` → `make status` で、ESP32 とキャプチャーの両方が OK になるか確認します。

#### Web カメラで画面を見せる場合

キャプチャーボードが無くても、テレビの画面を Web カメラで撮れば動きます（OpenCV でカメラとして読んでいるだけなので、種類は問いません）。`.env` をこうします。

```ini
CAPTURE_NAME=<Web カメラの名前の一部>
CAPTURE_FOURCC=MJPG
```

- 多くの Web カメラは 1080p を MJPG でしか出せないので、`CAPTURE_FOURCC=MJPG` にする（既定の YUY2 のままだと解像度やコマ数が落ちる）。うまくいかなければ空にして、カメラの既定の形式にする
- 正面から、画面がなるべく大きく映るように置く。斜めの角度、照明の映り込み、モアレがあると小さい文字が読みにくくなる
- 画面の外（壁など）も映るので、`screenshot` の `crop` の座標は「画面全体＝1920x1080」にならない
- 露出やピントの調整で少し遅れることがあるので、`press_and_look` の `wait_ms` は長めがよい

### 1-4. Claude に MCP サーバーを登録する

MCP サーバーは常駐サーバー（`make start`）の中で動いていて、`http://127.0.0.1:8765/mcp` で待っています。Claude からはこの URL につなぐだけです。ESP32 のトークンは常駐サーバーが `config.h` から読むので、Claude 側の設定には書きません。

使う方だけ設定すれば OK です。

#### Claude Desktop の場合

[Node.js](https://nodejs.org/) が必要です（`npx` を使います）。設定ファイル `claude_desktop_config.json` を開きます。Claude Desktop の「設定 → 開発者 → 設定を編集」から開くのが確実です。

場所はインストール方法やバージョンで変わります。Microsoft Store 版では `%LOCALAPPDATA%\Claude-Data\claude_pzs8sxrjxfjjc\` にあることがあります。`%APPDATA%\Claude\` などにも同じ名前のファイルがあり、そこに `.user-data-left...` という目印があれば古い場所で、今は読まれていません。

`mcpServers` の中に `switchpad` を足します。ほかのサーバーと並べるときは、`,` の位置と `{ }` の対応に注意してください（JSON が壊れると全部読み込まれません）。

```json
{
  "mcpServers": {
    "switchpad": {
      "command": "cmd",
      "args": ["/c", "npx", "-y", "mcp-remote", "http://127.0.0.1:8765/mcp"]
    }
  }
}
```

`cmd /c` を挟むのは、PowerShell の実行ポリシーで `npx.ps1` が止まるのを避けるためです。

保存したら **Claude を完全に再起動** します（ウィンドウの × ではなく、タスクトレイのアイコンを右クリック →「Quit」）。PowerShell なら次の 1 行でも再起動できます。

```powershell
Stop-Process -Name Claude -Force; Start-Sleep 3; Start-Process "shell:AppsFolder\Claude_pzs8sxrjxfjjc!Claude"
```

#### Claude Code（ターミナル）の場合

HTTP でそのままつなげます（`npx` は不要）。

```powershell
claude mcp add --transport http switchpad http://127.0.0.1:8765/mcp
```

#### 設定のポイント

- 先に `make start` で常駐サーバーを起動しておく。止まっているとツールが使えない（起動し直せば Claude は再起動しなくてよい）
- ESP32 の IP やキャプチャーボードは `.env` で設定する（1-3）
- 以前の設定（`uv run switchpad_mcp.py` を Claude から直接起動する方式）もまだ動くが、Python が Claude の中にもう 1 つ増えるので非推奨

ほかの環境変数は [HANDOFF.md §6.3](HANDOFF.md) を参照。

---

## 2. 毎回の使い方

このフォルダで PowerShell を開いて：

```powershell
make start     # 常駐サーバーを起動（最小化したウィンドウで動く）
make voice     # 読み上げ付きで起動したいときはこちら（Docker Desktop が止まっていれば自動で起動する）
make status    # ESP32・キャプチャー・VOICEVOX の状態を確認
make view      # ライブ表示をブラウザで開く
make stop      # 止める（VOICEVOX のコンテナも止まる）
```

| コマンド | 内容 |
|---|---|
| `make start` | 常駐キャプチャーサーバーを起動 |
| `make voice` | ＋VOICEVOX（ずんだもん）で読み上げ。Docker Desktop が止まっていれば起動して待つ。初回はイメージのダウンロードで数分かかる |
| `make stop` | 停止 |
| `make restart` | 停止して起動し直す |
| `make status` | 3 つの状態をまとめて表示 |
| `make view` | `http://127.0.0.1:8765/` を開く |
| `make probe` | カメラ一覧を表示 |
| `make test` | 1 枚撮って `test.jpg` に保存 |

設定は `.env` に書きます。その場だけ変えるなら `make start CAPTURE_NAME=GC311` や `make voice VOICEVOX=gpu` のように上書きできます。

中身は [switchpad.ps1](switchpad.ps1) です（Windows の make はレシピを cmd で動かすため、処理は PowerShell 側に置いています）。make が無くても、次のように同じことができます（`.\switchpad.ps1 start` と直接書くと、PowerShell の実行ポリシーで止められることがある）。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\switchpad.ps1 start
```

`make status` で `usb_ready=False` と出たり、ライブ表示が「No Signal」になったりするときは、Switch がスリープしています。

### 起動の順番

1. Switch 2 をドックに入れてテレビモードにし、ESP32 をつなぐ
2. `make start`（または `make voice`）
3. `make status` で 3 つとも OK を確認
4. Claude を起動して話しかける

### Claude への頼み方の例

- 「Switch の画面を見て、今どこにいるか教えて」
- 「HOME 画面に戻って」
- 「ストリートファイター6 を起動して、トレーニングモードに入って」
- 「操作するときは say で何をするか一言しゃべって」

### 操作の決まりごと（スキル）

`say` で一言しゃべってから押す、取り消せない画面では止まって確認する、などの基本手順は [.claude/skills/switchpad-play/SKILL.md](.claude/skills/switchpad-play/SKILL.md) にまとめてあります。Claude Code ではこのフォルダで開けば自動で読まれます（`/switchpad-play` で明示的に呼ぶこともできる）。Claude Desktop で使うときは、この SKILL.md をスキルとしてアップロードするか、内容をプロジェクトの指示に貼ってください。

### Claude が使えるツール

| ツール | 内容 |
|---|---|
| `status` | ESP32 とキャプチャーの状態 |
| `screenshot` | 画面を 1 枚撮る（幅 640 まで。細かく見たいときは `crop`） |
| `press` | ボタンを短く押す |
| `press_and_look` | 押して、少し待って、画面を撮る |
| `sequence` | タイミング付きの入力列（コマンド技など） |
| `set_state` / `rest` | 押しっぱなし／全部離す |
| `say` | 配信画面にコメントを出す（`make voice` なら読み上げも） |

### ライブ表示と配信

| URL | 内容 |
|---|---|
| `http://127.0.0.1:8765/` | 画面＋操作ログ（左下）＋コメント吹き出し（上） |
| `http://127.0.0.1:8765/overlay` | 背景が透明な版。OBS のブラウザソース用（1920x1080） |
| `?tts=1` を付ける | ブラウザ側でも読み上げる |

`/overlay` には、上の中央に Claude のコメントの吹き出し（`say`、15 秒で消える）、左下に操作ログ（`→`、`A`、`↓↘→+X` など。最新 6 件）が出ます。

VOICEVOX を配信で使うときは、キャラクターごとのクレジット表記が必要です。

### OBS で配信する

キャプチャーボードは **1 つのアプリしか開けない** ので、OBS と常駐サーバーで取り合いになります。OBS にキャプチャーボードを持たせ、常駐サーバーは OBS の仮想カメラから映像をもらう形にします。

> この構成はまだ実機で通しで試していません。うまくいかないところがあれば HANDOFF.md に追記してください。

**1. OBS でシーンを作る**

1. ソースに「映像キャプチャデバイス」を追加し、キャプチャーボード（GC311G2 など）を選ぶ。名前は「Switch」などにしておく
2. その上に「ブラウザ」ソースを追加する
   - URL：`http://127.0.0.1:8765/overlay`
   - 幅：1920、高さ：1080
   - 「ページの権限」などはそのままでよい。背景は自動で透明になる
3. 声を出したい場合（どちらか 1 つ）
   - VOICEVOX（`make voice`）：PC のスピーカーから出るので、OBS の「デスクトップ音声」で拾う
   - ブラウザの音声：URL を `http://127.0.0.1:8765/overlay?tts=1` にして、ブラウザソースの「OBS で音声を制御する」をオンにする
   - 両方使うと声が二重になる
4. ゲームの音は、映像キャプチャデバイスのプロパティで「音声出力モード」を「デスクトップ音声出力」にするか、キャプチャーボードの音声入力を足す

**2. 仮想カメラで常駐サーバーに映像を渡す**

1. OBS の「仮想カメラ開始」の横の歯車から、出力の種類を **「ソース」** にして、上で作った「Switch」（映像キャプチャデバイス）を選ぶ
   - 「番組（Program）」のままだと、吹き出しや操作ログまで Claude の `screenshot` に写ってしまう
2. 「仮想カメラ開始」を押す
3. `.env` を次のようにして、`make restart`

   ```ini
   CAPTURE_NAME=OBS Virtual Camera
   CAPTURE_FOURCC=
   ```

   `CAPTURE_FOURCC` は空にして、仮想カメラの既定の形式で受け取る

4. `make status` でキャプチャーが OK、`make view` で映像が出れば完了

**起動の順番**は「OBS（仮想カメラ開始）→ `make start` → Claude」です。OBS を先に閉じると、常駐サーバーは映像が来なくなり、2 秒おきに開き直そうとします（OBS を起動し直せば戻ります）。

---

## 3. 困ったとき

| 症状 | 対処 |
|---|---|
| MCP のツールがエラーになる・タイムアウトする | 常駐サーバーが止まっている。`make status` で確認して `make start`。それでもだめなら Claude を完全に再起動する |
| `say` などのツールが Claude に出てこない | 古い設定（stdio 版）のプロセスが残っている。設定を 1-4 の形にして Claude を完全に再起動する |
| 画面の色がおかしい | 取り込み形式が MJPG になっている。`CAPTURE_FOURCC` を消すか `YUY2` にする |
| ESP32 に届かない | Switch がスリープすると ESP32 の電源も落ちる。Switch を起こして `make status`（`.env` の `SWITCHPAD_URL` が合っているかも確認） |
| ボタンは効くのにゲーム内で動かない | 1P になっていない。HOME → コントローラー →「持ちかた/順番を変える」で L+R を押す。Joy-Con は外しておく |
| `screenshot` が返ってこない | 幅 640 より大きいと止まることがある。`max_width` を下げて `crop` を使う |
| `make` が見つからない | `winget install ezwinports.make` のあと、PowerShell を開き直す。Claude アプリの Terminal パネルや VS Code のターミナルは、アプリごと再起動しないと新しい PATH が入らない |
| `make voice` で「起動中に終了しました」 | 最小化したウィンドウ（タスクバー）にエラーが出ている。Docker の問題なら Docker Desktop の画面も確認。読み上げなしでよければ `make start` |

## 4. 注意

- オンライン対戦・ランクマッチでは自動操作を使わない
- セーブデータが消える確認画面などでは、Claude に押させる前に人が確認する
- `config.h` と Claude の設定ファイルにはトークンと Wi-Fi パスワードが入っている。公開しない
- ESP32 は HORI のコントローラーを名乗っている。個人の動作確認用で、配布・販売はしない
