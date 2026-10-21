// ESP32-S3-GEEK KVM panel. Four pages (scoreboard, KVM, desktop, terminal); every page uses
// the shared design system below (palette, ramp(), panel()). Formerly: three pages, one short press to advance:
//   scoreboard → desktop still → terminal → scoreboard.
// A long press does nothing, and two quick presses are just two page steps. There is no menu.
// The status word (LINKED / KEYS / NO HOST) comes from the Zero's /api/status.
// While LINKED, each PC figure has a 2-minute sparkline. After the helper goes
// quiet the last PC figures stay up for a minute under a "reconnecting" toast,
// then the board shows the Pi and "searching". The desktop still is GET /api/frame,
// polled only while that page is showing, and still goes stale with the Zero (6 s).
// Networking runs on core 0 so the UI loop never blocks on HTTP.
#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include <Preferences.h>
#include <WebServer.h>
#if __has_include("secrets.h")
#include "secrets.h"
#else
#include "secrets.example.h"   // CI / web-flasher build: placeholders, real values set in the setup portal
#define KVM_PLACEHOLDER_SECRETS 1
#endif
#ifndef KVM_PIN
#define KVM_PIN ""   // not in secrets.h: set it in the setup portal (BOOT held at power-on)
#endif

#define LCD_MOSI 11
#define LCD_SCLK 12
#define LCD_CS   10
#define LCD_DC    8
#define LCD_RST   9
#define LCD_BL    7

#ifndef BTN_BACK
#define BTN_BACK    0
#endif
#ifndef BTN_UP
#define BTN_UP     -1
#endif
#ifndef BTN_DOWN
#define BTN_DOWN   -1
#endif
#ifndef BTN_SELECT
#define BTN_SELECT -1
#endif

static const int W = 240, H = 135;
Adafruit_ST7789 tft(&SPI, LCD_CS, LCD_DC, LCD_RST);
GFXcanvas16 fb(W, H);
// One still. The UI keeps the previous copy in the canvas while a new one downloads.
static uint16_t gFrame[W * H];
static bool gFrameOk = false;
static volatile bool gFrameBusy = false;
static bool gShown = false;

struct Cfg { String ssid, pass, host, pin; int port; } cfg;
String BASE;
static void loadCfg() {
  Preferences p; p.begin("kvm", true);
  cfg.ssid = p.getString("ssid", KVM_WIFI_SSID); cfg.pass = p.getString("pass", KVM_WIFI_PASS);
  cfg.host = p.getString("host", KVM_HOST);      cfg.pin  = p.getString("pin", KVM_PIN);
  cfg.port = p.getInt("port", KVM_PORT); p.end();
  BASE = String("http://") + cfg.host + ":" + cfg.port;
}
static bool cfgSaved() { Preferences p; p.begin("kvm", true); bool ok = p.isKey("ssid"); p.end(); return ok; }

static String esc(const String& v) { String o = v; o.replace("&", "&amp;"); o.replace("\"", "&quot;"); o.replace("<", "&lt;"); return o; }
static void setupPortal() {
  tft.fillScreen(ST77XX_BLACK); tft.setTextColor(ST77XX_YELLOW); tft.setTextSize(2);
  tft.setCursor(6, 10); tft.print("GEEK setup");
  tft.setTextSize(1); tft.setTextColor(ST77XX_WHITE);
  tft.setCursor(6, 45); tft.print("1. Join Wi-Fi: KVM-GEEK-setup");
  tft.setCursor(6, 62); tft.print("2. Open http://192.168.4.1");
  tft.setCursor(6, 79); tft.print("3. Enter Zero Wi-Fi, host, PIN");
  tft.setCursor(6, 110); tft.setTextColor(0x8410); tft.print("hold BOOT at power-on to return");
  WiFi.mode(WIFI_AP); WiFi.softAP("KVM-GEEK-setup");
  static WebServer srv(80);
  srv.on("/", HTTP_GET, [] {
    String h = F("<!doctype html><meta name=viewport content='width=device-width'><title>GEEK setup</title>"
                 "<style>body{font-family:sans-serif;max-width:420px;margin:2em auto}input{width:100%;padding:6px;margin:4px 0 12px}</style>"
                 "<h2>KVM GEEK setup</h2><form method=post action=/save>");
    auto f = [&](const char* n, const char* l, const String& v, const char* t) {
      h += String(l) + "<input name=" + n + " type=" + t + " value=\"" + esc(v) + "\">"; };
    f("ssid", "Zero Wi-Fi SSID", cfg.ssid, "text"); f("pass", "Wi-Fi password", "", "password");
    f("host", "Zero address", cfg.host, "text");   f("port", "Port", String(cfg.port), "number");
    f("pin", "Console PIN (from install.sh)", "", "password");
    h += F("<small>Blank password/PIN keeps the saved value.</small><br><br><button>Save &amp; reboot</button></form>");
    srv.send(200, "text/html", h);
  });
  srv.on("/save", HTTP_POST, [] {
    Preferences p; p.begin("kvm", false);
    String ssid = srv.arg("ssid"); ssid.trim();
    String host = srv.arg("host"); host.trim();
    p.putString("ssid", ssid.length() ? ssid : cfg.ssid);
    p.putString("host", host.length() ? host : cfg.host);
    p.putInt("port", srv.arg("port").toInt() > 0 ? srv.arg("port").toInt() : 8080);
#ifdef KVM_PLACEHOLDER_SECRETS
    const char* defPass = "";
    const char* defPin = "";
#else
    const char* defPass = cfg.pass.c_str();
    const char* defPin = cfg.pin.c_str();
#endif
    if (srv.arg("pass").length()) p.putString("pass", srv.arg("pass")); else if (!p.isKey("pass")) p.putString("pass", defPass);
    if (srv.arg("pin").length())  p.putString("pin", srv.arg("pin"));   else if (!p.isKey("pin"))  p.putString("pin", defPin);
    p.end();
    srv.send(200, "text/html", "<h3>Saved. GEEK is rebooting.</h3>"); delay(800); ESP.restart();
  });
  srv.begin(); Serial.println("setup portal: AP KVM-GEEK-setup, http://192.168.4.1");
  for (;;) { srv.handleClient(); delay(2); }
}

struct Status {
  bool wifi = false, api = false, hid = false, host = false;
  bool ledNum = false, ledCaps = false, ledScroll = false;
  int httpCode = 0, rssi = 0;
  char udc[16] = "?", ip[16] = "--", geekIp[16] = "--", link[12] = "";
  char pcIp[16] = "", pcTopProc[16] = "";
  float temp = NAN, load1 = NAN, pcTemp = NAN, pcLoad = NAN;
  float pcRx = NAN, pcTx = NAN, pcNcpu = NAN, pcTopCpu = NAN;
  int mem = -1, pcMem = -1, pcDisk = -1, pcCoreCount = 0;
  uint8_t pcCores[8] = {0};
  // KVM page (PIN-gated fields: the status GET sends X-KVM-PIN)
  int zRssi = 0;                 // Zero wlan0 dBm, 0 = unknown
  int8_t ts = -1;                // tailscale: -1 n/a, 0 down, 1 up
  char serialMode[8] = "";
  // type-8 extended stats (absent -> NAN / 0)
  uint8_t coresAll[16] = {0};
  int coresAllN = 0, coresTotal = 0;
  float pcGpu = NAN, pcCpuT = NAN, pcGpuT = NAN, pcNvmeT = NAN;
  long upS = -1, pcUpS = -1;
  uint32_t latencyMs = 0, stampMs = 0, polls = 0, fails = 0;
};
Status gStat;
// One sample per /api/status poll (2 s). Gaps are NAN so a quiet helper breaks the line
// instead of drawing across the outage. last* is the last live PC reading.
static const int HIST = 60;
static const uint32_t RECONNECT_MS = 60000;
struct PcHist {
  float temp[HIST], load[HIST], mem[HIST], rx[HIST], tx[HIST];
  float cpuT[HIST], gpuT[HIST], nvT[HIST];   // type-8 sensor temps, NAN when absent
  int n = 0, next = 0;
  uint32_t lastMs = 0;
  bool had = false;
  float lastT = NAN, lastL = NAN, lastM = NAN, lastRx = NAN, lastTx = NAN, lastNcpu = NAN, lastTopCpu = NAN;
  int lastDisk = -1, lastCoreCount = 0;
  uint8_t lastCores[8] = {0};
  char lastPcIp[16] = "", lastTopProc[16] = "";
};
PcHist gHist;
static void copyStr(char* dst, size_t n, const char* src) {
  if (!src) src = "";
  strlcpy(dst, src, n);
}
static void pushHist(const Status& s, bool live) {
  float t = live ? s.pcTemp : NAN;
  float l = live ? s.pcLoad : NAN;
  float m = live ? (float)s.pcMem : NAN;
  float rx = live ? s.pcRx : NAN;
  float tx = live ? s.pcTx : NAN;
  gHist.temp[gHist.next] = t;
  gHist.load[gHist.next] = l;
  gHist.mem[gHist.next] = m;
  gHist.rx[gHist.next] = rx;
  gHist.tx[gHist.next] = tx;
  gHist.cpuT[gHist.next] = live ? s.pcCpuT : NAN;
  gHist.gpuT[gHist.next] = live ? s.pcGpuT : NAN;
  gHist.nvT[gHist.next] = live ? s.pcNvmeT : NAN;
  gHist.next = (gHist.next + 1) % HIST;
  if (gHist.n < HIST) gHist.n++;
  if (live) {
    gHist.lastMs = millis();
    gHist.had = true;
    gHist.lastT = t; gHist.lastL = l; gHist.lastM = m;
    gHist.lastRx = s.pcRx; gHist.lastTx = s.pcTx;
    if (!isnan(s.pcNcpu) && s.pcNcpu >= 1) gHist.lastNcpu = s.pcNcpu;
    if (s.pcCoreCount > 0) {
      gHist.lastCoreCount = s.pcCoreCount;
      memcpy(gHist.lastCores, s.pcCores, sizeof s.pcCores);
    }
    if (s.pcDisk >= 0) gHist.lastDisk = s.pcDisk;
    if (s.pcIp[0]) copyStr(gHist.lastPcIp, sizeof gHist.lastPcIp, s.pcIp);
    if (s.pcTopProc[0]) {
      copyStr(gHist.lastTopProc, sizeof gHist.lastTopProc, s.pcTopProc);
      gHist.lastTopCpu = s.pcTopCpu;
    }
  }
}
SemaphoreHandle_t gLock;
// Wrong PIN: the Zero counts every failed attempt and locks this address out after 5.
// After a 401/429 (or pin_ok:false on the status poll) stop sending the PIN for a while.
static const uint32_t PIN_BACKOFF_MS = 180000;
volatile uint32_t gPinBadAt = 0;
volatile bool gPinBad = false;
static bool pinUsable() {
  if (!cfg.pin.length()) return false;
  if (gPinBad && millis() - gPinBadAt < PIN_BACKOFF_MS) return false;
  gPinBad = false;
  return true;
}
static void pinFailed() { gPinBad = true; gPinBadAt = millis(); }
volatile bool gTermOn = false;
volatile bool gDeskOn = false;
volatile bool gFramePull = false;

static const int TCOLS = 39, TROWS = 16;
struct Term { char row[TROWS][TCOLS + 1]; int n = 0, code = 0; bool open = false; uint32_t seq = 0, stampMs = 0; };
Term gTerm;

void pollOnce() {
  Status s; s.wifi = WiFi.status() == WL_CONNECTED; s.rssi = s.wifi ? WiFi.RSSI() : 0;
  if (s.wifi) copyStr(s.geekIp, sizeof s.geekIp, WiFi.localIP().toString().c_str());
  if (s.wifi) {
    HTTPClient http; http.setTimeout(1500); http.setConnectTimeout(1500);
    uint32_t t0 = millis(); http.begin(BASE + "/api/status");
    if (pinUsable()) http.addHeader("X-KVM-PIN", cfg.pin);   // unlocks the Zero link + serial fields
    s.httpCode = http.GET(); s.latencyMs = millis() - t0;
    if (s.httpCode == 200) {
      JsonDocument d;
      if (!deserializeJson(d, http.getString())) {
        s.api = true; s.hid = d["hid_ready"] | false; s.host = d["host_connected"] | false;
        if (d["leds"].is<JsonObject>()) {
          s.ledNum = d["leds"]["num"] | false;
          s.ledCaps = d["leds"]["caps"] | false;
          s.ledScroll = d["leds"]["scroll"] | false;
        }
        copyStr(s.udc, sizeof s.udc, d["udc_state"] | "?");
        copyStr(s.ip, sizeof s.ip, d["ip"] | "--");
        copyStr(s.link, sizeof s.link, d["link"] | "");
        if (!d["temp_c"].isNull()) s.temp = d["temp_c"].as<float>();
        if (!d["load1"].isNull()) s.load1 = d["load1"].as<float>();
        if (!d["mem_pct"].isNull()) s.mem = d["mem_pct"].as<int>();
        s.upS = d["uptime_s"] | -1L;
        if (!d["pc_temp_c"].isNull()) s.pcTemp = d["pc_temp_c"].as<float>();
        if (!d["pc_load1"].isNull()) s.pcLoad = d["pc_load1"].as<float>();
        if (!d["pc_mem_pct"].isNull()) s.pcMem = d["pc_mem_pct"].as<int>();
        if (!d["pc_rx_bps"].isNull()) s.pcRx = d["pc_rx_bps"].as<float>();
        if (!d["pc_tx_bps"].isNull()) s.pcTx = d["pc_tx_bps"].as<float>();
        if (!d["pc_ncpu"].isNull()) s.pcNcpu = d["pc_ncpu"].as<float>();
        if (d["pc_cores"].is<JsonArray>()) {
          int ci = 0;
          for (JsonVariant v : d["pc_cores"].as<JsonArray>()) {
            if (ci >= 8) break;
            int cv = v | -1;
            if (cv >= 0 && cv <= 100) s.pcCores[ci++] = (uint8_t)cv;
          }
          s.pcCoreCount = ci;
        }
        if (!d["pc_ip"].isNull()) copyStr(s.pcIp, sizeof s.pcIp, d["pc_ip"] | "");
        if (!d["pc_disk_pct"].isNull()) s.pcDisk = d["pc_disk_pct"].as<int>();
        if (!d["pc_top_proc"].isNull()) copyStr(s.pcTopProc, sizeof s.pcTopProc, d["pc_top_proc"] | "");
        if (!d["pc_top_cpu_pct"].isNull()) s.pcTopCpu = d["pc_top_cpu_pct"].as<float>();
        if (d["pin_ok"].is<bool>() && !(d["pin_ok"] | true)) pinFailed();
        if (!d["zero_wifi_rssi"].isNull()) s.zRssi = d["zero_wifi_rssi"].as<int>();
        if (d["tailscale"].is<const char*>()) s.ts = strcmp(d["tailscale"] | "", "up") == 0 ? 1 : 0;
        copyStr(s.serialMode, sizeof s.serialMode, d["serial_mode"] | "");
        if (d["pc_cores_all"].is<JsonArray>()) {
          int ci = 0, tot = 0;
          for (JsonVariant v : d["pc_cores_all"].as<JsonArray>()) {
            tot++;
            if (ci >= 16) continue;
            float cv = v | -1.f;
            if (cv >= 0.f && cv <= 100.f) s.coresAll[ci++] = (uint8_t)(cv + 0.5f);
          }
          s.coresAllN = ci; s.coresTotal = tot;
        }
        if (!d["pc_gpu_pct"].isNull()) s.pcGpu = d["pc_gpu_pct"].as<float>();
        if (!d["pc_cpu_temp_c"].isNull()) s.pcCpuT = d["pc_cpu_temp_c"].as<float>();
        if (!d["pc_gpu_temp_c"].isNull()) s.pcGpuT = d["pc_gpu_temp_c"].as<float>();
        if (!d["pc_nvme_temp_c"].isNull()) s.pcNvmeT = d["pc_nvme_temp_c"].as<float>();
        s.pcUpS = d["pc_uptime_s"] | -1L;
      }
    }
    http.end();
  }
  xSemaphoreTake(gLock, portMAX_DELAY);
  s.polls = gStat.polls + 1; s.fails = gStat.fails + (s.api ? 0 : 1); s.stampMs = millis();
  gStat = s;
  bool live = s.api && strcmp(s.link, "LINKED") == 0
              && !isnan(s.pcTemp) && !isnan(s.pcLoad) && s.pcMem >= 0;
  pushHist(s, live);
  // Cable pulled: the board shows NO HOST at once, and the PC figures held for "reconnecting"
  // belong to a machine that is gone. Plugging into a different host must not revive them.
  if (s.api && strcmp(s.link, "NO HOST") == 0) gHist.had = false;
  xSemaphoreGive(gLock);
}

void pollTerm() {
  Term t; t.n = 0;
  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http; http.setTimeout(1500); http.setConnectTimeout(1500);
    http.begin(BASE + "/api/term?n=" + TROWS); http.addHeader("X-KVM-PIN", cfg.pin);
    t.code = http.GET();
    if (t.code == 401 || t.code == 429) pinFailed();
    if (t.code == 200) {
      JsonDocument d;
      if (!deserializeJson(d, http.getString())) {
        t.open = d["open"] | false; t.seq = d["seq"] | 0;
        static char tmp[64][TCOLS + 1]; int cnt = 0;
        auto add = [&](const char* l) {
          size_t len = strlen(l);
          for (size_t o = 0; o == 0 || o < len; o += TCOLS) {
            char* dst = tmp[cnt % 64]; size_t k = min((size_t)TCOLS, len - min(o, len));
            memcpy(dst, l + o, k); dst[k] = 0; cnt++;
            if (len == 0) break;
          }
        };
        for (JsonVariant v : d["lines"].as<JsonArray>()) add(v | "");
        const char* p = d["partial"] | "";
        if (*p) add(p);
        int first = max(0, cnt - TROWS);
        for (int i = first; i < cnt; i++) memcpy(t.row[t.n++], tmp[i % 64], TCOLS + 1);
      }
    }
    http.end();
  }
  t.stampMs = millis();
  xSemaphoreTake(gLock, portMAX_DELAY); gTerm = t; xSemaphoreGive(gLock);
}

void pollFrame() {
  xSemaphoreTake(gLock, portMAX_DELAY);
  gFrameBusy = true;
  xSemaphoreGive(gLock);
  bool ok = false;
  if (WiFi.status() == WL_CONNECTED && gDeskOn) {
    HTTPClient http; http.setTimeout(4000); http.setConnectTimeout(1500);
    http.begin(BASE + "/api/frame"); http.addHeader("X-KVM-PIN", cfg.pin);
    int code = http.GET();
    if (code == 401 || code == 429) pinFailed();
    if (code == 200) {
      WiFiClient* stream = http.getStreamPtr();
      uint8_t* dst = (uint8_t*)gFrame;
      int need = W * H * 2, got = 0;
      uint32_t t0 = millis();
      while (got < need && gDeskOn && millis() - t0 < 4000) {
        int n = stream->read(dst + got, need - got);
        if (n > 0) got += n;
        else if (!stream->connected()) break;
        else delay(1);
      }
      ok = got == need && gDeskOn;
    }
    http.end();
  }
  xSemaphoreTake(gLock, portMAX_DELAY);
  gFrameOk = ok && gDeskOn;
  gFrameBusy = false;
  xSemaphoreGive(gLock);
}

void netTask(void*) {
  // Start due, and poll again the moment Wi-Fi comes up, so the first figures do not wait
  // out a 2 s tick on a board that has been showing "wifi down".
  uint32_t last = millis() - 2001, lastTerm = 0, lastFrame = 0;
  bool wasUp = false;
  for (;;) {
    uint32_t now = millis();
    bool up = WiFi.status() == WL_CONNECTED;
    if (up && !wasUp) last = now - 2001;
    wasUp = up;
    if (now - last > 2000) { last = now; pollOnce(); }
    bool pinOk = pinUsable();     // PIN-only endpoints wait out the backoff
    if (gTermOn && pinOk && now - lastTerm > 800) { lastTerm = now; pollTerm(); }
    if (gDeskOn && pinOk && (gFramePull || now - lastFrame > 2000)) {
      gFramePull = false; lastFrame = now; pollFrame();
    }
    delay(50);
  }
}

enum Ev { EV_NONE, EV_SHORT, EV_DOUBLE, EV_LONG, EV_VLONG };
struct Btn {
  Btn(int p, bool d, bool v = false) : pin(p), dbl(d), vlong(v) {}
  int pin; bool dbl, vlong; bool down = false, raw = false, longFired = false, pendingShort = false;
  uint32_t edgeMs = 0, pressMs = 0, releaseMs = 0;
  static const uint32_t DEB = 25, LONG = 600, VLONG = 3000, DBL = 280;
  Ev poll(uint32_t now) {
    if (pin < 0) return EV_NONE;
    bool r = digitalRead(pin) == LOW;
    if (r != raw) { raw = r; edgeMs = now; }
    if (now - edgeMs >= DEB && r != down) {
      down = r;
      if (down) { pressMs = now; longFired = false; }
      else if (!longFired) {
        if (vlong && now - pressMs >= LONG) { pendingShort = false; return EV_LONG; }
        if (!dbl) return EV_SHORT;
        if (pendingShort && now - releaseMs < DBL) { pendingShort = false; return EV_DOUBLE; }
        pendingShort = true; releaseMs = now;
      }
    }
    if (down && !longFired) {
      if (vlong && now - pressMs >= VLONG) { longFired = true; pendingShort = false; return EV_VLONG; }
      if (!vlong && now - pressMs >= LONG) { longFired = true; pendingShort = false; return EV_LONG; }
    }
    if (pendingShort && !down && now - releaseMs >= DBL) { pendingShort = false; return EV_SHORT; }
    return EV_NONE;
  }
};
// Double-press detection stays off: it would hold every short press for DBL ms and turn two
// quick presses (scoreboard -> terminal) into an ignored EV_DOUBLE. Each press is one step.
Btn bBack{BTN_BACK, false}, bUp{BTN_UP, false}, bDown{BTN_DOWN, false}, bSel{BTN_SELECT, false};

uint16_t rgb(uint8_t r, uint8_t g, uint8_t b) { return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3); }
uint16_t mix(uint16_t a, uint16_t b, int t) {
  if (t <= 0) return a;
  if (t >= 255) return b;
  int ar = a >> 11, ag = (a >> 5) & 63, ab = a & 31, br = b >> 11, bg = (b >> 5) & 63, bb = b & 31;
  return ((ar + (br - ar) * t / 255) << 11) | ((ag + (bg - ag) * t / 255) << 5) | (ab + (bb - ab) * t / 255);
}

// ---- Design system: every page draws with these and nothing else. ----------------------
// Palette. One value ramp (low -> warm -> hot) feeds every bar, capsule, sparkline and core.
const uint16_t C_BG   = rgb(12, 16, 24),   C_TXT  = 0xFFFF,           C_DIM  = rgb(130, 140, 165),
               C_VAL  = rgb(220, 228, 240), C_SOFT = rgb(197, 208, 200),
               C_OK   = rgb(60, 220, 120), C_WARN = rgb(255, 170, 40), C_BAD  = rgb(255, 70, 70),
               C_EDGE = rgb(44, 56, 80),   C_HDR  = rgb(26, 34, 52),   C_TRACK = rgb(22, 30, 44),
               C_LOW  = rgb(48, 176, 255), C_WARM = rgb(255, 186, 48), C_HOT  = rgb(255, 60, 60),
               C_RX   = rgb(48, 224, 136), C_TX   = rgb(245, 165, 45),
               C_RISE = rgb(255, 200, 90), C_FALL = rgb(110, 245, 190);
// Type and spacing. Small = 6x8 GFX font, big = 12x16.
const int FS_S = 1, FS_L = 2, CW = 6, LINE = 10, PAD = 4, GAP = 2, HDR_H = 10, RADIUS = 3;
// Shared scales: level in 0..1 plus the point where the ramp turns hot.
const float T_LO = 35.f, T_SPAN = 60.f;
inline float tempLevel(float c) { return (c - T_LO) / T_SPAN; }

// The single value -> colour ramp, one continuous gradient for every bar, spark, core and temp.
// GEEK_RAMP_LOW_BLUE 0 (default): green -> yellow -> orange -> red.
// GEEK_RAMP_LOW_BLUE 1:           blue -> cyan -> yellow -> orange -> red.
// `warn` is where the metric turns hot; it is mapped onto the orange stop (0.75).
#ifndef GEEK_RAMP_LOW_BLUE
#define GEEK_RAMP_LOW_BLUE 0
#endif
struct Stop { float at; uint8_t r, g, b; };
#if GEEK_RAMP_LOW_BLUE
const Stop RAMP[] = {{0.f, 48, 140, 255}, {0.25f, 40, 210, 200}, {0.5f, 230, 220, 50}, {0.75f, 255, 150, 40}, {1.f, 255, 60, 60}};
#else
const Stop RAMP[] = {{0.f, 40, 200, 90}, {0.45f, 230, 220, 50}, {0.75f, 255, 150, 40}, {1.f, 255, 60, 60}};
#endif
const int RAMP_N = sizeof RAMP / sizeof RAMP[0];
uint16_t ramp(float t, float warn = 0.75f) {
  if (isnan(t) || t < 0.f) t = 0.f;
  if (t > 1.f) t = 1.f;
  if (!(warn > 0.05f && warn < 0.95f)) warn = 0.75f;
  float u = t < warn ? t / warn * 0.75f : 0.75f + (t - warn) / (1.f - warn) * 0.25f;
  for (int i = 1; i < RAMP_N; i++) {
    if (u <= RAMP[i].at || i == RAMP_N - 1) {
      const Stop &a = RAMP[i - 1], &b = RAMP[i];
      float f = (u - a.at) / (b.at - a.at);
      if (f < 0.f) f = 0.f;
      if (f > 1.f) f = 1.f;
      return rgb(a.r + (b.r - a.r) * f, a.g + (b.g - a.g) * f, a.b + (b.b - a.b) * f);
    }
  }
  return rgb(RAMP[RAMP_N - 1].r, RAMP[RAMP_N - 1].g, RAMP[RAMP_N - 1].b);
}
uint16_t dimmed(uint16_t c, bool dim) { return dim ? mix(C_BG, c, 120) : c; }

void vgrad(int x, int y, int w, int h, uint16_t top, uint16_t bot) {
  for (int i = 0; i < h; i++) fb.drawFastHLine(x, y + i, w, mix(top, bot, h > 1 ? i * 255 / (h - 1) : 0));
}
void txt(int x, int y, const char* s, uint16_t c, int size = FS_S) {
  fb.setTextSize(size); fb.setTextColor(c); fb.setCursor(x, y); fb.print(s);
}
void txtR(int right, int y, const char* s, uint16_t c, int size = FS_S) {
  txt(right - (int)strlen(s) * CW * size, y, s, c, size);
}
void dot(int x, int y, bool ok) { fb.fillCircle(x, y, 2, ok ? C_OK : C_BAD); }

// Outlined device panel with a header strip. Returns the first content row (y).
struct Box { int x, y, w, h; int l() const { return x + PAD; } int r() const { return x + w - PAD; }
             int top() const { return y + HDR_H + 2; } int bot() const { return y + h - 2; } };
Box panel(int x, int y, int w, int h, const char* label, uint16_t accent) {
  fb.fillRoundRect(x, y, w, HDR_H + RADIUS, RADIUS, C_HDR);
  fb.fillRect(x + 1, y + HDR_H, w - 2, RADIUS, C_BG);
  fb.drawRoundRect(x, y, w, h, RADIUS, C_EDGE);
  fb.drawFastHLine(x + 1, y + HDR_H, w - 2, C_EDGE);
  txt(x + PAD, y + 1, label, accent);
  return Box{x, y, w, h};
}
// Header-right text / status dot for a panel.
void panelNote(const Box& b, const char* s, uint16_t c) { txtR(b.r(), b.y + 1, s, c); }
void panelDot(const Box& b, bool ok) { dot(b.r() - 2, b.y + 4, ok); }

void vword(int x, int y, int w, int h, const char* word, uint16_t ink) {
  int letters = 0, gaps = 0;
  for (const char* p = word; *p; p++) { if (*p == ' ') gaps++; else letters++; }
  int ch = 16, gap = 6, cw = 12;
  int block = letters * ch + gaps * gap;
  int yy = y + max(0, (h - block) / 2);
  int x0 = x + max(0, (w - cw) / 2);
  fb.setTextSize(FS_L); fb.setTextColor(ink);
  for (const char* p = word; *p; p++) {
    if (*p == ' ') { yy += gap; continue; }
    fb.setCursor(x0, yy);
    char s[2] = {*p, 0};
    fb.print(s);
    yy += ch;
  }
}
void fmtTemp(char* b, size_t n, float t) { if (isnan(t)) snprintf(b, n, "--"); else snprintf(b, n, "%.0fc", t); }
void fmtLoad(char* b, size_t n, float t) {
  if (isnan(t)) snprintf(b, n, "--");
  else if (t >= 100) snprintf(b, n, "%.0f", t);
  else snprintf(b, n, t >= 10 ? "%.1f" : "%.2f", t);
}
void fmtMem(char* b, size_t n, int m) { if (m < 0) snprintf(b, n, "--"); else snprintf(b, n, "%d%%", m); }
void fmtRate(char* b, size_t n, float bps) {
  if (isnan(bps) || bps < 0) { snprintf(b, n, "--"); return; }
  if (bps < 999.5f) snprintf(b, n, "%.0fB", bps);
  else if (bps < 9950.f) snprintf(b, n, "%.1fK", bps / 1000.f);
  else if (bps < 999500.f) snprintf(b, n, "%.0fK", bps / 1000.f);
  else if (bps < 9950000.f) snprintf(b, n, "%.1fM", bps / 1000000.f);
  else if (bps < 999500000.f) snprintf(b, n, "%.0fM", bps / 1000000.f);
  else snprintf(b, n, "%.1fG", bps / 1000000000.f);
}
void fmtUptime(char* b, size_t n, long s) {
  if (s < 0) snprintf(b, n, "--");
  else if (s < 3600) snprintf(b, n, "%ldm", s / 60);
  else if (s < 86400) snprintf(b, n, "%ldh%02ldm", s / 3600, (s % 3600) / 60);
  else snprintf(b, n, "%ldd%02ldh", s / 86400, (s % 86400) / 3600);
}
void fmtDbm(char* b, size_t n, int dbm) { if (dbm) snprintf(b, n, "%ddBm", dbm); else snprintf(b, n, "--"); }

float logRate(float bps) {
  if (isnan(bps) || bps <= 500.f) return 0.f;
  float t = log10f(bps / 500.f) / 5.3f;
  if (t < 0.f) return 0.f;
  if (t > 1.f) return 1.f;
  return t;
}

// Peak value across the last `window` samples (e.g. 30 samples = 60 s).
float recentPeak(const float* series, int n, int next, int window) {
  if (n <= 0) return NAN;
  int start = (n < HIST) ? 0 : next;
  int first = max(0, n - window);
  float pk = NAN;
  for (int i = first; i < n; i++) {
    float v = series[(start + i) % HIST];
    if (!isnan(v) && (isnan(pk) || v > pk)) pk = v;
  }
  return pk;
}
float lastValue(const float* series, int n, int next) {
  if (n <= 0) return NAN;
  int start = (n < HIST) ? 0 : next;
  return series[(start + n - 1) % HIST];
}

// Horizontal capsule filled along the shared ramp, warn notch and 60 s peak-hold pip.
void drawCapsule(int x, int y, int w, int h, float level, float peakLevel, float warnAt, bool dim) {
  if (isnan(level) || level < 0.f) level = 0.f;
  float shown = level > 1.f ? 1.f : level;
  fb.fillRoundRect(x, y, w, h, h / 2, C_TRACK);
  int inner = w - 2;
  int fw = (int)(shown * inner + 0.5f);
  for (int dx = 0; dx < fw; dx++) {
    float p = inner > 1 ? (float)dx / (float)(inner - 1) : 0.f;
    uint16_t col = dimmed(ramp(p, warnAt), dim);
    int topPad = (dx == 0 || dx == inner - 1) ? 2 : 1;
    int colH = h - topPad * 2;
    if (colH > 0) fb.drawFastVLine(x + 1 + dx, y + topPad, colH, col);
  }
  if (warnAt > 0.05f && warnAt < 0.95f) fb.drawFastVLine(x + 1 + (int)(warnAt * inner), y + 1, h - 2, C_BG);
  if (!isnan(peakLevel) && peakLevel > 0.02f) {
    float pk = peakLevel > 1.f ? 1.f : peakLevel;
    int px = x + 1 + (int)(pk * (inner - 1));
    uint16_t pipC = dimmed(pk >= warnAt ? C_HOT : C_TXT, dim);
    fb.drawFastVLine(px, y + 1, h - 2, pipC);
    if (px > x + 1) fb.drawFastVLine(px - 1, y + 1, h - 2, pipC);
  }
}

// Shaded sparkline. Each segment is colored by the shared ramp of its own value, so a
// rising temperature walks low -> warm -> hot exactly like the bars do.
// level(v) maps a raw sample to 0..1 on the same scale the metric's bar uses.
void drawSpark(int x, int y, int w, int h, const float* series, int n, int next,
               float lo, float hi, float (*level)(float, float), float k, float warnAt, bool dim) {
  fb.drawFastHLine(x, y + h - 1, w, C_TRACK);
  if (n <= 0 || !(hi > lo)) return;
  int start = (n < HIST) ? 0 : next;
  int prevX = -1, prevY = 0, lastX = -1, lastY = -1;
  uint16_t lastC = C_LOW;
  for (int i = 0; i < n; i++) {
    float v = series[(start + i) % HIST];
    if (isnan(v)) { prevX = -1; continue; }
    float t = (v - lo) / (hi - lo);
    if (t < 0.f) t = 0.f;
    if (t > 1.f) t = 1.f;
    int px = x + (n == 1 ? w - 1 : i * (w - 1) / (n - 1));
    int py = y + h - 1 - (int)(t * (h - 2) + 0.5f);
    uint16_t c = dimmed(ramp(level(v, k), warnAt), dim);
    if (prevX >= 0 && px >= prevX) {
      int span = max(1, px - prevX);
      uint16_t cTop = mix(C_BG, c, dim ? 65 : 115), cBot = mix(C_BG, c, dim ? 25 : 45);
      for (int cx = prevX; cx <= px; cx++) {
        int cy = prevY + (py - prevY) * (cx - prevX) / span;
        for (int sy = cy + 1; sy < y + h - 1; sy++)
          fb.drawPixel(cx, sy, mix(cTop, cBot, (sy - cy) * 255 / max(1, (y + h - 1 - cy))));
      }
      fb.drawLine(prevX, prevY, px, py, c);
    } else fb.drawPixel(px, py, c);
    prevX = px; prevY = py; lastX = px; lastY = py; lastC = c;
  }
  if (lastX >= 0 && !dim) fb.fillRect(max(x, lastX - 1), max(y, lastY - 1), 2, 2, lastC);
}
float lvTemp(float v, float) { return tempLevel(v); }
float lvFrac(float v, float k) { return k > 0 ? v / k : 0.f; }

bool seriesRange(const float* series, int n, int next, float minSpan, float& lo, float& hi) {
  int start = (n < HIST) ? 0 : next;
  bool any = false;
  for (int i = 0; i < n; i++) {
    float v = series[(start + i) % HIST];
    if (isnan(v)) continue;
    if (!any) { lo = hi = v; any = true; }
    else { if (v < lo) lo = v; if (v > hi) hi = v; }
  }
  if (!any) return false;
  if (hi - lo < minSpan) {
    float mid = (lo + hi) * 0.5f;
    lo = mid - minSpan * 0.5f;
    hi = mid + minSpan * 0.5f;
  }
  if (lo < 0) lo = 0;
  if (hi <= lo) hi = lo + minSpan;
  return true;
}

// 30s (15-sample) trend delta badge under the row label.
void drawDeltaBadge(int x, int y, const float* series, int n, int next, int rowIdx, bool dim) {
  if (n < 2) return;
  int start = (n < HIST) ? 0 : next;
  float cur = series[(start + n - 1) % HIST];
  if (isnan(cur)) return;
  int back = min(n - 1, 15);
  float ref = NAN;
  for (int k = back; k >= 1; k--) {
    float v = series[(start + n - 1 - k) % HIST];
    if (!isnan(v)) { ref = v; break; }
  }
  if (isnan(ref)) return;
  float d = cur - ref;
  char buf[10];
  uint16_t col = C_SOFT;
  if (rowIdx == 1) {
    int tenths = (int)roundf(fabsf(d) * 10.f);
    if (tenths == 0) snprintf(buf, sizeof buf, "=0.0");
    else if (tenths < 10) snprintf(buf, sizeof buf, "%s.%d", d > 0 ? "+" : "-", tenths);
    else if (fabsf(d) < 9.95f) snprintf(buf, sizeof buf, "%+.1f", d);
    else snprintf(buf, sizeof buf, "%+d", (int)roundf(d));
    if (d >= 0.25f) col = C_RISE; else if (d <= -0.25f) col = C_FALL;
  } else {
    int di = (int)roundf(d);
    const char* u = rowIdx == 0 ? "c" : "%";
    if (di > 0) snprintf(buf, sizeof buf, "+%d%s", di, u);
    else if (di < 0) snprintf(buf, sizeof buf, "%d%s", di, u);
    else snprintf(buf, sizeof buf, "=0%s", u);
    if (di >= 2) col = C_RISE; else if (di <= -2) col = C_FALL;
  }
  int bw = (int)strlen(buf) * CW + 2;
  fb.fillRoundRect(x - 1, y - 1, bw, 9, 2, C_HDR);
  txt(x, y, buf, dimmed(col, dim));
}

// Mirrored network waterfall: RX grows up, TX grows down.
void drawDuplexWaterfall(int x, int y, int w, int h, const float* rxS, const float* txS,
                         int n, int next, bool dim) {
  int cy = y + h / 2, maxBar = h / 2;
  fb.drawFastHLine(x, cy, w, C_EDGE);
  if (n <= 0) return;
  int start = (n < HIST) ? 0 : next;
  uint16_t cRx = dimmed(C_RX, dim), cTx = dimmed(C_TX, dim);
  int cols = min(n, w / 2), first = n - cols;
  for (int i = 0; i < cols; i++) {
    int idx = (start + first + i) % HIST;
    int bx = x + w - (cols - i) * 2;
    if (bx < x) continue;
    float r = logRate(rxS[idx]), t = logRate(txS[idx]);
    int rh = min(maxBar, (int)(r * maxBar + (r > 0.f ? 0.8f : 0.f)));
    int th = min(maxBar, (int)(t * maxBar + (t > 0.f ? 0.8f : 0.f)));
    if (rh > 0) { fb.drawFastVLine(bx, cy - rh, rh, cRx); fb.drawPixel(bx, cy - rh, mix(cRx, C_TXT, 110)); }
    if (th > 0) { fb.drawFastVLine(bx, cy + 1, th, cTx); fb.drawPixel(bx, cy + th, mix(cTx, C_TXT, 110)); }
  }
}

// Vertical per-core bars on the shared ramp; the bar width grows to fill the box.
void drawCoreBars(int x, int y, int w, int h, const uint8_t* cv, int cn) {
  int bw = max(2, (w - GAP * (cn - 1)) / cn);
  int used = cn * bw + GAP * (cn - 1);
  x += max(0, (w - used) / 2);
  for (int i = 0; i < cn; i++) {
    int bx = x + i * (bw + GAP);
    int fh = (int)(cv[i] * (h - 2) / 100.f + 0.5f);
    fb.fillRect(bx, y, bw, h, C_TRACK);
    uint16_t col = ramp(cv[i] / 100.f, 0.6f);
    if (fh > 0) vgrad(bx, y + h - 1 - fh, bw, fh, mix(col, C_TXT, 60), col);
  }
  for (int q = 1; q < 4; q++) {   // 25/50/75 % guide ticks on the left edge
    int gy = y + h - 1 - q * (h - 2) / 4;
    fb.drawPixel(x - 2, gy, C_EDGE);
  }
}

// Num / Caps / Scroll lock indicators: three small dots stacked at the foot of the rail,
// lit = C_FALL, off = a hollow ring in the rail's ink colour.
void drawRailLeds(int railW, const Status& s, uint16_t ink) {
  if (!s.host) return;
  const bool on[3] = {s.ledNum, s.ledCaps, s.ledScroll};
  int cx = railW / 2;
  for (int i = 0; i < 3; i++) {
    int cy = H - 26 + i * 9;
    if (on[i]) { fb.fillCircle(cx, cy, 3, C_FALL); fb.drawCircle(cx, cy, 3, rgb(8, 24, 16)); }
    else fb.drawCircle(cx, cy, 2, ink);
  }
}

// Shared ZERO / GEEK detail panels (both pages). `rows` = how many text lines fit.
void drawZeroPanel(int x, int y, int w, int h, const Status& s, int rows) {
  Box b = panel(x, y, w, h, "ZERO", s.api ? C_TXT : C_DIM);
  panelDot(b, s.api);
  char a[24], c[24], d[24], line[40];
  int ly = b.top();
  if (!s.api) { txt(b.l(), ly, s.wifi ? "no zero" : "wifi down", C_DIM); return; }
  fmtUptime(a, sizeof a, s.upS);
  txt(b.l(), ly, "up", C_DIM); txt(b.l() + 3 * CW, ly, a, C_VAL);
  fmtDbm(c, sizeof c, s.zRssi);
  uint16_t rc = !s.zRssi ? C_DIM : s.zRssi < -75 ? C_WARN : C_VAL;
  if (rows >= 4) txtR(b.r(), ly, c, rc);   // narrow panel: the Zero's RSSI moves to the ts line
  ly += LINE;
  if (rows >= 3) {
    fmtTemp(a, sizeof a, s.temp); fmtLoad(c, sizeof c, s.load1); fmtMem(d, sizeof d, s.mem);
    snprintf(line, sizeof line, "%s ld%s m%s", a, c, d);
    txt(b.l(), ly, line, C_VAL); ly += LINE;
  }
  txt(b.l(), ly, "ts", C_DIM);
  if (s.ts >= 0) { dot(b.l() + 3 * CW + 2, ly + 3, s.ts == 1); txt(b.l() + 4 * CW + 2, ly, s.ts == 1 ? "up" : "down", C_VAL); }
  else txt(b.l() + 3 * CW, ly, "--", C_DIM);
  if (rows < 4) { txtR(b.r(), ly, c, rc); return; }
  {
    ly += LINE;
    txt(b.l(), ly, "serial", C_DIM);
    txt(b.l() + 7 * CW, ly, s.serialMode[0] ? s.serialMode : "--", s.serialMode[0] ? C_VAL : C_DIM);
  }
}
void drawGeekPanel(int x, int y, int w, int h, const Status& s, int rows) {
  Box b = panel(x, y, w, h, "GEEK", s.wifi ? C_TXT : C_DIM);
  panelDot(b, s.wifi);
  char a[24], c[24];
  int ly = b.top();
  if (!s.wifi) { txt(b.l(), ly, "wifi down", C_BAD); }
  else {
    fmtDbm(a, sizeof a, s.rssi);
    txt(b.l(), ly, "wifi", C_DIM); txt(b.l() + 5 * CW, ly, a, s.rssi < -75 ? C_WARN : C_VAL);
  }
  if (gPinBad) panelNote(b, "PIN!", C_BAD);
  ly += LINE;
  txt(b.l(), ly, s.wifi ? s.geekIp : "--", s.wifi ? C_VAL : C_DIM);
  if (rows < 3) return;
  ly += LINE;
  snprintf(a, sizeof a, "%uK", (unsigned)(ESP.getFreeHeap() / 1024));
  txt(b.l(), ly, "heap", C_DIM); txt(b.l() + 5 * CW, ly, a, C_VAL);
  if (s.api) { snprintf(c, sizeof c, "%ums", (unsigned)s.latencyMs); txtR(b.r(), ly, c, s.latencyMs > 800 ? C_WARN : C_DIM); }
  if (rows < 4) return;
  ly += LINE;
  fmtUptime(a, sizeof a, (long)(millis() / 1000));
  txt(b.l(), ly, "up", C_DIM); txt(b.l() + 3 * CW, ly, a, C_VAL);
  if (s.polls) { snprintf(c, sizeof c, "%u/%u", (unsigned)s.fails, (unsigned)s.polls); txtR(b.r(), ly, c, C_DIM); }
}

enum Page { P_BOARD, P_KVM, P_DESK, P_TERM, NPAGES };
int page = P_BOARD;

// Page 1, the scoreboard. Status rail | PC panel (3 metric rows + net) over ZERO | GEEK.
void drawBoard(const Status& s, const PcHist& h, uint32_t now) {
  bool nums = !isnan(s.pcTemp) && !isnan(s.pcLoad) && s.pcMem >= 0;
  bool linked = s.api && strcmp(s.link, "LINKED") == 0 && nums;
  bool nohost = s.api && strcmp(s.link, "NO HOST") == 0;
  // Cable still in, helper quiet: hold the last PC figures. Green stays off; the
  // reading is not live. Unplugging the cable skips this and shows NO HOST at once.
  bool reconnecting = s.api && !linked && !nohost && h.had
                      && (uint32_t)(now - h.lastMs) < RECONNECT_MS;
  bool showPc = linked || reconnecting;
  uint16_t top, bot, ink;
  const char* word;
  if (!s.api) { top = rgb(50, 52, 60); bot = rgb(20, 22, 28); ink = rgb(180, 184, 196); word = "ZERO"; }
  else if (linked) { top = rgb(94, 234, 144); bot = rgb(18, 122, 66); ink = rgb(4, 34, 8); word = "LINKED"; }
  else if (nohost) { top = rgb(255, 210, 122); bot = rgb(138, 90, 16); ink = rgb(42, 24, 4); word = "NO HOST"; }
  else { top = rgb(126, 182, 238); bot = rgb(26, 78, 134); ink = rgb(4, 16, 24); word = "KEYS"; }
  // Thin state rail: colour carries the state, one letter names it (L/N/K/Z).
  const int RAIL = 14, PX = RAIL + GAP, PW = W - PX, PH = 100, BY = PH + GAP, BH = H - BY;
  vgrad(0, 0, RAIL, H, top, bot);
  // Full state word, stacked 6x8 letters on a 10 px pitch; a space is a 5 px gap.
  // Longest (NO HOST) ends at y=67, the lock dots start at y=106.
  for (int i = 0, yy = 4; word[i]; i++) {
    if (word[i] == ' ') { yy += 5; continue; }
    char ch[2] = {word[i], 0};
    txt((RAIL - CW) / 2 + 1, yy, ch, ink);
    yy += 10;
  }
  drawRailLeds(RAIL, s, ink);

  // Main panel: the PC when linked / held, otherwise the Zero's own figures.
  Box p = panel(PX, 0, PW, PH, showPc ? "PC" : "ZERO", reconnecting ? C_WARN : showPc ? C_OK : C_DIM);
  if (showPc) {
    int pDisk = linked ? s.pcDisk : h.lastDisk;
    const char* pProc = linked ? s.pcTopProc : h.lastTopProc;
    float pCpu = linked ? s.pcTopCpu : h.lastTopCpu;
    char hdr[40] = "", tmp[24] = "";
    if (pDisk >= 0) snprintf(hdr, sizeof hdr, "/:%d%% ", pDisk);
    if (pProc && pProc[0]) {
      if (!isnan(pCpu) && pCpu >= 1.f) snprintf(tmp, sizeof tmp, "%s %.0f%%", pProc, pCpu);
      else snprintf(tmp, sizeof tmp, "%s", pProc);
      strlcat(hdr, tmp, sizeof hdr);
    }
    hdr[(p.w - 4 * CW) / CW] = 0;
    if (hdr[0]) panelNote(p, hdr, dimmed(pDisk >= 90 ? C_WARN : C_DIM, reconnecting));
  }

  const int NUM_R = PX + 62, LAB_X = NUM_R + 5, SP_X = LAB_X + 34, SP_W = p.r() - SP_X;
  const int ROW_Y[3] = {p.top() + 1, p.top() + 25, p.top() + 49};
  float ft, fl; int fm;
  uint16_t numC = C_TXT;
  if (showPc && linked) { ft = s.pcTemp; fl = s.pcLoad; fm = s.pcMem; }
  else if (showPc) { ft = h.lastT; fl = h.lastL; fm = isnan(h.lastM) ? -1 : (int)(h.lastM + 0.5f); numC = C_SOFT; }
  else { ft = s.temp; fl = s.load1; fm = s.mem; }
  char fig[3][16];
  fmtTemp(fig[0], sizeof fig[0], ft); fmtLoad(fig[1], sizeof fig[1], fl); fmtMem(fig[2], sizeof fig[2], fm);
  const char* lab[3] = {"temp", "load", "mem"};
  float ncpu = showPc ? (linked ? s.pcNcpu : h.lastNcpu) : 1.f;
  if (!(ncpu >= 1.f)) ncpu = showPc ? 4.f : 1.f;
  float level[3] = {tempLevel(ft), fl / ncpu, fm < 0 ? NAN : (float)fm / 100.f};
  float warnAt[3] = {tempLevel(80.f), 0.75f, 0.85f};
  float peaks[3] = {showPc ? tempLevel(recentPeak(h.temp, h.n, h.next, 30)) : NAN,
                    showPc ? recentPeak(h.load, h.n, h.next, 30) / ncpu : NAN,
                    showPc ? recentPeak(h.mem, h.n, h.next, 30) / 100.f : NAN};
  for (int i = 0; i < 3; i++) {
    txtR(NUM_R, ROW_Y[i] + 2, fig[i], numC, FS_L);
    txt(LAB_X, ROW_Y[i], lab[i], C_DIM);
    drawCapsule(SP_X, ROW_Y[i], SP_W, 7, level[i], peaks[i], warnAt[i], reconnecting);
  }
  if (showPc) {
    const float* ser[3] = {h.temp, h.load, h.mem};
    float minSpan[3] = {8.f, 0.5f, 10.f};
    for (int i = 0; i < 3; i++) {
      drawDeltaBadge(LAB_X, ROW_Y[i] + 9, ser[i], h.n, h.next, i, reconnecting);
      float lo = 0.f, hi = 0.f;
      if (!seriesRange(ser[i], h.n, h.next, minSpan[i], lo, hi)) continue;
      if (i == 0) drawSpark(SP_X, ROW_Y[i] + 9, SP_W, 13, ser[i], h.n, h.next, lo, hi, lvTemp, 0, warnAt[i], reconnecting);
      else drawSpark(SP_X, ROW_Y[i] + 9, SP_W, 13, ser[i], h.n, h.next, lo, hi, lvFrac, i == 1 ? ncpu : 100.f, warnAt[i], reconnecting);
    }
  }

  // Net strip along the bottom of the main panel.
  int ny = p.bot() - 12;
  fb.drawFastHLine(p.x + 1, ny - 2, p.w - 2, C_EDGE);
  if (reconnecting) {
    fb.fillRoundRect(p.l(), ny, p.w - 2 * PAD, 11, RADIUS, rgb(72, 44, 8));
    uint16_t toastC = ((now / 400) & 1) ? C_WARM : mix(C_BG, C_WARM, 150);
    txt(p.x + (p.w - 12 * CW) / 2, ny + 2, "reconnecting", toastC);
  } else if (linked) {
    char dn[8], up[8];
    fmtRate(dn, sizeof dn, s.pcRx); fmtRate(up, sizeof up, s.pcTx);
    int x = p.l();
    txt(x, ny + 2, "dn", C_RX); txtR(x + 42, ny + 2, dn, C_TXT);
    txt(x + 46, ny + 2, "up", C_TX); txtR(x + 88, ny + 2, up, C_TXT);
    drawDuplexWaterfall(SP_X, ny - 1, SP_W, 13, h.rx, h.tx, h.n, h.next, false);
  } else {
    char l1[40];
    if (!s.api) snprintf(l1, sizeof l1, "%s", s.wifi ? "no zero" : "wifi down");
    else if (nohost) snprintf(l1, sizeof l1, "no host  zero %s", s.ip);
    else snprintf(l1, sizeof l1, "searching for helper");
    txt(p.l(), ny + 2, l1, C_SOFT);
  }

  int zw = (PW - GAP) / 2;
  drawZeroPanel(PX, BY, zw, BH, s, 2);
  drawGeekPanel(PX + zw + GAP, BY, PW - zw - GAP, BH, s, 2);
}

// Page 2: the KVM. PC panel (big per-core bars + temp sparklines + gpu/uptime) on top,
// ZERO and GEEK detail panels underneath. Every missing field shows "--".
void drawKvm(const Status& s, const PcHist& h) {
  char a[24], b[24];
  bool linked = s.api && strcmp(s.link, "LINKED") == 0;
  bool nohost = s.api && strcmp(s.link, "NO HOST") == 0;
  const char* lw = !s.api ? "NO ZERO" : linked ? "LINKED" : nohost ? "NO HOST" : "waiting";
  uint16_t lc = !s.api ? C_DIM : linked ? C_OK : nohost ? C_WARN : C_LOW;
  const int PH = 82, BY = PH + GAP, BH = H - BY;
  Box p = panel(0, 0, W, PH, "PC", lc);
  txt(p.l() + 3 * CW, p.y + 1, lw, lc);
  if (s.pcIp[0]) panelNote(p, s.pcIp, C_VAL);

  // left: per-core bars (type 8: every core; else main's top-8, hottest first)
  const int CX = p.l() + 3, CWID = 128, DIV = CX + CWID + 4, CY = p.top() + 1, CH = 44;
  const uint8_t* cv = s.coresAllN ? s.coresAll : s.pcCores;
  int cn = s.coresAllN ? s.coresAllN : s.pcCoreCount;
  if (!linked || cn == 0) {
    fb.drawRect(CX, CY, CWID, CH, C_TRACK);
    txt(CX + (CWID - 12 * CW) / 2, CY + CH / 2 - 4, linked ? "no core data" : "--", C_DIM);
  } else drawCoreBars(CX, CY, CWID, CH, cv, cn);
  int ty = CY + CH + 4;
  if (linked && cn) {
    if (s.coresTotal > cn) snprintf(a, sizeof a, "%dc +%d", cn, s.coresTotal - cn);
    else snprintf(a, sizeof a, s.coresAllN ? "%d cores" : "top %d", cn);
  } else snprintf(a, sizeof a, "cores --");
  txt(p.l(), ty, a, C_DIM);
  txt(p.l() + 66, ty, "gpu", C_DIM);
  if (linked && !isnan(s.pcGpu)) { snprintf(b, sizeof b, "%.0f%%", s.pcGpu); txt(p.l() + 66 + 4 * CW, ty, b, ramp(s.pcGpu / 100.f, 0.75f)); }
  else txt(p.l() + 66 + 4 * CW, ty, "--", C_DIM);
  ty += LINE;
  fmtUptime(a, sizeof a, linked ? s.pcUpS : -1);
  txt(p.l(), ty, "up", C_DIM); txt(p.l() + 3 * CW, ty, a, linked && s.pcUpS >= 0 ? C_VAL : C_DIM);
  fmtLoad(b, sizeof b, linked ? s.pcLoad : NAN);
  txt(p.l() + 66, ty, "ld", C_DIM); txt(p.l() + 66 + 3 * CW, ty, b, linked && !isnan(s.pcLoad) ? C_VAL : C_DIM);
  fb.drawFastVLine(DIV, p.top(), p.bot() - p.top(), C_EDGE);

  // right: temperature sparklines (rolling 2 min). CPU falls back to the type-1 temp.
  const int SX = DIV + PAD, SW = p.r() - SX;
  bool cpuSensor = linked && !isnan(s.pcCpuT);
  const float* ser[3] = {cpuSensor ? h.cpuT : h.temp, h.gpuT, h.nvT};
  float cur[3] = {linked ? (cpuSensor ? s.pcCpuT : s.pcTemp) : NAN, linked ? s.pcGpuT : NAN, linked ? s.pcNvmeT : NAN};
  float warnC[3] = {85.f, 85.f, 70.f};
  const char* nm[3] = {"cpu", "gpu", "nvme"};
  for (int i = 0; i < 3; i++) {
    int y = p.top() + i * 22;
    txt(SX, y, nm[i], C_DIM);
    fmtTemp(a, sizeof a, cur[i]);
    float wl = tempLevel(warnC[i]);
    txtR(p.r(), y, a, isnan(cur[i]) ? C_DIM : ramp(tempLevel(cur[i]), wl));
    float pk = linked ? recentPeak(ser[i], h.n, h.next, HIST) : NAN;
    if (!isnan(pk)) { snprintf(b, sizeof b, "^%.0f", pk); txt(SX + 5 * CW, y, b, mix(C_BG, C_DIM, 200)); }
    float lo = 0.f, hi = 0.f;
    if (linked && seriesRange(ser[i], h.n, h.next, 6.f, lo, hi))
      drawSpark(SX, y + 9, SW, 11, ser[i], h.n, h.next, lo, hi, lvTemp, 0, wl, false);
    else fb.drawFastHLine(SX, y + 19, SW, C_TRACK);
  }

  int zw = (W - GAP) / 2;
  drawZeroPanel(0, BY, zw, BH, s, 4);
  drawGeekPanel(zw + GAP, BY, W - zw - GAP, BH, s, 4);
}

void drawWaiting() {
  vgrad(0, 0, W, H, rgb(126, 182, 238), rgb(18, 60, 110));
  vword((W - 72) / 2, 0, 72, H, "KEYS", rgb(4, 16, 24));
  if (gPinBad) txt(78, 120, "bad PIN (portal)", rgb(120, 10, 10), 1);
  else txt(99, 120, "waiting", rgb(4, 20, 32), 1);
}

void drawTerm() {
  Term t; xSemaphoreTake(gLock, portMAX_DELAY); t = gTerm; xSemaphoreGive(gLock);
  fb.fillScreen(0x0000);
  uint16_t fg = rgb(170, 255, 170);
  if (gPinBad) { txt(4, 4, "bad PIN: set it in the setup portal", C_BAD); txt(4, 14, "(retrying in a few minutes)", C_DIM); return; }
  if (t.stampMs == 0) txt(4, 4, "loading...", C_DIM);
  else if (t.code != 200) {
    const char* msg = t.code == 401 ? "wrong PIN (setup portal)" : t.code == 429 ? "PIN locked out"
                    : t.code <= 0 ? "Zero unreachable" : nullptr;
    if (msg) txt(4, 4, msg, C_BAD);
    else { char b[24]; snprintf(b, sizeof b, "HTTP %d", t.code); txt(4, 4, b, C_BAD); }
  } else if (t.n == 0) txt(4, 4, t.open ? "(no output yet)" : "serial port closed", C_DIM);
  for (int i = 0; i < t.n; i++) txt(2, 2 + i * 8, t.row[i], fg);
  if (t.code == 200) fb.fillCircle(W - 5, 6, 2, t.open ? C_OK : C_WARN);
}

void paint() {
  fb.setTextWrap(false);
  if (page == P_DESK) {
    bool show = false;
    if (xSemaphoreTake(gLock, pdMS_TO_TICKS(20)) == pdTRUE) {
      if (gFrameBusy) show = gShown;
      else if (gFrameOk) {
        memcpy(fb.getBuffer(), gFrame, sizeof gFrame);
        gShown = true;
        show = true;
      } else gShown = false;
      xSemaphoreGive(gLock);
    } else show = gShown;
    if (!show) drawWaiting();
    tft.drawRGBBitmap(0, 0, fb.getBuffer(), W, H);
    return;
  }
  static Status s; static PcHist h;   // static: PcHist is ~2 KB, keep it off the loop stack
  xSemaphoreTake(gLock, portMAX_DELAY); s = gStat; h = gHist; xSemaphoreGive(gLock);
  fb.fillScreen(C_BG);
  if (page == P_TERM) drawTerm();
  else if (page == P_KVM) drawKvm(s, h);
  else drawBoard(s, h, millis());
  tft.drawRGBBitmap(0, 0, fb.getBuffer(), W, H);
}

void go(int n) {
  page = (n % NPAGES + NPAGES) % NPAGES;
  gTermOn = page == P_TERM;
  bool desk = page == P_DESK;
  if (desk && !gDeskOn) gFramePull = true;
  gDeskOn = desk;
  if (!desk) {
    gShown = false;
    xSemaphoreTake(gLock, portMAX_DELAY);
    gFrameOk = false;
    xSemaphoreGive(gLock);
  }
}

void setup() {
  Serial.begin(115200);
  for (int p : {BTN_BACK, BTN_UP, BTN_DOWN, BTN_SELECT}) if (p >= 0) pinMode(p, INPUT_PULLUP);
  pinMode(LCD_BL, OUTPUT); digitalWrite(LCD_BL, LOW);   // light the panel only once it is cleared
  SPI.begin(LCD_SCLK, -1, LCD_MOSI, LCD_CS);
  tft.init(135, 240); tft.setSPISpeed(40000000); tft.setRotation(1);
  tft.fillScreen(ST77XX_BLACK);
  digitalWrite(LCD_BL, HIGH);
  if (!fb.getBuffer()) {
    tft.fillScreen(ST77XX_RED); tft.setTextColor(ST77XX_WHITE); tft.setTextSize(2);
    tft.setCursor(6, 50); tft.print("no RAM for UI"); Serial.println("framebuffer alloc failed");
    for (;;) delay(1000);
  }
  loadCfg();
  bool bootHeld = digitalRead(BTN_BACK) == LOW;
#ifdef KVM_PLACEHOLDER_SECRETS
  bool unconfigured = !cfgSaved();
#else
  bool unconfigured = false;
#endif
  if (bootHeld || unconfigured) setupPortal();
  gLock = xSemaphoreCreateMutex();
  WiFi.mode(WIFI_STA); WiFi.setAutoReconnect(true);
  WiFi.begin(cfg.ssid.c_str(), cfg.pass.length() ? cfg.pass.c_str() : nullptr);
  xTaskCreatePinnedToCore(netTask, "net", 12288, nullptr, 1, nullptr, 0);
}

void loop() {
  uint32_t now = millis();
  Btn* btns[] = {&bBack, &bUp, &bDown, &bSel};
  for (Btn* b : btns) if (b->poll(now) == EV_SHORT) go(page + 1);
  static uint32_t lastPaint = 0;
  if (now - lastPaint >= 200) { lastPaint = now; paint(); }
  delay(2);
}
