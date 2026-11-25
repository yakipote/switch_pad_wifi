# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp[cli]>=1.2,<2",
#   "httpx>=0.27",
#   "opencv-python>=4.9",
#   "pillow>=10",
#   "pygrabber>=0.2; sys_platform == 'win32'",
# ]
# ///
"""switchpad_mcp.py

ESP32-S3 製の Switch 用コントローラー（switch_pad_wifi）と、キャプチャーボードを
Claude から使うための MCP サーバー。

環境変数で設定する（Claude Desktop の設定ファイルの env に書く）
  SWITCHPAD_URL      コントローラーの URL（既定: http://switchpad.local）
  SWITCHPAD_TOKEN    config.h の API_TOKEN と同じ値
  CAPTURE_NAME       キャプチャーボードの名前の一部（--probe で表示される。番号より確実）
  CAPTURE_DEVICE     キャプチャーボードのカメラ番号（CAPTURE_NAME が無いときに使う。既定: 0）
  CAPTURE_WIDTH      取り込み解像度の幅（既定: 1920）
  CAPTURE_HEIGHT     取り込み解像度の高さ（既定: 1080）
  CAPTURE_FOURCC     取り込み形式（既定: YUY2。MJPG は色化けする。空文字でデバイス既定のまま）

  SWITCHPAD_TOKEN が空なら、同じフォルダの config.h の API_TOKEN を読む

使い方（おすすめ）
  uv run switchpad_mcp.py --capture-server
    キャプチャーと MCP を 1 プロセスで動かす。MCP は http://127.0.0.1:8765/mcp（Streamable HTTP）。
    Claude Desktop からは npx mcp-remote http://127.0.0.1:8765/mcp で、
    Claude Code からは claude mcp add --transport http で接続する

コマンド
  uv run switchpad_mcp.py --probe   使えるカメラ番号を探す
  uv run switchpad_mcp.py --test    1 枚撮って test.jpg に保存する（切り分け用）
  uv run switchpad_mcp.py           従来の stdio の MCP サーバー（常駐サーバーから画像をもらう）
"""

from __future__ import annotations

import io
import os
import sys
import threading
import time
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP, Image

# ---------------------------------------------------------------
# 設定
# ---------------------------------------------------------------

def _token_from_config_h() -> str:
    """SWITCHPAD_TOKEN が無いときは、同じフォルダの config.h の API_TOKEN を使う"""
    import re

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.h")
    try:
        with open(path, encoding="utf-8") as f:
            m = re.search(r'^#define\s+API_TOKEN\s+"(.*)"', f.read(), re.MULTILINE)
        return m.group(1) if m else ""
    except OSError:
        return ""


PAD_URL = os.environ.get("SWITCHPAD_URL", "http://switchpad.local").rstrip("/")
PAD_TOKEN = os.environ.get("SWITCHPAD_TOKEN", "") or _token_from_config_h()
CAPTURE_NAME = os.environ.get("CAPTURE_NAME", "")  # デバイス名の一部（番号より確実）
CAPTURE_DEVICE = int(os.environ.get("CAPTURE_DEVICE", "0"))
CAPTURE_WIDTH = int(os.environ.get("CAPTURE_WIDTH", "1920"))
CAPTURE_HEIGHT = int(os.environ.get("CAPTURE_HEIGHT", "1080"))
CAPTURE_FOURCC = os.environ.get("CAPTURE_FOURCC", "YUY2")

# 最初のフレームが来るまで待つ上限（秒）
FIRST_FRAME_TIMEOUT = 10.0

# 常駐サーバー（キャプチャーの持ち主）が待ち受けるアドレス。/mcp もここで出す
SHARE_HOST = "127.0.0.1"
SHARE_PORT = int(os.environ.get("CAPTURE_SHARE_PORT", "8765"))

# stateless: 常駐サーバーを再起動しても、クライアントがセッションを張り直さずに済む
# json_response: サーバーから通知を送ることはないので、SSE ではなく JSON で返す
mcp = FastMCP(
    "switchpad",
    host=SHARE_HOST,
    port=SHARE_PORT,
    stateless_http=True,
    json_response=True,
)


def _log(message: str) -> None:
    """stderr に出すログ。Claude Desktop の MCP ログに表示される"""
    print(f"[switchpad] {message}", file=sys.stderr, flush=True)


def _list_device_names() -> list[str]:
    """Windows のカメラ名を、OpenCV の番号と同じ順で返す（Windows 以外は空）"""
    if sys.platform != "win32":
        return []
    try:
        from pygrabber.dshow_graph import FilterGraph

        return list(FilterGraph().get_input_devices())
    except Exception as exc:
        _log(f"device name listing failed: {exc}")
        return []


def _resolve_device_index() -> int:
    """CAPTURE_NAME が設定されていれば名前で番号を探し、なければ CAPTURE_DEVICE を使う"""
    if not CAPTURE_NAME:
        return CAPTURE_DEVICE
    names = _list_device_names()
    for index, name in enumerate(names):
        if CAPTURE_NAME.lower() in name.lower():
            _log(f"capture device '{name}' -> index {index}")
            return index
    _log(f"capture device name '{CAPTURE_NAME}' not found in {names}; falling back to {CAPTURE_DEVICE}")
    return CAPTURE_DEVICE


# ---------------------------------------------------------------
# コントローラー（ESP32-S3）との通信
# ---------------------------------------------------------------


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if PAD_TOKEN:
        headers["Authorization"] = f"Bearer {PAD_TOKEN}"
    return headers


def _pad_get(path: str) -> dict[str, Any]:
    with httpx.Client(timeout=5.0) as client:
        res = client.get(f"{PAD_URL}{path}", headers=_headers())
        res.raise_for_status()
        return res.json()


def _pad_post(path: str, body: Any) -> dict[str, Any]:
    with httpx.Client(timeout=5.0) as client:
        res = client.post(f"{PAD_URL}{path}", headers=_headers(), json=body)
        res.raise_for_status()
        return res.json()


# ---------------------------------------------------------------
# 配信用のイベントログ（操作と LLM のコメント）
#
# 持ち主プロセス（キャプチャーサーバー）が保持し、/view と /overlay に表示する。
# MCP サーバーが別プロセスのときは HTTP で持ち主に送る
# ---------------------------------------------------------------

import collections
import json as _json

EVENT_LOG_SIZE = 50
_events: collections.deque[dict[str, Any]] = collections.deque(maxlen=EVENT_LOG_SIZE)
_events_lock = threading.Lock()
_event_seq = 0


def _append_event(kind: str, text: str, data: Any = None) -> dict[str, Any]:
    """イベントをログに追加する（持ち主プロセス内で呼ぶ）"""
    global _event_seq
    with _events_lock:
        _event_seq += 1
        event = {"id": _event_seq, "t": time.time(), "kind": kind, "text": text, "data": data}
        _events.append(event)
        return event


def _recent_events(after_id: int = 0) -> list[dict[str, Any]]:
    with _events_lock:
        return [e for e in _events if e["id"] > after_id]


def _post_event(kind: str, text: str, data: Any = None) -> dict[str, Any] | None:
    """イベントを記録する。持ち主なら直接、そうでなければ HTTP で送る。失敗しても操作は止めない。
    持ち主のときは記録したイベントを返す（say の wait で使う）"""
    if _share_server is not None:
        return _append_event(kind, text, data)
    try:
        with httpx.Client(timeout=2.0) as client:
            client.post(
                f"http://{SHARE_HOST}:{SHARE_PORT}/event",
                json={"kind": kind, "text": text, "data": data},
            )
    except (httpx.HTTPError, OSError) as exc:
        _log(f"event post failed: {exc}")
    return None


_BUTTON_NAMES = {
    "b1": "A", "b2": "B", "b3": "X", "b4": "Y", "b5": "L", "b6": "R", "b7": "−", "b8": "+",
    "b9": "LS", "b10": "RS", "b11": "ZL", "b12": "ZR", "b13": "HOME", "b14": "CAP",
}
_HAT_ARROWS = {0: "↑", 45: "↗", 90: "→", 135: "↘", 180: "↓", 225: "↙", 270: "←", 315: "↖"}


def _describe_inputs(inputs: dict[str, Any]) -> str:
    """x360ce 形式の入力を、画面に出す短い文字列にする（例: "↓↘→+X"）"""
    parts: list[str] = []
    if inputs.get("rest") == 1:
        parts.append("rest")
    for key, value in inputs.items():
        if key in ("rest", "t", "ms"):
            continue
        if key in _BUTTON_NAMES:
            if value:
                parts.append(_BUTTON_NAMES[key])
        elif key == "p1":
            try:
                deg = int(value)
            except (TypeError, ValueError):
                continue
            if deg >= 0:
                parts.append(_HAT_ARROWS.get(((deg % 360) + 22) // 45 * 45 % 360, f"hat{deg}"))
        elif key in ("a1", "a2", "a4", "a5"):
            try:
                v = float(value)
            except (TypeError, ValueError):
                continue
            if v != 0:
                stick = "L" if key in ("a1", "a2") else "R"
                axis = "x" if key in ("a1", "a4") else "y"
                parts.append(f"{stick}{axis}{v:+.1f}")
        elif key in ("a3", "a6"):
            try:
                if float(value) > 0.5:
                    parts.append("ZL" if key == "a3" else "ZR")
            except (TypeError, ValueError):
                continue
    return "+".join(parts) if parts else "（なし）"


def _describe_sequence(steps: list[dict[str, Any]]) -> str:
    """入力列を "↓ ↘ →+X" のように、押した瞬間だけ並べて表示する"""
    shown: list[str] = []
    for step in steps:
        pressed = {k: v for k, v in step.items() if k != "t" and v not in (0, -1, "0")}
        if not pressed or pressed == {"rest": 1}:
            continue
        shown.append(_describe_inputs(pressed))
    return " ".join(shown) if shown else "（離す）"


# ツールはすべて async にして、通信や待ち時間はスレッドで動かす。
# 常駐サーバーでは HTTP（ライブ表示の MJPEG など）と同じイベントループで動くので、
# 同期のまま sleep するとその間ほかの応答が全部止まってしまう
from anyio import sleep as _async_sleep
from anyio.to_thread import run_sync as _in_thread


def _reading_seconds(text: str) -> float:
    """読み上げがないとき、吹き出しを読み終わるまでの目安（1 秒 7 文字 ＋ 1 秒、2〜15 秒）"""
    return max(2.0, min(15.0, len(text) / 7 + 1))


@mcp.tool()
async def say(text: str, wait: bool = False) -> dict[str, Any]:
    """配信画面にコメント（今考えていること・次にやること）を表示する。

    例: say("キャラセレクトに戻ってリュウを選びます")
    操作の前に一言添えると、見ている人に意図が伝わる。短めに。

    wait=True にすると、読み上げ（VOICEVOX）が終わるまで待ってから返る。
    読み上げがオフのときは、文字数から見積もった時間だけ待つ。
    say を続けて言わせたいとき、次の操作の前に言い終わってほしいときに使う
    （別途 sleep を挟む必要はない）。
    """
    event = await _in_thread(_post_event, "comment", text)
    done = _take_speech_done(event["id"]) if event else None
    if not wait:
        return {"ok": True}
    if done is not None:
        finished = await _in_thread(done.wait, 60.0)
        return {"ok": True, "waited": "speech", "finished": finished}
    seconds = _reading_seconds(text)
    await _async_sleep(seconds)
    return {"ok": True, "waited": "estimate", "seconds": round(seconds, 1)}


def _status_sync() -> dict[str, Any]:
    result = _pad_get("/")
    result["capture"] = _capture_status()
    return result


@mcp.tool()
async def status() -> dict[str, Any]:
    """コントローラーの状態（IP、USB の準備状況、今の入力）と、キャプチャーの状態を返す"""
    return await _in_thread(_status_sync)


@mcp.tool()
async def read() -> dict[str, Any]:
    """今の入力状態を {"State":..., "Now":..., "Error":...} の形式で返す"""
    return await _in_thread(_pad_get, "/read")


def _send(path: str, body: Any, description: str) -> dict[str, Any]:
    """ESP32 に送って、操作ログにも残す"""
    result = _pad_post(path, body)
    _post_event("input", description, body)
    return result


@mcp.tool()
async def set_state(state: dict[str, Any]) -> dict[str, Any]:
    """入力をセットして、次に変えるまで保持する（x360ce と同じ形式）。

    例: {"rest":1, "b1":1, "a1":0.5, "p1":90}
    名前: b1=A b2=B b3=X b4=Y b5=L b6=R b7=Minus b8=Plus b9=LStick b10=RStick
          b11=ZL b12=ZR b13=Home b14=Capture（1 で押す、0 で離す）
          a1/a2=左スティック X/Y、a4/a5=右スティック X/Y（-1〜1、右と上がプラス）
          p1=十字キーの角度（0=上 90=右 180=下 270=左、-1 でニュートラル）
          rest=1 で先にすべてをニュートラルに戻す
    離し忘れに注意。操作が終わったら {"rest":1} を送る。
    """
    return await _in_thread(_send, "/set_state", state, f"hold {_describe_inputs(state)}")


@mcp.tool()
async def press(inputs: dict[str, Any], ms: int = 100) -> dict[str, Any]:
    """ボタンや十字キーを ms ミリ秒だけ押して、自動で離す。

    例: press({"b1":1}) で A を 100ms 押す。press({"p1":180}, 150) で十字キーの下を 150ms。
    複数を同時に押すこともできる: press({"b5":1, "b6":1})
    """
    body = dict(inputs)
    body["ms"] = ms
    return await _in_thread(_send, "/press", body, _describe_inputs(inputs))


@mcp.tool()
async def sequence(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """タイミング付きの入力列を、コントローラー側で正確に実行する。

    各ステップは t（開始からのミリ秒）と、そのとき適用する入力を持つ。
    例: [{"t":0,"b1":1},{"t":80,"b1":0},{"t":300,"p1":0},{"t":400,"p1":-1}]
    最後は必ず離す指定（0 や -1、または {"rest":1}）で終えること。最大 64 ステップ。
    """
    return await _in_thread(_send, "/sequence", steps, _describe_sequence(steps))


@mcp.tool()
async def rest() -> dict[str, Any]:
    """すべての入力をニュートラルに戻す"""
    return await _in_thread(_send, "/set_state", {"rest": 1}, "rest")


# ---------------------------------------------------------------
# キャプチャーボード（裏で常に読み続けて、最新の 1 枚を保持する）
# ---------------------------------------------------------------


class FrameGrabber:
    """キャプチャーボードを別スレッドで読み続け、最新フレームを保持する"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: Any = None
        self._frame_at = 0.0
        self._frame_count = 0
        self._error = ""
        self._actual_size = ""
        self._index = CAPTURE_DEVICE
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="frame-grabber", daemon=True)
        self._thread.start()

    def _open(self) -> Any:
        import cv2

        backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
        self._index = _resolve_device_index()
        cap = cv2.VideoCapture(self._index, backend)
        if not cap.isOpened():
            raise RuntimeError(f"キャプチャーボード（番号 {self._index}）を開けませんでした")
        # 形式 → 解像度の順に設定する（逆だと効かないデバイスがある）
        if CAPTURE_FOURCC:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*CAPTURE_FOURCC))
        if CAPTURE_WIDTH and CAPTURE_HEIGHT:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAPTURE_WIDTH)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAPTURE_HEIGHT)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._actual_size = (
            f"{int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}"
        )
        _log(f"capture opened: device={self._index} {self._actual_size}")
        return cap

    def _run(self) -> None:
        cap = None
        while True:
            try:
                if cap is None:
                    cap = self._open()
                ok, frame = cap.read()
                if not ok:
                    raise RuntimeError("フレームを読めませんでした")
                with self._lock:
                    self._frame = frame
                    self._frame_at = time.time()
                    self._frame_count += 1
                    self._error = ""
            except Exception as exc:  # 失敗したら少し待って開き直す
                with self._lock:
                    self._error = str(exc)
                _log(f"capture error: {exc}")
                if cap is not None:
                    cap.release()
                    cap = None
                time.sleep(2.0)

    def latest(self, timeout: float = FIRST_FRAME_TIMEOUT) -> Any:
        """最新フレームを返す。まだ 1 枚もなければ timeout 秒まで待つ"""
        self.start()
        deadline = time.time() + timeout
        while True:
            with self._lock:
                if self._frame is not None:
                    return self._frame.copy()
                error = self._error
            if time.time() > deadline:
                raise RuntimeError(f"キャプチャーボードから映像が来ません: {error or 'timeout'}")
            time.sleep(0.05)

    def info(self) -> dict[str, Any]:
        with self._lock:
            age = time.time() - self._frame_at if self._frame is not None else None
            return {
                "device": self._index,
                "device_name": CAPTURE_NAME,
                "size": self._actual_size,
                "frames": self._frame_count,
                "last_frame_age_s": round(age, 2) if age is not None else None,
                "error": self._error,
            }


_grabber = FrameGrabber()

# ---------------------------------------------------------------
# キャプチャーの持ち主を 1 プロセスに決める
#
# Claude Desktop は MCP サーバーを複数回起動するが、キャプチャーボードは
# 1 プロセスしか開けない。そこでローカルのポートを取れたプロセスが「持ち主」になり、
# ほかのプロセスはそこから HTTP で最新フレームをもらう
# ---------------------------------------------------------------

_share_server: Any = None  # 持ち主になったら uvicorn.Server が入る


def _frame_to_jpeg_bytes(frame: Any, quality: int = 90) -> bytes:
    import cv2

    ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("JPEG のエンコードに失敗しました")
    return encoded.tobytes()


# ライブ表示ページ。__WITH_VIDEO__ が true なら映像つき（/view）、false なら透明オーバーレイ（/overlay）
_VIEW_HTML = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><title>Switch Live</title>
<style>
  html, body { margin: 0; height: 100%; overflow: hidden; font-family: "Segoe UI", "Hiragino Sans", "Noto Sans JP", sans-serif; }
  body.video { background: #000; }
  body.overlay { background: transparent; }
  #video { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; }
  /* LLM のコメント（上の吹き出し） */
  #comment { position: absolute; top: 3vh; left: 50%; transform: translateX(-50%);
    max-width: 80vw; padding: 1.2vh 2.4vw; border-radius: 2vh;
    background: rgba(255,255,255,.92); color: #222; font-size: 2.6vh; line-height: 1.5;
    box-shadow: 0 .4vh 2vh rgba(0,0,0,.35); opacity: 0; transition: opacity .3s; }
  #comment.show { opacity: 1; }
  #comment::before { content: "🤖"; margin-right: .6em; }
  /* 操作ログ（左下） */
  #inputs { position: absolute; left: 2vw; bottom: 3vh; display: flex; flex-direction: column-reverse; gap: .8vh; }
  .input { padding: .6vh 1.4vw; border-radius: 1vh; background: rgba(0,0,0,.65); color: #fff;
    font-size: 2.4vh; font-weight: 600; letter-spacing: .05em; opacity: .55; transition: opacity .2s;
    border-left: .5vw solid #7c3aed; }
  .input.latest { opacity: 1; background: rgba(124,58,237,.85); }
  .input small { opacity: .7; font-weight: 400; margin-left: .8em; font-size: .75em; }
</style></head>
<body>
<img id="video" src="/stream" alt="">
<div id="comment"></div>
<div id="inputs"></div>
<script>
  const withVideo = __WITH_VIDEO__;
  document.body.className = withVideo ? "video" : "overlay";
  if (!withVideo) document.getElementById("video").remove();

  const MAX_INPUTS = 6;          // 画面に残す操作ログの数
  const COMMENT_HOLD_MS = 15000; // コメントを表示し続ける時間
  let lastId = 0;
  let commentTimer = null;

  // ?tts=1 を付けて開くと、コメントをブラウザの音声合成で読み上げる
  const ttsEnabled = new URLSearchParams(location.search).get("tts") === "1";
  function speak(text) {
    if (!ttsEnabled || !("speechSynthesis" in window)) return;
    const utter = new SpeechSynthesisUtterance(text.replace(/[\\p{Emoji_Presentation}\\p{Extended_Pictographic}]/gu, ""));
    utter.lang = "ja-JP";
    const voice = speechSynthesis.getVoices().find(v => v.lang === "ja-JP");
    if (voice) utter.voice = voice;
    utter.rate = 1.1;
    speechSynthesis.cancel();   // 前のコメントを読み終えていなければ打ち切る
    speechSynthesis.speak(utter);
  }

  function showComment(text) {
    const el = document.getElementById("comment");
    el.textContent = text;
    el.classList.add("show");
    clearTimeout(commentTimer);
    commentTimer = setTimeout(() => el.classList.remove("show"), COMMENT_HOLD_MS);
    speak(text);
  }

  function addInput(text, when) {
    const box = document.getElementById("inputs");
    for (const el of box.querySelectorAll(".latest")) el.classList.remove("latest");
    const el = document.createElement("div");
    el.className = "input latest";
    const time = new Date(when * 1000).toLocaleTimeString("ja-JP", { hour12: false });
    el.innerHTML = `<span></span><small></small>`;
    el.firstChild.textContent = text;
    el.lastChild.textContent = time;
    box.prepend(el);
    while (box.children.length > MAX_INPUTS) box.lastChild.remove();
  }

  async function poll() {
    try {
      const res = await fetch(`/events?after=${lastId}`, { cache: "no-store" });
      const events = await res.json();
      for (const ev of events) {
        lastId = Math.max(lastId, ev.id);
        if (ev.kind === "comment") showComment(ev.text);
        else addInput(ev.text, ev.t);
      }
    } catch (e) { /* サーバー再起動中などは無視 */ }
    setTimeout(poll, 400);
  }
  poll();
</script>
</body></html>
"""


def _build_http_app() -> Any:
    """常駐サーバーの HTTP アプリ。/mcp（Streamable HTTP の MCP）とライブ表示を同じポートで出す"""
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
    from starlette.routing import Route

    def page(with_video: bool) -> Any:
        body = _VIEW_HTML.replace("__WITH_VIDEO__", "true" if with_video else "false")

        async def handler(_request: Request) -> Response:
            return HTMLResponse(body)

        return handler

    async def stream(_request: Request) -> Response:
        """MJPEG で流し続ける（ブラウザでライブ表示する用）。ブラウザが閉じたら止まる"""

        async def frames() -> Any:
            while True:
                body = await _in_thread(lambda: _frame_to_jpeg_bytes(_grabber.latest(), quality=80))
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode()
                    + body
                    + b"\r\n"
                )
                await _async_sleep(1 / 30)

        return StreamingResponse(
            frames(),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-cache"},
        )

    async def frame_jpg(_request: Request) -> Response:
        try:
            body = await _in_thread(lambda: _frame_to_jpeg_bytes(_grabber.latest()))
        except Exception as exc:
            return PlainTextResponse(str(exc), status_code=503)
        return Response(body, media_type="image/jpeg")

    async def info(_request: Request) -> Response:
        body = _grabber.info()
        body["voice"] = _voicevox.enabled  # 読み上げが動いているか（make voice が待つのに使う）
        return JSONResponse(body)

    async def events(request: Request) -> Response:
        try:
            after = int(request.query_params.get("after", "0"))
        except ValueError:
            after = 0
        body = _json.dumps(_recent_events(after), ensure_ascii=False)
        return Response(body, media_type="application/json; charset=utf-8", headers={"Cache-Control": "no-cache"})

    async def post_event(request: Request) -> Response:
        """操作やコメントを記録する（stdio で動いている MCP サーバーから送られてくる）"""
        try:
            payload = await request.json()
            event = await _in_thread(
                _append_event,
                str(payload.get("kind", "comment")),
                str(payload.get("text", "")),
                payload.get("data"),
            )
        except Exception as exc:
            return PlainTextResponse(str(exc), status_code=400)
        return Response(_json.dumps(event, ensure_ascii=False), media_type="application/json; charset=utf-8")

    # streamable_http_app() は /mcp のルートと、セッション管理の lifespan を持った Starlette アプリ。
    # そこにライブ表示などのルートを足す
    app = mcp.streamable_http_app()
    app.router.routes.extend(
        [
            Route("/", page(True)),
            Route("/view", page(True)),
            Route("/overlay", page(False)),  # OBS のブラウザソース用（背景透明）
            Route("/stream", stream),
            Route("/frame.jpg", frame_jpg),
            Route("/info", info),
            Route("/events", events),
            Route("/event", post_event, methods=["POST"]),
        ]
    )
    return app


def _try_become_owner() -> bool:
    """共有ポートを取れたら、このプロセスがキャプチャーの持ち主になる"""
    global _share_server
    import socket

    import uvicorn

    # 先にソケットを取ってみる。取れなければ別の持ち主がいる
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((SHARE_HOST, SHARE_PORT))
    except OSError:
        sock.close()
        return False

    config = uvicorn.Config(_build_http_app(), log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    # 別スレッドで動かす（メインスレッドは stdio の MCP や状態表示に使う）
    threading.Thread(target=server.run, kwargs={"sockets": [sock]}, name="http", daemon=True).start()
    _share_server = server
    _grabber.start()
    _log(f"capture owner: serving on http://{SHARE_HOST}:{SHARE_PORT}/ (MCP: /mcp)")
    return True


def _fetch_frame_from_owner() -> Any:
    """持ち主プロセスから最新フレームをもらう"""
    import cv2
    import numpy as np

    with httpx.Client(timeout=FIRST_FRAME_TIMEOUT + 2) as client:
        res = client.get(f"http://{SHARE_HOST}:{SHARE_PORT}/frame.jpg")
        if res.status_code != 200:
            raise RuntimeError(f"持ち主プロセスからの応答: {res.status_code} {res.text}")
        frame = cv2.imdecode(np.frombuffer(res.content, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise RuntimeError("持ち主プロセスからの画像をデコードできませんでした")
    return frame


def _latest_frame() -> Any:
    """持ち主ならキャプチャーボードから、そうでなければ持ち主から最新フレームを取る"""
    if _share_server is not None:
        return _grabber.latest()
    try:
        return _fetch_frame_from_owner()
    except (httpx.HTTPError, OSError) as exc:
        if os.environ.get("CAPTURE_EXTERNAL", "") == "1":
            raise RuntimeError(
                "キャプチャーサーバーが動いていません。PowerShell で "
                "`uv run switchpad_mcp.py --capture-server` を起動してください"
            ) from exc
        # 持ち主が終了していたら、自分が持ち主になることを試みる
        if _try_become_owner():
            return _grabber.latest()
        raise


def _capture_status() -> dict[str, Any]:
    if _share_server is not None:
        info = _grabber.info()
        info["role"] = "owner"
        return info
    try:
        with httpx.Client(timeout=3.0) as client:
            info = client.get(f"http://{SHARE_HOST}:{SHARE_PORT}/info").json()
        info["role"] = "client"
        return info
    except (httpx.HTTPError, OSError) as exc:
        return {"role": "client", "error": f"持ち主プロセスに接続できません: {exc}"}


def _encode(frame: Any, max_width: int, crop: list[int] | None) -> Image:
    import cv2
    from PIL import Image as PILImage

    if crop and len(crop) == 4:
        x, y, w, h = (int(v) for v in crop)
        frame = frame[y : y + h, x : x + w]
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    img = PILImage.fromarray(rgb)
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)), PILImage.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return Image(data=buf.getvalue(), format="jpeg")


@mcp.tool()
async def screenshot(max_width: int = 640, crop: list[int] | None = None) -> Image:
    """キャプチャーボードから Switch の画面を 1 枚撮って返す。

    max_width: 返す画像の最大幅。640 より大きいと転送で止まることがあるので、
               細かく見たいときは crop で一部を切り出す
    crop: [x, y, w, h] を元画像（1920x1080）の座標で指定すると、その範囲だけを切り出す
    """
    return await _in_thread(lambda: _encode(_latest_frame(), max_width, crop))


@mcp.tool()
async def press_and_look(
    inputs: dict[str, Any], ms: int = 100, wait_ms: int = 600, max_width: int = 640
) -> Image:
    """ボタンを押して、少し待ってから画面を撮る（メニュー操作を 1 回で済ませる用）。

    inputs と ms は press と同じ。wait_ms は押してから撮影するまでの待ち時間。
    """
    await press(inputs, ms)
    await _async_sleep((ms + wait_ms) / 1000.0)
    return await screenshot(max_width=max_width)


# ---------------------------------------------------------------
# VOICEVOX による読み上げと、Docker コンテナの管理
#
# キャプチャーサーバー（持ち主）で動く。コメントのイベントが来たら
# VOICEVOX の API で WAV を作り、スピーカーで再生する。
# --voicevox-docker cpu|gpu を付けると、コンテナの起動と終了も面倒を見る
# ---------------------------------------------------------------

import atexit
import queue
import signal
import subprocess

VOICEVOX_URL = os.environ.get("VOICEVOX_URL", "http://127.0.0.1:50021").rstrip("/")
VOICEVOX_SPEAKER = int(os.environ.get("VOICEVOX_SPEAKER", "3"))  # 3 = ずんだもん（ノーマル）
VOICEVOX_CONTAINER = os.environ.get("VOICEVOX_CONTAINER", "switchpad-voicevox")
VOICEVOX_IMAGES = {
    "cpu": "voicevox/voicevox_engine:cpu-latest",
    "gpu": "voicevox/voicevox_engine:nvidia-latest",
}


class VoicevoxSpeaker:
    """コメントを順番に読み上げるワーカー。重ならないよう 1 つずつ再生する"""

    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[str, threading.Event]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self.enabled = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self.enabled = True
        self._thread = threading.Thread(target=self._run, name="voicevox", daemon=True)
        self._thread.start()

    def say(self, text: str) -> threading.Event | None:
        """読み上げ待ちの列に入れる。読み終わったら（または捨てられたら）立つ Event を返す。
        読み上げが無効なら None"""
        if not self.enabled:
            return None
        # 読み上げが追いつかないときは古いものを捨てて、最新を優先する。
        # 捨てた文を待っている人が止まらないよう、その Event は立てておく
        while self._queue.qsize() >= 3:
            try:
                _, dropped = self._queue.get_nowait()
                dropped.set()
            except queue.Empty:
                break
        done = threading.Event()
        self._queue.put((text, done))
        return done

    def _run(self) -> None:
        while True:
            text, done = self._queue.get()
            try:
                wav = self._synthesize(text)
                self._play(wav)  # 再生が終わるまでブロックする
            except Exception as exc:
                _log(f"voicevox error: {exc}")
            finally:
                done.set()

    @staticmethod
    def _strip_emoji(text: str) -> str:
        return "".join(ch for ch in text if ord(ch) < 0x1F000 and ch not in "✨💖🔥")

    def _synthesize(self, text: str) -> bytes:
        text = self._strip_emoji(text).strip()
        if not text:
            return b""
        with httpx.Client(timeout=30.0) as client:
            query = client.post(
                f"{VOICEVOX_URL}/audio_query",
                params={"text": text, "speaker": VOICEVOX_SPEAKER},
            )
            query.raise_for_status()
            audio = client.post(
                f"{VOICEVOX_URL}/synthesis",
                params={"speaker": VOICEVOX_SPEAKER},
                json=query.json(),
            )
            audio.raise_for_status()
            return audio.content

    @staticmethod
    def _play(wav: bytes) -> None:
        if not wav:
            return
        if sys.platform == "win32":
            import winsound  # Windows 標準。追加のライブラリ不要

            winsound.PlaySound(wav, winsound.SND_MEMORY)  # 再生が終わるまでブロックする
        else:
            # Windows 以外は aplay / afplay に任せる（開発用）
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                f.write(wav)
            player = "afplay" if sys.platform == "darwin" else "aplay"
            subprocess.run([player, f.name], check=False, capture_output=True)
            os.unlink(f.name)


_voicevox = VoicevoxSpeaker()


def _voicevox_ready(timeout: float = 2.0) -> bool:
    try:
        with httpx.Client(timeout=timeout) as client:
            return client.get(f"{VOICEVOX_URL}/version").status_code == 200
    except (httpx.HTTPError, OSError):
        return False


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


def _voicevox_container_state() -> str:
    """"running" / "exited" / "" （存在しない）"""
    res = _docker("inspect", "-f", "{{.State.Status}}", VOICEVOX_CONTAINER, check=False)
    return res.stdout.strip() if res.returncode == 0 else ""


def _start_voicevox_container(kind: str) -> None:
    """VOICEVOX のコンテナを起動する。すでに動いていればそれを使う"""
    image = VOICEVOX_IMAGES[kind]
    state = _voicevox_container_state()
    if state == "running":
        _log(f"voicevox container '{VOICEVOX_CONTAINER}' is already running; reusing it")
    elif state:
        _log(f"voicevox container '{VOICEVOX_CONTAINER}' exists ({state}); starting it")
        _docker("start", VOICEVOX_CONTAINER)
    else:
        _log(f"starting voicevox container from {image} (first time may take a while to pull)")
        args = ["run", "-d", "--name", VOICEVOX_CONTAINER, "-p", "127.0.0.1:50021:50021"]
        if kind == "gpu":
            args += ["--gpus", "all"]
        _docker(*args, image)

    # API が応答するまで待つ（モデルの読み込みに少しかかる）
    deadline = time.time() + 120
    while time.time() < deadline:
        if _voicevox_ready():
            _log("voicevox is ready")
            return
        time.sleep(1.0)
    raise RuntimeError("VOICEVOX が起動しませんでした。docker logs " + VOICEVOX_CONTAINER + " を確認してください")


_voicevox_stopped = False


def _stop_voicevox_container() -> None:
    """コンテナを止める。何度呼ばれても 1 回しか実行しない"""
    global _voicevox_stopped
    if _voicevox_stopped:
        return
    _voicevox_stopped = True
    print("VOICEVOX のコンテナを停止しています…", flush=True)
    _docker("stop", "-t", "5", VOICEVOX_CONTAINER, check=False)
    _docker("rm", VOICEVOX_CONTAINER, check=False)


def _install_shutdown_hooks(on_shutdown: Any) -> None:
    """Ctrl+C / Ctrl+Break / ウィンドウを閉じる / 通常終了 のすべてで on_shutdown を呼ぶ。

    注意: Windows の Stop-Process やタスクマネージャーの「タスクの終了」は
    プロセスを即座に殺すので、どのフックも動かない。
    """
    atexit.register(on_shutdown)

    def handler(_signum: int, _frame: Any) -> None:
        on_shutdown()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    if hasattr(signal, "SIGBREAK"):  # Windows の Ctrl+Break
        signal.signal(signal.SIGBREAK, handler)

    if sys.platform == "win32":
        # コンソールウィンドウを閉じたとき（CTRL_CLOSE_EVENT）は signal では捕まえられないので、
        # Win32 API の SetConsoleCtrlHandler で捕まえる
        import ctypes

        HandlerRoutine = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

        def console_handler(event: int) -> bool:
            if event in (2, 5, 6):  # CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT
                on_shutdown()
            return False  # 既定の処理（終了）も続けてもらう

        global _console_handler_ref
        _console_handler_ref = HandlerRoutine(console_handler)  # GC されないよう参照を保持
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler_ref, True)


_console_handler_ref: Any = None


# PID ファイル（--stop で止めるために使う）
PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".switchpad-capture.pid")


def _write_pid_file() -> None:
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))


def _remove_pid_file() -> None:
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


def _stop_running_server() -> None:
    """--stop: PID ファイルのプロセスを止めて、コンテナも落とす"""
    try:
        with open(PID_FILE, encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        pid = 0
    if pid:
        print(f"キャプチャーサーバー（PID {pid}）を止めます", flush=True)
        if sys.platform == "win32":
            # taskkill は即死させるので、先にコンテナを止めてから殺す
            _stop_voicevox_container()
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=False, capture_output=True)
        else:
            os.kill(pid, signal.SIGTERM)
        _remove_pid_file()
    else:
        print("PID ファイルがありません。コンテナだけ確認します", flush=True)
    if _voicevox_container_state():
        _stop_voicevox_container()
    print("完了", flush=True)


# コメントのイベントが来たら読み上げる（持ち主プロセスだけ）
_original_append_event = _append_event


# コメントのイベント ID → 読み終わったら立つ Event。say(wait=True) が待つのに使う
_speech_done: dict[int, threading.Event] = {}
_speech_done_lock = threading.Lock()


def _append_event(kind: str, text: str, data: Any = None) -> dict[str, Any]:  # noqa: F811
    event = _original_append_event(kind, text, data)
    if kind == "comment":
        done = _voicevox.say(text)
        if done is not None:
            with _speech_done_lock:
                _speech_done[event["id"]] = done
                # 誰も待たなかった分（/event 経由など）がたまらないよう、古いものから捨てる
                while len(_speech_done) > 50:
                    _speech_done.pop(next(iter(_speech_done)))
    return event


def _take_speech_done(event_id: int) -> threading.Event | None:
    with _speech_done_lock:
        return _speech_done.pop(event_id, None)


# ---------------------------------------------------------------
# コマンドライン
# ---------------------------------------------------------------


def _probe_cameras() -> None:
    """0〜9 番のカメラを順に開いて、使えるものを表示する"""
    import cv2

    backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
    names = _list_device_names()
    print("使えるカメラを探しています…")
    for index in range(10):
        cap = cv2.VideoCapture(index, backend)
        if cap.isOpened():
            ok, frame = cap.read()
            size = f"{frame.shape[1]}x{frame.shape[0]}" if ok else "映像なし"
            name = names[index] if index < len(names) else "(名前不明)"
            print(f"  番号 {index}: {name}（{size}）")
            cap.release()
    print("キャプチャーボードの名前の一部を CAPTURE_NAME に設定してください（番号は起動ごとに変わることがあります）")


def _test_capture() -> None:
    """キャプチャーボードを開いて数枚読み、test.jpg に保存する（切り分け用）"""
    import cv2

    backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
    index = _resolve_device_index()
    print(f"番号 {index} を開きます（{CAPTURE_FOURCC or '既定'} / {CAPTURE_WIDTH}x{CAPTURE_HEIGHT}）…", flush=True)
    t0 = time.time()
    cap = cv2.VideoCapture(index, backend)
    print(f"  open: {cap.isOpened()} ({time.time() - t0:.1f}s)", flush=True)
    if not cap.isOpened():
        return
    if CAPTURE_FOURCC:
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*CAPTURE_FOURCC))
    if CAPTURE_WIDTH and CAPTURE_HEIGHT:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, CAPTURE_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, CAPTURE_HEIGHT)
    print(
        f"  実際の解像度: {int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}",
        flush=True,
    )
    for i in range(10):
        t1 = time.time()
        ok, frame = cap.read()
        print(f"  read {i}: ok={ok} ({time.time() - t1:.2f}s)", flush=True)
        if ok and i == 9:
            cv2.imwrite("test.jpg", frame)
            print("  test.jpg に保存しました", flush=True)
    cap.release()


if __name__ == "__main__":
    if "--probe" in sys.argv:
        _probe_cameras()
    elif "--test" in sys.argv:
        _test_capture()
    elif "--stop" in sys.argv:
        _stop_running_server()
    elif "--capture-server" in sys.argv:
        # キャプチャーの持ち主だけを動かす（Claude Desktop の外で起動する用）
        #   --voicevox             起動済みの VOICEVOX（VOICEVOX_URL）で読み上げる
        #   --voicevox-docker cpu  VOICEVOX の Docker コンテナを起動して読み上げる（gpu も可）
        docker_kind = ""
        if "--voicevox-docker" in sys.argv:
            idx = sys.argv.index("--voicevox-docker")
            docker_kind = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else "cpu"
            if docker_kind not in VOICEVOX_IMAGES:
                print("--voicevox-docker には cpu か gpu を指定してください", flush=True)
                sys.exit(2)
        use_voicevox = docker_kind != "" or "--voicevox" in sys.argv

        if not _try_become_owner():
            print(f"ポート {SHARE_PORT} がすでに使われています。別の持ち主が動いているかも", flush=True)
            sys.exit(1)
        _write_pid_file()

        def shutdown() -> None:
            _remove_pid_file()
            if docker_kind:
                _stop_voicevox_container()

        _install_shutdown_hooks(shutdown)

        if docker_kind:
            _start_voicevox_container(docker_kind)
        if use_voicevox:
            if _voicevox_ready():
                _voicevox.start()
                print(f"VOICEVOX で読み上げます（speaker={VOICEVOX_SPEAKER}）", flush=True)
            else:
                print(f"VOICEVOX（{VOICEVOX_URL}）に接続できません。読み上げは無効です", flush=True)

        print("キャプチャーサーバーを起動しました", flush=True)
        print(f"  ライブ表示: http://{SHARE_HOST}:{SHARE_PORT}/  （ブラウザで開く）", flush=True)
        print(f"  OBS 用    : http://{SHARE_HOST}:{SHARE_PORT}/overlay", flush=True)
        print(f"  静止画    : http://{SHARE_HOST}:{SHARE_PORT}/frame.jpg", flush=True)
        print(f"  MCP       : http://{SHARE_HOST}:{SHARE_PORT}/mcp  （ESP32: {PAD_URL}）", flush=True)
        print("止めるときは Ctrl + C（または別の PowerShell で --stop）", flush=True)
        try:
            while True:
                time.sleep(5)
                info = _grabber.info()
                print(f"  frames={info['frames']} size={info['size']} error={info['error'] or '-'}", flush=True)
        except KeyboardInterrupt:
            pass
        finally:
            shutdown()
    else:
        _log(f"pad url: {PAD_URL}, capture: name='{CAPTURE_NAME}' device={CAPTURE_DEVICE}")
        # CAPTURE_EXTERNAL=1 のときは自分では開かず、--capture-server で動かした持ち主から画像をもらう
        if os.environ.get("CAPTURE_EXTERNAL", "") == "1":
            _log(f"capture client (external): will fetch frames from {SHARE_HOST}:{SHARE_PORT}")
        elif not _try_become_owner():
            _log(f"capture client: will fetch frames from {SHARE_HOST}:{SHARE_PORT}")
        mcp.run()