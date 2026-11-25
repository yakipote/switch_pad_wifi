# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "mcp>=1.2,<2",
#   "httpx>=0.27",
# ]
# ///
"""ollama_agent.py

ローカル LLM（Ollama）から switchpad の MCP ツールを使う小さなエージェント。
常駐サーバー（switchpad_mcp.py --capture-server）を先に起動しておくこと。

使い方
  uv run ollama_agent.py "今の画面を説明して"
  uv run ollama_agent.py --model gemma4:e4b "status を見て、say で挨拶して"
  uv run ollama_agent.py --allow-input "十字キーの右で、カーソルを 1 つ動かして"
  uv run ollama_agent.py --list-tools        モデルに渡すツールの一覧を出して終わる

ボタン入力のツール（press / sequence / set_state / rest / press_and_look）は、
--allow-input を付けたときだけモデルに渡す。
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from typing import Any

import anyio
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

INPUT_TOOLS = {"press", "sequence", "set_state", "rest", "press_and_look"}

SYSTEM_PROMPT = """\
あなたは Nintendo Switch 2 をツールで操作するアシスタントです。日本語で答えます。
画面はキャプチャーボードで見られます（screenshot / press_and_look が画像を返します）。

守ること:
- 操作の前に say で「これから何をするか」を 20〜40 字で一言いう。長い説明はしない
- 画面を見てから判断する。見えていないものを見えたことにしない。読めない文字は「読めない」と言う
- オンライン対戦・ランクマッチ・eShop での購入・セーブデータの削除や上書きの確認画面では、絶対にボタンを押さない
- ゲームの起動やセーブに関わる操作は、ユーザーに頼まれていなければしない
- 指示されたことが終わったら、ツールを呼ばずに結果を短く報告して終わる

ボタンの名前（press / press_and_look の inputs）:
  b1=A b2=B b3=X b4=Y b5=L b6=R b11=ZL b12=ZR b7=− b8=+ b13=HOME
  p1=十字キーの角度（0 上、90 右、180 下、270 左）
  例: press_and_look({"inputs": {"p1": 90}}) で十字キー右を押して画面を撮る
"""


def _log(text: str = "") -> None:
    print(text, flush=True)


def _short(value: Any, limit: int = 300) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def mcp_tool_to_ollama(tool: Any) -> dict[str, Any]:
    schema = dict(tool.inputSchema or {"type": "object", "properties": {}})
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    # pydantic が付ける title は要らない（小さいモデルが混乱しないように削る）
    schema.pop("title", None)
    for prop in schema["properties"].values():
        if isinstance(prop, dict):
            prop.pop("title", None)
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": (tool.description or "").strip(),
            "parameters": schema,
        },
    }


def split_result(result: Any) -> tuple[str, list[str]]:
    """MCP の CallToolResult を、テキストと base64 の画像の列に分ける"""
    texts: list[str] = []
    images: list[str] = []
    for item in result.content:
        if item.type == "text":
            texts.append(item.text)
        elif item.type == "image":
            images.append(item.data)  # MCP の画像はもともと base64
        else:
            texts.append(f"[{item.type}]")
    text = "\n".join(texts)
    if result.isError:
        text = "エラー: " + text
    if images and not text:
        text = f"（画像 {len(images)} 枚）"
    return text, images


async def ollama_chat(client: httpx.AsyncClient, args: argparse.Namespace, messages: list[dict[str, Any]],
                      tools: list[dict[str, Any]]) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": args.model,
        "messages": messages,
        "tools": tools,
        "stream": False,
        "options": {"num_ctx": args.num_ctx, "temperature": args.temperature},
    }
    if args.think is not None:
        body["think"] = args.think
    url = f"{args.ollama.rstrip('/')}/api/chat"
    for attempt in range(3):
        response = await client.post(url, json=body)
        if response.status_code == 200:
            return response.json()
        # モデルが崩れたツール呼び出しを出すと、Ollama がパースに失敗して 500 を返す
        # （qwen の "XML syntax error" など）。出力が揺れるので、少し温度を上げて投げ直す
        if response.status_code == 500 and attempt < 2:
            _log(f"（Ollama 500、投げ直す: {response.text[:200]}）")
            body["options"]["temperature"] = max(0.6, args.temperature)
            continue
        break
    raise RuntimeError(f"Ollama が {response.status_code} を返した: {response.text[:500]}")


async def run(args: argparse.Namespace) -> int:
    async with streamablehttp_client(args.mcp) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            tools = [t for t in listed.tools if args.allow_input or t.name not in INPUT_TOOLS]
            ollama_tools = [mcp_tool_to_ollama(t) for t in tools]

            if args.list_tools:
                for t in ollama_tools:
                    _log(f"- {t['function']['name']}: {_short(t['function']['description'], 80)}")
                hidden = sorted({t.name for t in listed.tools} - {t.name for t in tools})
                if hidden:
                    _log(f"（--allow-input が無いので渡さない: {', '.join(hidden)}）")
                return 0

            _log(f"モデル: {args.model} / ツール {len(tools)} 個: {', '.join(t.name for t in tools)}")
            _log(f"指示: {args.prompt}")
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": args.prompt},
            ]
            totals = {"prompt": 0, "eval": 0, "llm_sec": 0.0, "tool_sec": 0.0}
            started = time.monotonic()

            async with httpx.AsyncClient(timeout=args.timeout) as client:
                for step in range(1, args.max_steps + 1):
                    t0 = time.monotonic()
                    reply = await ollama_chat(client, args, messages, ollama_tools)
                    llm_sec = time.monotonic() - t0
                    totals["llm_sec"] += llm_sec
                    totals["prompt"] += reply.get("prompt_eval_count", 0) or 0
                    totals["eval"] += reply.get("eval_count", 0) or 0
                    message = reply.get("message", {})
                    calls = message.get("tool_calls") or []

                    _log(f"\n=== ステップ {step}（LLM {llm_sec:.1f} 秒、"
                         f"入力 {reply.get('prompt_eval_count', 0)} / 出力 {reply.get('eval_count', 0)} トークン）")
                    if message.get("thinking") and args.show_thinking:
                        _log(f"[考え] {_short(message['thinking'], 600)}")
                    if message.get("content"):
                        _log(f"[返答] {message['content'].strip()}")

                    assistant: dict[str, Any] = {"role": "assistant", "content": message.get("content", "")}
                    if calls:
                        assistant["tool_calls"] = calls
                    messages.append(assistant)

                    if not calls:
                        break

                    for call in calls:
                        fn = call.get("function", {})
                        name = fn.get("name", "")
                        arguments = fn.get("arguments") or {}
                        if isinstance(arguments, str):
                            try:
                                arguments = json.loads(arguments)
                            except json.JSONDecodeError:
                                arguments = {}
                        # 幅 640 を超えると返ってこないことがある（gemma は 1920 を指定してきた）
                        if isinstance(arguments.get("max_width"), (int, float)) and arguments["max_width"] > 640:
                            arguments["max_width"] = 640
                        t1 = time.monotonic()
                        if name not in {t.name for t in tools}:
                            text, images = f"エラー: {name} というツールは使えない", []
                        else:
                            try:
                                result = await session.call_tool(name, arguments)
                                text, images = split_result(result)
                            except Exception as exc:  # noqa: BLE001
                                text, images = f"エラー: {exc}", []
                        tool_sec = time.monotonic() - t1
                        totals["tool_sec"] += tool_sec
                        _log(f"[ツール] {name}({_short(arguments, 200)}) {tool_sec:.1f} 秒 → {_short(text, 200)}"
                             + (f" ＋画像 {len(images)} 枚" if images else ""))

                        tool_msg: dict[str, Any] = {"role": "tool", "tool_name": name, "content": text}
                        if images and args.image_mode == "tool":
                            tool_msg["images"] = images
                        messages.append(tool_msg)
                        if images and args.image_mode == "user":
                            messages.append({
                                "role": "user",
                                "content": f"（{name} が返した画面です）",
                                "images": images,
                            })
                        if images and args.save_images:
                            for i, data in enumerate(images):
                                path = f"{args.save_images}_{step}_{i}.jpg"
                                with open(path, "wb") as f:
                                    f.write(base64.b64decode(data))
                else:
                    _log(f"\n（--max-steps {args.max_steps} に達したので止める）")

            if args.allow_input:
                # 押しっぱなしで終わらないように、最後に必ず離す
                await session.call_tool("rest", {})

            elapsed = time.monotonic() - started
            _log(f"\n合計: {elapsed:.1f} 秒（LLM {totals['llm_sec']:.1f} 秒、ツール {totals['tool_sec']:.1f} 秒）、"
                 f"トークン 入力 {totals['prompt']} / 出力 {totals['eval']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Ollama から switchpad の MCP ツールを使う")
    parser.add_argument("prompt", nargs="?", default="", help="指示文")
    parser.add_argument("--model", default="qwen3.5:9b")
    parser.add_argument("--ollama", default="http://127.0.0.1:11434")
    parser.add_argument("--mcp", default="http://127.0.0.1:8765/mcp")
    parser.add_argument("--allow-input", action="store_true",
                        help="ボタン入力のツール（press / sequence / set_state / rest / press_and_look）も渡す")
    parser.add_argument("--max-steps", type=int, default=10)
    think = parser.add_mutually_exclusive_group()
    think.add_argument("--think", dest="think", action="store_true", default=None, help="思考を有効にする")
    think.add_argument("--no-think", dest="think", action="store_false", help="思考を切る（速くなる）")
    parser.add_argument("--show-thinking", action="store_true", help="思考の中身も表示する")
    parser.add_argument("--image-mode", choices=["tool", "user"], default="user",
                        help="画像の渡し方。tool: tool メッセージの images、user: 画像付きの user メッセージを足す")
    parser.add_argument("--num-ctx", type=int, default=16384)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--timeout", type=float, default=300.0, help="Ollama 1 回の待ち時間の上限（秒）")
    parser.add_argument("--save-images", metavar="PREFIX", help="受け取った画像を PREFIX_<ステップ>_<番号>.jpg に保存する")
    parser.add_argument("--list-tools", action="store_true", help="モデルに渡すツールを表示して終わる")
    args = parser.parse_args()
    if not args.prompt and not args.list_tools:
        parser.error("指示文を指定する")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    return anyio.run(run, args)


if __name__ == "__main__":
    sys.exit(main())
