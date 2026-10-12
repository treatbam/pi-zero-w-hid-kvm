// ESP32-S3-GEEK status panel: joins Zero W AP, polls /api/status, shows on 1.14" ST7789 (135x240).
// BOOT button (GPIO0) -> sends Ctrl+Alt+Del macro.
#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include "secrets.h"

// TODO(verify): LCD pins from Waveshare ESP32-S3-GEEK wiki/schematic. Values below are
// believed correct but NOT verified on hardware — check before trusting.
#define LCD_MOSI 11
#define LCD_SCLK 12
#define LCD_CS   10
#define LCD_DC    8
#define LCD_RST   9
#define LCD_BL    7
#define BTN_BOOT  0   // BOOT button, active low (standard ESP32-S3 strapping pin)

Adafruit_ST7789 tft(&SPI, LCD_CS, LCD_DC, LCD_RST);
const String BASE = String("http://") + KVM_HOST + ":" + KVM_PORT;

void show(const char* l1, uint16_t c1, const char* l2, uint16_t c2, const String& l3) {
  tft.fillScreen(ST77XX_BLACK); tft.setTextSize(2);
  tft.setCursor(4, 10);  tft.setTextColor(c1); tft.print(l1);
  tft.setCursor(4, 45);  tft.setTextColor(c2); tft.print(l2);
  tft.setCursor(4, 80);  tft.setTextColor(ST77XX_WHITE); tft.print(l3);
}

void setup() {
  Serial.begin(115200);
  pinMode(BTN_BOOT, INPUT_PULLUP);
  pinMode(LCD_BL, OUTPUT); digitalWrite(LCD_BL, HIGH);   // TODO(verify): BL polarity
  SPI.begin(LCD_SCLK, -1, LCD_MOSI, LCD_CS);
  tft.init(135, 240);   // 1.14" panel; Adafruit lib handles the 135x240 offsets
  tft.setRotation(1);   // landscape; TODO: adjust to how the GEEK is mounted
  show("KVM panel", ST77XX_CYAN, "WiFi...", ST77XX_YELLOW, KVM_WIFI_SSID);
  WiFi.mode(WIFI_STA); WiFi.begin(KVM_WIFI_SSID, KVM_WIFI_PASS);
}

void pollStatus() {
  if (WiFi.status() != WL_CONNECTED) { show("WiFi: down", ST77XX_RED, "", 0, KVM_WIFI_SSID); return; }
  HTTPClient http; http.setTimeout(1500);
  http.begin(BASE + "/api/status");
  int code = http.GET();
  if (code != 200) { show("Zero: no API", ST77XX_RED, "", 0, String("HTTP ") + code); http.end(); return; }
  JsonDocument doc;
  if (deserializeJson(doc, http.getString())) { show("bad JSON", ST77XX_RED, "", 0, ""); http.end(); return; }
  http.end();
  bool hid = doc["hid_ready"] | false, host = doc["host_connected"] | false;
  show(hid ? "HID: ready" : "HID: DOWN", hid ? ST77XX_GREEN : ST77XX_RED,
       host ? "Host: linked" : "Host: none", host ? ST77XX_GREEN : ST77XX_ORANGE,
       String(doc["ip"] | "?"));
}

void sendMacro(const char* name) {
  HTTPClient http; http.begin(BASE + "/api/macro");
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(String("{\"name\":\"") + name + "\"}");
  Serial.printf("macro %s -> %d\n", name, code); http.end();
}

void loop() {
  static uint32_t last = 0;
  if (millis() - last > 2000) { last = millis(); pollStatus(); }
  if (digitalRead(BTN_BOOT) == LOW) {        // simple debounce + wait for release
    delay(30);
    if (digitalRead(BTN_BOOT) == LOW) { sendMacro("ctrl_alt_del"); while (digitalRead(BTN_BOOT) == LOW) delay(10); }
  }
  delay(10);
}
