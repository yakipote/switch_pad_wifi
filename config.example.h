// config.example.h → config.h にコピーして書き換える
// 自分の環境に合わせて書き換えるファイル（config.h は git に入らない。公開しないこと）
#pragma once

// 接続する Wi-Fi（2.4GHz のみ。ESP32-S3 は 5GHz に対応していません）
#define WIFI_SSID "your-ssid"
#define WIFI_PASSWORD "your-password"

// HTTP の認証トークン。Authorization: Bearer <トークン> で送る
// 空文字 "" にすると認証なし（同じ LAN の誰でも操作できるので注意）
#define API_TOKEN "change-me"

// mDNS のホスト名。http://switchpad.local/ でアクセスできる
#define MDNS_HOSTNAME "switchpad"

// HTTP サーバーのポート
#define HTTP_PORT 80