// switch_pad_wifi.ino
// ESP32-S3 を HORI ポッ拳コントローラーとして Switch 2 に認識させ、
// Wi-Fi 経由の HTTP API で入力を操作するファームウェア
//
// Arduino IDE の設定（ツールメニュー）
//   ボード            : ESP32S3 Dev Module
//   USB Mode          : USB-OTG (TinyUSB)
//   USB CDC On Boot   : Disabled
//   Flash Size        : 16MB (128Mb)
//   PSRAM             : OPI PSRAM
//
// 必要なライブラリ（ライブラリマネージャからインストール）
//   ArduinoJson (7.x)  by Benoit Blanchon
//
// HTTP API（すべて JSON。認証は Authorization: Bearer <API_TOKEN>）
//   GET  /            → 状態の概要（IP、稼働時間、今の入力）
//   GET  /read        → 今の入力状態 {"State":{...},"Now":{...},"Error":""}
//   POST /set_state   → 入力をセットして保持する（x360ce と同じ形式）
//                       例: {"rest":1,"b1":1,"a1":0.5,"p1":90}
//   POST /press       → ボタンを指定ミリ秒だけ押して自動で離す
//                       例: {"b1":1,"ms":100}
//   POST /sequence    → タイミング付きの入力列を実行する（t はミリ秒の経過時間）
//                       例: [{"t":0,"b1":1},{"t":100,"b1":0},{"t":300,"p1":0},{"t":400,"p1":-1}]
//
// 入力名（x360ce 互換）
//   b1=A b2=B b3=X b4=Y b5=L b6=R b7=Minus b8=Plus b9=LStick b10=RStick
//   b11=ZL b12=ZR b13=Home b14=Capture（値は 1 で押す、0 で離す）
//   a1=左スティックX a2=左スティックY a4=右スティックX a5=右スティックY（-1〜1、上と右がプラス）
//   a3=ZL a6=ZR（0.5 より大きければ押す。Switch のトリガーはデジタルなので）
//   p1=十字キー（0=上、90=右、180=下、270=左、45 刻み。-1 でニュートラル）
//   rest=1 で、ほかの値を適用する前にすべてをニュートラルに戻す

#include <ArduinoJson.h>
#include <ESPmDNS.h>
#include <WebServer.h>
#include <WiFi.h>

#include "USB.h"
#include "USBHID.h"
#include "config.h"

// ============================================================
// USB HID（ポッ拳コントローラー）
// ============================================================

static const uint8_t kReportDescriptor[] = {
  0x05, 0x01,        // Usage Page (Generic Desktop)
  0x09, 0x05,        // Usage (Game Pad)
  0xA1, 0x01,        // Collection (Application)
  0x15, 0x00,        //   Logical Minimum (0)
  0x25, 0x01,        //   Logical Maximum (1)
  0x35, 0x00,        //   Physical Minimum (0)
  0x45, 0x01,        //   Physical Maximum (1)
  0x75, 0x01,        //   Report Size (1)
  0x95, 0x10,        //   Report Count (16)
  0x05, 0x09,        //   Usage Page (Button)
  0x19, 0x01,        //   Usage Minimum (1)
  0x29, 0x10,        //   Usage Maximum (16)
  0x81, 0x02,        //   Input (Data, Var, Abs)
  0x05, 0x01,        //   Usage Page (Generic Desktop)
  0x25, 0x07,        //   Logical Maximum (7)
  0x46, 0x3B, 0x01,  //   Physical Maximum (315)
  0x75, 0x04,        //   Report Size (4)
  0x95, 0x01,        //   Report Count (1)
  0x65, 0x14,        //   Unit (Degrees)
  0x09, 0x39,        //   Usage (Hat Switch)
  0x81, 0x42,        //   Input (Data, Var, Abs, Null State)
  0x65, 0x00,        //   Unit (None)
  0x95, 0x01,        //   Report Count (1)
  0x81, 0x01,        //   Input (Const)
  0x26, 0xFF, 0x00,  //   Logical Maximum (255)
  0x46, 0xFF, 0x00,  //   Physical Maximum (255)
  0x09, 0x30,        //   Usage (X)
  0x09, 0x31,        //   Usage (Y)
  0x09, 0x32,        //   Usage (Z)
  0x09, 0x35,        //   Usage (Rz)
  0x75, 0x08,        //   Report Size (8)
  0x95, 0x04,        //   Report Count (4)
  0x81, 0x02,        //   Input (Data, Var, Abs)
  0x06, 0x00, 0xFF,  //   Usage Page (Vendor Defined)
  0x09, 0x20,        //   Usage (0x20)
  0x95, 0x01,        //   Report Count (1)
  0x81, 0x02,        //   Input (Data, Var, Abs)
  0x0A, 0x21, 0x26,  //   Usage (0x2621)
  0x95, 0x08,        //   Report Count (8)
  0x91, 0x02,        //   Output (Data, Var, Abs)
  0xC0               // End Collection
};

struct __attribute__((packed)) SwitchReport {
  uint16_t buttons;
  uint8_t hat;
  uint8_t lx;
  uint8_t ly;
  uint8_t rx;
  uint8_t ry;
  uint8_t vendor;
};

enum SwitchButton : uint16_t {
  kButtonY = 1 << 0,
  kButtonB = 1 << 1,
  kButtonA = 1 << 2,
  kButtonX = 1 << 3,
  kButtonL = 1 << 4,
  kButtonR = 1 << 5,
  kButtonZL = 1 << 6,
  kButtonZR = 1 << 7,
  kButtonMinus = 1 << 8,
  kButtonPlus = 1 << 9,
  kButtonLStick = 1 << 10,
  kButtonRStick = 1 << 11,
  kButtonHome = 1 << 12,
  kButtonCapture = 1 << 13,
};

static const uint8_t kHatNeutral = 8;
static const uint8_t kStickCenter = 128;
static const uint8_t kLedPin = 6;
static const uint32_t kReportIntervalMs = 8;

USBHID HID;

class PokkenPad : public USBHIDDevice {
 public:
  PokkenPad() { HID.addDevice(this, sizeof(kReportDescriptor)); }

  uint16_t _onGetDescriptor(uint8_t* buffer) override {
    memcpy(buffer, kReportDescriptor, sizeof(kReportDescriptor));
    return sizeof(kReportDescriptor);
  }

  bool send(const SwitchReport& report) {
    if (!HID.ready()) {
      return false;
    }
    return HID.SendReport(0, &report, sizeof(report));
  }
};

PokkenPad pad;

// ============================================================
// 論理的な入力状態（x360ce 形式）
// ============================================================

// x360ce 形式の名前で保持する入力状態。ここから SwitchReport を組み立てる
struct PadState {
  bool buttons[15];  // b1〜b14（添字 0 は未使用）
  float axes[7];     // a1〜a6（添字 0 は未使用）
  int hat;           // p1：度数（0〜315）、-1 でニュートラル

  void reset() {
    memset(buttons, 0, sizeof(buttons));
    memset(axes, 0, sizeof(axes));
    hat = -1;
  }
};

PadState currentState;

// b1〜b14 を Switch のボタンビットに対応づける
static const uint16_t kButtonBits[15] = {
  0,
  kButtonA,       // b1
  kButtonB,       // b2
  kButtonX,       // b3
  kButtonY,       // b4
  kButtonL,       // b5
  kButtonR,       // b6
  kButtonMinus,   // b7
  kButtonPlus,    // b8
  kButtonLStick,  // b9
  kButtonRStick,  // b10
  kButtonZL,      // b11
  kButtonZR,      // b12
  kButtonHome,    // b13
  kButtonCapture  // b14
};

// -1〜1 の軸の値を 0〜255 に変換する。invert が true なら上下を反転する
uint8_t axisToByte(float value, bool invert) {
  value = constrain(value, -1.0f, 1.0f);
  if (invert) {
    value = -value;
  }
  return static_cast<uint8_t>(lroundf(kStickCenter + value * 127.0f));
}

// 度数（0〜315）をハットの値（0〜7）に変換する
uint8_t degreesToHat(int degrees) {
  if (degrees < 0) {
    return kHatNeutral;
  }
  // 45 度刻みに丸める
  const int index = ((degrees % 360) + 22) / 45;
  return static_cast<uint8_t>(index % 8);
}

SwitchReport buildReport(const PadState& state) {
  SwitchReport report = {};
  for (int i = 1; i <= 14; ++i) {
    if (state.buttons[i]) {
      report.buttons |= kButtonBits[i];
    }
  }
  // a3 / a6 はトリガー扱い。0.5 を超えたら ZL / ZR を押す
  if (state.axes[3] > 0.5f) {
    report.buttons |= kButtonZL;
  }
  if (state.axes[6] > 0.5f) {
    report.buttons |= kButtonZR;
  }
  report.hat = degreesToHat(state.hat);
  report.lx = axisToByte(state.axes[1], false);
  report.ly = axisToByte(state.axes[2], true);  // Switch は上が 0 なので反転する
  report.rx = axisToByte(state.axes[4], false);
  report.ry = axisToByte(state.axes[5], true);
  report.vendor = 0;
  return report;
}

// JSON オブジェクトの内容を PadState に適用する
// "rest":1 があれば先にニュートラルへ戻す。知らないキーは無視して、その名前を返す
String applyJsonToState(JsonObjectConst obj, PadState& state) {
  String ignored;
  if (obj["rest"].as<int>() == 1) {
    state.reset();
  }
  for (JsonPairConst kv : obj) {
    const char* key = kv.key().c_str();
    if (strcmp(key, "rest") == 0 || strcmp(key, "t") == 0 || strcmp(key, "ms") == 0) {
      continue;
    }
    const char prefix = key[0];
    const int index = atoi(key + 1);
    if (prefix == 'b' && index >= 1 && index <= 14) {
      state.buttons[index] = kv.value().as<int>() != 0;
    } else if (prefix == 'a' && index >= 1 && index <= 6) {
      state.axes[index] = kv.value().as<float>();
    } else if (prefix == 'p' && index == 1) {
      state.hat = kv.value().as<int>();
    } else {
      if (ignored.length() > 0) {
        ignored += ",";
      }
      ignored += key;
    }
  }
  return ignored;
}

// PadState を JSON に書き出す（ニュートラルでない項目だけ）
void stateToJson(const PadState& state, JsonObject out) {
  for (int i = 1; i <= 14; ++i) {
    if (state.buttons[i]) {
      out[String("b") + i] = 1;
    }
  }
  for (int i = 1; i <= 6; ++i) {
    if (state.axes[i] != 0.0f) {
      out[String("a") + i] = state.axes[i];
    }
  }
  if (state.hat >= 0) {
    out["p1"] = state.hat;
  }
}

// ============================================================
// タイミング付きの入力列（sequence / press）
// ============================================================

static const int kMaxSteps = 64;
static const size_t kStepJsonSize = 128;

struct SequenceStep {
  uint32_t atMs;                // 開始からの経過時間
  char json[kStepJsonSize];     // そのとき適用する JSON
};

SequenceStep sequenceSteps[kMaxSteps];
int sequenceLength = 0;
int sequenceCursor = 0;
uint32_t sequenceStartMs = 0;
bool sequenceRunning = false;

// 実行中の入力列を進める。期限が来たステップを順に適用する
void updateSequence() {
  if (!sequenceRunning) {
    return;
  }
  const uint32_t elapsed = millis() - sequenceStartMs;
  while (sequenceCursor < sequenceLength && sequenceSteps[sequenceCursor].atMs <= elapsed) {
    JsonDocument doc;
    if (deserializeJson(doc, sequenceSteps[sequenceCursor].json) == DeserializationError::Ok) {
      applyJsonToState(doc.as<JsonObjectConst>(), currentState);
    }
    ++sequenceCursor;
  }
  if (sequenceCursor >= sequenceLength) {
    sequenceRunning = false;
  }
}

// JSON 配列から入力列を組み立てて開始する。失敗したらエラー文字列を返す
String startSequence(JsonArrayConst steps) {
  if (steps.size() == 0 || steps.size() > kMaxSteps) {
    return String("steps must be 1..") + kMaxSteps;
  }
  sequenceLength = 0;
  for (JsonObjectConst step : steps) {
    SequenceStep& dst = sequenceSteps[sequenceLength];
    dst.atMs = step["t"].as<uint32_t>();
    const size_t written = serializeJson(step, dst.json, kStepJsonSize);
    if (written >= kStepJsonSize - 1) {
      return "a step is too long";
    }
    ++sequenceLength;
  }
  sequenceCursor = 0;
  sequenceStartMs = millis();
  sequenceRunning = true;
  return "";
}

// ============================================================
// HTTP サーバー
// ============================================================

WebServer server(HTTP_PORT);

void sendJson(int code, JsonDocument& doc) {
  String body;
  serializeJson(doc, body);
  server.send(code, "application/json", body);
}

void sendError(int code, const String& message) {
  JsonDocument doc;
  doc["Error"] = message;
  sendJson(code, doc);
}

// トークンを確認する。API_TOKEN が空なら認証なし
bool checkAuth() {
  if (strlen(API_TOKEN) == 0) {
    return true;
  }
  const String header = server.header("Authorization");
  const String expected = String("Bearer ") + API_TOKEN;
  if (header == expected) {
    return true;
  }
  sendError(401, "unauthorized");
  return false;
}

// リクエストボディを JSON として読む。失敗したらエラーを返して false
bool readBody(JsonDocument& doc) {
  const String body = server.arg("plain");
  const DeserializationError err = deserializeJson(doc, body);
  if (err) {
    sendError(400, String("invalid json: ") + err.c_str());
    return false;
  }
  return true;
}

void handleRoot() {
  if (!checkAuth()) return;
  JsonDocument doc;
  doc["ip"] = WiFi.localIP().toString();
  doc["rssi"] = WiFi.RSSI();
  doc["uptime_ms"] = millis();
  doc["usb_ready"] = HID.ready();
  doc["sequence_running"] = sequenceRunning;
  stateToJson(currentState, doc["Now"].to<JsonObject>());
  sendJson(200, doc);
}

void handleRead() {
  if (!checkAuth()) return;
  JsonDocument doc;
  stateToJson(currentState, doc["State"].to<JsonObject>());
  stateToJson(currentState, doc["Now"].to<JsonObject>());
  doc["Error"] = "";
  sendJson(200, doc);
}

void handleSetState() {
  if (!checkAuth()) return;
  JsonDocument doc;
  if (!readBody(doc)) return;
  if (!doc.is<JsonObjectConst>()) {
    sendError(400, "body must be a json object");
    return;
  }
  // 手動でセットしたら、実行中の入力列は止める
  sequenceRunning = false;
  const String ignored = applyJsonToState(doc.as<JsonObjectConst>(), currentState);

  JsonDocument res;
  stateToJson(currentState, res["Now"].to<JsonObject>());
  res["Ignored"] = ignored;
  res["Error"] = "";
  sendJson(200, res);
}

void handlePress() {
  if (!checkAuth()) return;
  JsonDocument doc;
  if (!readBody(doc)) return;
  if (!doc.is<JsonObjectConst>()) {
    sendError(400, "body must be a json object");
    return;
  }
  uint32_t ms = doc["ms"] | 100;
  ms = constrain(ms, 1, 10000);

  // 押す → 離す、の 2 ステップの入力列に変換する
  JsonDocument seq;
  JsonArray steps = seq.to<JsonArray>();
  JsonObject pressStep = steps.add<JsonObject>();
  pressStep["t"] = 0;
  JsonObject releaseStep = steps.add<JsonObject>();
  releaseStep["t"] = ms;
  for (JsonPairConst kv : doc.as<JsonObjectConst>()) {
    const char* key = kv.key().c_str();
    if (strcmp(key, "ms") == 0 || strcmp(key, "rest") == 0) continue;
    pressStep[key] = kv.value();
    if (key[0] == 'p') {
      releaseStep[key] = -1;  // 十字キーはニュートラルが -1
    } else {
      releaseStep[key] = 0;
    }
  }
  const String err = startSequence(seq.as<JsonArrayConst>());
  if (err.length() > 0) {
    sendError(400, err);
    return;
  }
  JsonDocument res;
  res["ms"] = ms;
  res["Error"] = "";
  sendJson(200, res);
}

void handleSequence() {
  if (!checkAuth()) return;
  JsonDocument doc;
  if (!readBody(doc)) return;
  if (!doc.is<JsonArrayConst>()) {
    sendError(400, "body must be a json array");
    return;
  }
  const String err = startSequence(doc.as<JsonArrayConst>());
  if (err.length() > 0) {
    sendError(400, err);
    return;
  }
  JsonDocument res;
  res["steps"] = sequenceLength;
  res["Error"] = "";
  sendJson(200, res);
}

void handleNotFound() {
  sendError(404, "not found");
}

// ============================================================
// Wi-Fi
// ============================================================

void connectWiFi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // 応答を速くするため省電力モードを切る
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  // つながるまで LED を点滅させる（その間も USB レポートは送り続ける）
  uint32_t lastBlink = 0;
  bool led = false;
  while (WiFi.status() != WL_CONNECTED) {
    const uint32_t now = millis();
    if (now - lastBlink >= 250) {
      lastBlink = now;
      led = !led;
      digitalWrite(kLedPin, led ? HIGH : LOW);
    }
    pad.send(buildReport(currentState));
    delay(kReportIntervalMs);
  }
  digitalWrite(kLedPin, LOW);

  if (MDNS.begin(MDNS_HOSTNAME)) {
    MDNS.addService("http", "tcp", HTTP_PORT);
  }
}

// ============================================================
// setup / loop
// ============================================================

uint32_t lastReportMs = 0;

void setup() {
  pinMode(kLedPin, OUTPUT);
  digitalWrite(kLedPin, LOW);
  currentState.reset();

  // HORI ポッ拳コントローラーの USB 情報を名乗る（個人での動作確認用）
  USB.VID(0x0F0D);
  USB.PID(0x0092);
  USB.manufacturerName("HORI CO.,LTD.");
  USB.productName("POKKEN CONTROLLER");
  HID.begin();
  USB.begin();

  connectWiFi();

  // Authorization ヘッダーを読めるように、収集するヘッダーを登録する
  const char* headerKeys[] = {"Authorization"};
  server.collectHeaders(headerKeys, 1);

  server.on("/", HTTP_GET, handleRoot);
  server.on("/read", HTTP_GET, handleRead);
  server.on("/set_state", HTTP_POST, handleSetState);
  server.on("/press", HTTP_POST, handlePress);
  server.on("/sequence", HTTP_POST, handleSequence);
  server.onNotFound(handleNotFound);
  server.begin();
}

void loop() {
  server.handleClient();
  updateSequence();

  // Wi-Fi が切れたら再接続する
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
  }

  const uint32_t now = millis();
  if (now - lastReportMs >= kReportIntervalMs) {
    lastReportMs = now;
    const SwitchReport report = buildReport(currentState);
    // 何か押している間は LED を点灯する
    digitalWrite(kLedPin, (report.buttons != 0 || report.hat != kHatNeutral) ? HIGH : LOW);
    pad.send(report);
  }
}