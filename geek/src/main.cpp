// ESP32-S3-GEEK KVM panel. Three pages, one short press to advance:
//   scoreboard → desktop still → terminal → scoreboard.
// A long press and a double press do nothing. There is no menu.
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
#error "add #define KVM_PIN \"<pin>\" to secrets.h (see secrets.example.h)"
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
  long upS = -1;
  uint32_t latencyMs = 0, stampMs = 0, polls = 0, fails = 0;
};
Status gStat;
// One sample per /api/status poll (2 s). Gaps are NAN so a quiet helper breaks the line
// instead of drawing across the outage. last* is the last live PC reading.
static const int HIST = 60;
static const uint32_t RECONNECT_MS = 60000;
struct PcHist {
  float temp[HIST], load[HIST], mem[HIST], rx[HIST], tx[HIST];
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
  xSemaphoreGive(gLock);
}

void pollTerm() {
  Term t; t.n = 0;
  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http; http.setTimeout(1500); http.setConnectTimeout(1500);
    http.begin(BASE + "/api/term?n=" + TROWS); http.addHeader("X-KVM-PIN", cfg.pin);
    t.code = http.GET();
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
  uint32_t last = 0, lastTerm = 0, lastFrame = 0;
  for (;;) {
    uint32_t now = millis();
    if (now - last > 2000) { last = now; pollOnce(); }
    if (gTermOn && now - lastTerm > 800) { lastTerm = now; pollTerm(); }
    if (gDeskOn && (gFramePull || now - lastFrame > 2000)) {
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
Btn bBack{BTN_BACK, true}, bUp{BTN_UP, true}, bDown{BTN_DOWN, true}, bSel{BTN_SELECT, true};

uint16_t rgb(uint8_t r, uint8_t g, uint8_t b) { return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3); }
uint16_t mix(uint16_t a, uint16_t b, int t) {
  if (t <= 0) return a;
  if (t >= 255) return b;
  int ar = a >> 11, ag = (a >> 5) & 63, ab = a & 31, br = b >> 11, bg = (b >> 5) & 63, bb = b & 31;
  return ((ar + (br - ar) * t / 255) << 11) | ((ag + (bg - ag) * t / 255) << 5) | (ab + (bb - ab) * t / 255);
}
const uint16_t C_BG = rgb(12, 16, 24), C_TXT = 0xFFFF, C_DIM = rgb(130, 140, 165),
               C_OK = rgb(60, 220, 120), C_WARN = rgb(255, 170, 40), C_BAD = rgb(255, 70, 70);

void vgrad(int x, int y, int w, int h, uint16_t top, uint16_t bot) {
  for (int i = 0; i < h; i++) fb.drawFastHLine(x, y + i, w, mix(top, bot, h > 1 ? i * 255 / (h - 1) : 0));
}
void txt(int x, int y, const char* s, uint16_t c, int size = 1) {
  fb.setTextSize(size); fb.setTextColor(c); fb.setCursor(x, y); fb.print(s);
}
void vword(int x, int y, int w, int h, const char* word, uint16_t ink) {
  int letters = 0, gaps = 0;
  for (const char* p = word; *p; p++) { if (*p == ' ') gaps++; else letters++; }
  int ch = 16, gap = 6, cw = 12;
  int block = letters * ch + gaps * gap;
  int yy = y + max(0, (h - block) / 2);
  int x0 = x + max(0, (w - cw) / 2);
  fb.setTextSize(2); fb.setTextColor(ink);
  for (const char* p = word; *p; p++) {
    if (*p == ' ') { yy += gap; continue; }
    fb.setCursor(x0, yy);
    char s[2] = {*p, 0};
    fb.print(s);
    yy += ch;
  }
}
void txtR(int right, int y, const char* s, uint16_t c, int size) {
  txt(right - (int)strlen(s) * 6 * size, y, s, c, size);
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

// 7px horizontal plasma gradient capsule with warn threshold notch and 60s peak-hold pip.
void drawPlasmaCapsule(int x, int y, int w, int h, float level, float peakLevel,
                       float warnAt, uint16_t cLow, uint16_t cWarm, uint16_t cHot, bool dim) {
  if (isnan(level) || level < 0.f) level = 0.f;
  float shown = level > 1.f ? 1.f : level;
  fb.fillRoundRect(x, y, w, h, h / 2, rgb(22, 30, 44));
  int inner = w - 2;
  int fw = (int)(shown * inner + 0.5f);
  float wSplit = (warnAt > 0.05f && warnAt < 0.95f) ? warnAt : 0.75f;
  for (int dx = 0; dx < fw; dx++) {
    float p = inner > 1 ? (float)dx / (float)(inner - 1) : 0.f;
    uint16_t col;
    if (p < wSplit) col = mix(cLow, cWarm, (int)(p / wSplit * 255.f));
    else col = mix(cWarm, cHot, (int)((p - wSplit) / (1.f - wSplit) * 255.f));
    if (dim) col = mix(rgb(28, 36, 52), col, 110);
    int topPad = (dx == 0 || dx == inner - 1) ? 2 : 1;
    int colH = h - topPad * 2;
    if (colH > 0) fb.drawFastVLine(x + 1 + dx, y + topPad, colH, col);
  }
  if (warnAt > 0.05f && warnAt < 0.95f) {
    int nx = x + 1 + (int)(warnAt * inner);
    fb.drawFastVLine(nx, y + 1, h - 2, rgb(8, 10, 16));
  }
  if (!isnan(peakLevel) && peakLevel > 0.02f) {
    float pk = peakLevel > 1.f ? 1.f : peakLevel;
    int px = x + 1 + (int)(pk * (inner - 1));
    uint16_t pipC = dim ? rgb(140, 132, 100) : (pk >= warnAt ? rgb(255, 120, 80) : rgb(255, 245, 190));
    fb.drawFastVLine(px, y + 1, h - 2, pipC);
    if (px > x + 1) fb.drawFastVLine(px - 1, y + 1, h - 2, pipC);
  }
}

// 13px shaded Tufte horizon 60-sample sparkline with warning-threshold ember tint.
void drawHorizonSpark(int x, int y, int w, int h, const float* series, int n, int next,
                      float lo, float hi, float warnVal, uint16_t col, bool dim) {
  fb.drawFastHLine(x, y + h - 1, w, rgb(24, 32, 48));
  if (n <= 0 || !(hi > lo)) return;
  int start = (n < HIST) ? 0 : next;
  uint16_t baseCol = dim ? mix(C_BG, col, 120) : col;
  uint16_t emberCol = dim ? rgb(150, 80, 60) : rgb(255, 92, 52);
  int prevX = -1, prevY = 0;
  float prevV = NAN;
  int lastX = -1, lastY = -1;
  uint16_t lastC = baseCol;
  for (int i = 0; i < n; i++) {
    float v = series[(start + i) % HIST];
    if (isnan(v)) { prevX = -1; continue; }
    float t = (v - lo) / (hi - lo);
    if (t < 0.f) t = 0.f;
    if (t > 1.f) t = 1.f;
    int px = x + (n == 1 ? w - 1 : i * (w - 1) / (n - 1));
    int py = y + h - 1 - (int)(t * (h - 2) + 0.5f);
    uint16_t ptCol = (!isnan(warnVal) && v >= warnVal) ? emberCol : baseCol;
    if (prevX >= 0 && px >= prevX) {
      int span = max(1, px - prevX);
      for (int cx = prevX; cx <= px; cx++) {
        int k = cx - prevX;
        int cy = prevY + (py - prevY) * k / span;
        float cv = prevV + (v - prevV) * (float)k / (float)span;
        uint16_t cLine = (!isnan(warnVal) && cv >= warnVal) ? emberCol : baseCol;
        uint16_t cTop = mix(C_BG, cLine, dim ? 65 : 115);
        uint16_t cBot = mix(C_BG, cLine, dim ? 25 : 45);
        for (int sy = cy + 1; sy < y + h - 1; sy++) {
          int st = (sy - cy) * 255 / max(1, (y + h - 1 - cy));
          fb.drawPixel(cx, sy, mix(cTop, cBot, st));
        }
      }
      fb.drawLine(prevX, prevY, px, py, ptCol);
    } else {
      fb.drawPixel(px, py, ptCol);
    }
    prevX = px; prevY = py; prevV = v;
    lastX = px; lastY = py; lastC = ptCol;
  }
  if (lastX >= 0 && !dim) {
    fb.fillRect(max(x, lastX - 1), max(y, lastY - 1), 2, 2, lastC);
  }
}

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
  uint16_t col = rgb(100, 112, 136);
  if (rowIdx == 0) {
    int di = (int)roundf(d);
    snprintf(buf, sizeof buf, di > 0 ? "+%dc" : di < 0 ? "%dc" : "=0c", di);
    if (di >= 2) col = rgb(255, 176, 64);
    else if (di <= -2) col = rgb(80, 210, 160);
  } else if (rowIdx == 1) {
    int tenths = (int)roundf(fabsf(d) * 10.f);
    if (tenths == 0) snprintf(buf, sizeof buf, "=0.0");
    else if (tenths < 10) snprintf(buf, sizeof buf, "%s.%d", d > 0 ? "+" : "-", tenths);
    else if (fabsf(d) < 9.95f) snprintf(buf, sizeof buf, "%+.1f", d);
    else snprintf(buf, sizeof buf, "%+d", (int)roundf(d));
    if (d >= 0.25f) col = rgb(255, 176, 64);
    else if (d <= -0.25f) col = rgb(80, 210, 160);
  } else {
    int di = (int)roundf(d);
    snprintf(buf, sizeof buf, di > 0 ? "+%d%%" : di < 0 ? "%d%%" : "=0%%", di);
    if (di >= 2) col = rgb(255, 176, 64);
    else if (di <= -2) col = rgb(80, 210, 160);
  }
  if (dim) col = mix(C_BG, col, 120);
  txt(x, y, buf, col, 1);
}

// 8 micro-LED chiclets under the load label, colored slate -> cyan -> amber -> red.
void drawCoreChiclets(int x, int y, const uint8_t* cores, int count, bool dim) {
  for (int i = 0; i < 8; i++) {
    uint16_t col = rgb(24, 30, 44);
    if (i < count) {
      int u = cores[i];
      if (u < 20) col = mix(rgb(52, 68, 92), rgb(60, 140, 190), u * 255 / 20);
      else if (u < 60) col = mix(rgb(60, 180, 240), rgb(80, 230, 255), (u - 20) * 255 / 40);
      else if (u < 85) col = mix(rgb(255, 196, 56), rgb(255, 140, 32), (u - 60) * 255 / 25);
      else col = rgb(255, 60, 60);
      if (dim) col = mix(C_BG, col, 110);
    }
    fb.fillRect(x + i * 4, y, 3, 3, col);
  }
}

// Mirrored bi-directional network waterfall: RX bars grow up in emerald, TX bars down in amber.
void drawDuplexWaterfall(int x, int y, int w, int h, const float* rxS, const float* txS,
                         int n, int next, bool dim) {
  int cy = y + h / 2;
  int maxBar = h / 2;
  fb.drawFastHLine(x, cy, w, rgb(34, 46, 66));
  if (n <= 0) return;
  int start = (n < HIST) ? 0 : next;
  uint16_t cRx = dim ? rgb(40, 120, 75) : rgb(48, 224, 136);
  uint16_t cRxTip = dim ? rgb(60, 150, 95) : rgb(130, 255, 190);
  uint16_t cTx = dim ? rgb(135, 95, 35) : rgb(245, 165, 45);
  uint16_t cTxTip = dim ? rgb(165, 120, 50) : rgb(255, 220, 120);
  int cols = min(n, 41);
  int first = n - cols;
  for (int i = 0; i < cols; i++) {
    int idx = (start + first + i) % HIST;
    int bx = x + w - (cols - i) * 2;
    if (bx < x) continue;
    float r = logRate(rxS[idx]);
    float t = logRate(txS[idx]);
    int rh = (int)(r * maxBar + (r > 0.f ? 0.8f : 0.f));
    int th = (int)(t * maxBar + (t > 0.f ? 0.8f : 0.f));
    if (rh > maxBar) rh = maxBar;
    if (th > maxBar) th = maxBar;
    if (rh > 0) {
      fb.drawFastVLine(bx, cy - rh, rh, cRx);
      fb.drawPixel(bx, cy - rh, cRxTip);
    }
    if (th > 0) {
      fb.drawFastVLine(bx, cy + 1, th, cTx);
      fb.drawPixel(bx, cy + th, cTxTip);
    }
  }
}

// Num / Caps / Scroll lock indicators on the bottom of the 52px status rail.
void drawRailLeds(int railW, const Status& s, uint16_t ink) {
  if (!s.host) return;
  const char labels[3] = {'N', 'C', 'S'};
  const bool on[3] = {s.ledNum, s.ledCaps, s.ledScroll};
  int y = H - 11;
  for (int i = 0; i < 3; i++) {
    int bx = 5 + i * 15;
    if (on[i]) {
      fb.fillRoundRect(bx, y, 13, 9, 2, rgb(8, 24, 16));
      txt(bx + 4, y + 1, (char[2]){labels[i], 0}, rgb(120, 255, 180), 1);
    } else {
      txt(bx + 4, y + 1, (char[2]){labels[i], 0}, ink, 1);
    }
  }
}

enum Page { P_BOARD, P_DESK, P_TERM, NPAGES };
int page = P_BOARD;

void drawBoard(const Status& s, const PcHist& h, uint32_t now) {
  bool nums = !isnan(s.pcTemp) && !isnan(s.pcLoad) && s.pcMem >= 0;
  bool linked = s.api && strcmp(s.link, "LINKED") == 0 && nums;
  bool nohost = s.api && strcmp(s.link, "NO HOST") == 0;
  // Cable still in, helper quiet: hold the last PC figures. Green stays off; the
  // reading is not live. Unplugging the cable skips this and shows NO HOST at once.
  bool reconnecting = s.api && !linked && !nohost && h.had
                      && (uint32_t)(now - h.lastMs) < RECONNECT_MS;
  bool showPc = linked || reconnecting;
  uint16_t top, bot, ink, accent;
  const char* word;
  if (!s.api) {
    top = rgb(50, 52, 60); bot = rgb(20, 22, 28); ink = rgb(180, 184, 196); accent = C_DIM; word = "ZERO";
  } else if (linked) {
    top = rgb(94, 234, 144); bot = rgb(18, 122, 66); ink = rgb(4, 34, 8); accent = rgb(60, 220, 120); word = "LINKED";
  } else if (nohost) {
    top = rgb(255, 210, 122); bot = rgb(138, 90, 16); ink = rgb(42, 24, 4); accent = rgb(255, 210, 122); word = "NO HOST";
  } else {
    top = rgb(126, 182, 238); bot = rgb(26, 78, 134); ink = rgb(4, 16, 24); accent = rgb(159, 212, 255); word = "KEYS";
  }
  // 52px rail so a figure, its label + badges, and an 82x22 dual-layer readout fit on one row.
  const int RAIL = 52, NUM_R = 114, LAB_X = 118, SP_X = 152, SP_W = 82;
  const int ROW_Y[3] = {11, 42, 73};
  vgrad(0, 0, RAIL, H, top, bot);
  vword(0, 0, RAIL, s.host ? H - 12 : H, word, ink);
  drawRailLeds(RAIL, s, ink);
  fb.fillRect(RAIL, 0, W - RAIL, H, C_BG);
  uint16_t tagC = reconnecting ? rgb(255, 186, 80) : accent;
  txt(RAIL + 4, 1, !s.api ? "" : showPc ? "PC" : "PI", tagC, 1);

  if (showPc) {
    const char* pIp = linked ? s.pcIp : h.lastPcIp;
    int pDisk = linked ? s.pcDisk : h.lastDisk;
    const char* pProc = linked ? s.pcTopProc : h.lastTopProc;
    float pCpu = linked ? s.pcTopCpu : h.lastTopCpu;
    char hdr[29] = "";
    size_t pos = 0;
    auto app = [&](const char* fmt, ...) {
      if (pos >= sizeof(hdr) - 1) return;
      va_list ap; va_start(ap, fmt);
      int w = vsnprintf(hdr + pos, sizeof(hdr) - pos, fmt, ap);
      va_end(ap);
      if (w > 0) pos = min(sizeof(hdr) - 1, pos + (size_t)w);
    };
    if (pIp && pIp[0]) app("%s ", pIp);
    if (pDisk >= 0) app("/:%d%% ", pDisk);
    if (pProc && pProc[0]) {
      if (!isnan(pCpu) && pCpu >= 1.f) app("%.7s:%.0f%%", pProc, pCpu);
      else app("%.10s", pProc);
    }
    if (hdr[0]) {
      uint16_t hCol = (pDisk >= 90) ? C_WARN : rgb(140, 156, 180);
      txt(RAIL + 20, 1, hdr, reconnecting ? mix(C_BG, hCol, 130) : hCol, 1);
    }
  }

  float ft, fl; int fm;
  uint16_t numC = C_TXT;
  if (showPc && linked) { ft = s.pcTemp; fl = s.pcLoad; fm = s.pcMem; }
  else if (showPc) { ft = h.lastT; fl = h.lastL; fm = isnan(h.lastM) ? -1 : (int)(h.lastM + 0.5f); numC = rgb(176, 186, 200); }
  else { ft = s.temp; fl = s.load1; fm = s.mem; }
  char fig[3][16];
  fmtTemp(fig[0], sizeof fig[0], ft); fmtLoad(fig[1], sizeof fig[1], fl); fmtMem(fig[2], sizeof fig[2], fm);
  const char* lab[3] = {"temp", "load", "mem"};
  float ncpu = showPc ? (linked ? s.pcNcpu : h.lastNcpu) : 1.f;
  if (!(ncpu >= 1.f)) ncpu = showPc ? 4.f : 1.f;
  float level[3] = {(ft - 35.f) / 60.f, fl / ncpu, fm < 0 ? NAN : (float)fm / 100.f};
  float warnAt[3] = {(80.f - 35.f) / 60.f, 0.75f, 0.85f};
  float pkT = showPc ? (recentPeak(h.temp, h.n, h.next, 30) - 35.f) / 60.f : NAN;
  float pkL = showPc ? recentPeak(h.load, h.n, h.next, 30) / ncpu : NAN;
  float pkM = showPc ? recentPeak(h.mem, h.n, h.next, 30) / 100.f : NAN;
  float peaks[3] = {pkT, pkL, pkM};
  uint16_t cLow[3]  = {rgb(255, 136, 40), rgb(48, 176, 255), rgb(150, 110, 255)};
  uint16_t cWarm[3] = {rgb(255, 196, 48), rgb(255, 186, 48), rgb(245, 160, 70)};
  uint16_t cHot[3]  = {rgb(255, 56, 56),  rgb(255, 60, 60),  rgb(255, 60, 60)};

  for (int i = 0; i < 3; i++) {
    txtR(NUM_R, ROW_Y[i] + 2, fig[i], numC, 2);
    txt(LAB_X, ROW_Y[i], lab[i], C_DIM, 1);
    drawPlasmaCapsule(SP_X, ROW_Y[i], SP_W, 7, level[i], peaks[i], warnAt[i],
                      cLow[i], cWarm[i], cHot[i], reconnecting);
  }
  if (showPc) {
    drawDeltaBadge(LAB_X, ROW_Y[0] + 9, h.temp, h.n, h.next, 0, reconnecting);
    drawDeltaBadge(LAB_X, ROW_Y[1] + 9, h.load, h.n, h.next, 1, reconnecting);
    drawDeltaBadge(LAB_X, ROW_Y[2] + 9, h.mem,  h.n, h.next, 2, reconnecting);
    const uint8_t* cores = linked ? s.pcCores : h.lastCores;
    int ccnt = linked ? s.pcCoreCount : h.lastCoreCount;
    drawCoreChiclets(LAB_X, ROW_Y[1] + 18, cores, ccnt, reconnecting);

    float lo = 0.f, hi = 0.f;
    if (!seriesRange(h.temp, h.n, h.next, 8.f, lo, hi)) { lo = 0.f; hi = 0.f; }
    drawHorizonSpark(SP_X, ROW_Y[0] + 9, SP_W, 13, h.temp, h.n, h.next, lo, hi, 80.f, rgb(255, 170, 80), reconnecting);
    if (!seriesRange(h.load, h.n, h.next, 0.5f, lo, hi)) { lo = 0.f; hi = 0.f; }
    drawHorizonSpark(SP_X, ROW_Y[1] + 9, SP_W, 13, h.load, h.n, h.next, lo, hi, 0.75f * ncpu, rgb(110, 190, 255), reconnecting);
    drawHorizonSpark(SP_X, ROW_Y[2] + 9, SP_W, 13, h.mem, h.n, h.next, 0.f, 100.f, 85.f, rgb(186, 160, 255), reconnecting);
  }

  if (reconnecting) {
    int fx = RAIL + 4, fy = 106, fw = W - fx - 4, fh = 26;
    fb.fillRoundRect(fx, fy, fw, fh, 4, rgb(72, 44, 8));
    const char* msg = "reconnecting";
    uint16_t toastC = ((now / 400) & 1) ? rgb(255, 196, 96) : rgb(168, 120, 48);
    txt(fx + (fw - (int)strlen(msg) * 12) / 2, fy + 5, msg, toastC, 2);
    return;
  }
  char l1[40], l2[40];
  if (!s.api) snprintf(l1, sizeof l1, "%s", s.wifi ? "no zero" : "wifi down");
  else if (linked) {
    char tc[8], mem[8];
    fmtTemp(tc, sizeof tc, s.temp); fmtMem(mem, sizeof mem, s.mem);
    snprintf(l1, sizeof l1, "pi %s %s %s", tc, mem, s.ip);
  } else if (nohost) snprintf(l1, sizeof l1, "zero %s", s.ip);
  else snprintf(l1, sizeof l1, "searching");
  txt(RAIL + 6, 107, l1, rgb(197, 208, 200), 1);

  if (linked) {
    char dn[8], up[8];
    fmtRate(dn, sizeof dn, s.pcRx); fmtRate(up, sizeof up, s.pcTx);
    int x = RAIL + 6;
    txt(x, 121, "dn", rgb(60, 220, 140), 1);
    txtR(x + 42, 121, dn, C_TXT, 1);
    txt(x + 46, 121, "up", rgb(255, 176, 64), 1);
    txtR(x + 88, 121, up, C_TXT, 1);
    drawDuplexWaterfall(SP_X, 118, SP_W, 15, h.rx, h.tx, h.n, h.next, false);
    return;
  }
  if (!s.wifi) snprintf(l2, sizeof l2, "geek --");
  else snprintf(l2, sizeof l2, "geek %s %ddBm", s.geekIp, s.rssi);
  txt(RAIL + 6, 121, l2, rgb(197, 208, 200), 1);
}

void drawWaiting() {
  vgrad(0, 0, W, H, rgb(126, 182, 238), rgb(18, 60, 110));
  vword((W - 72) / 2, 0, 72, H, "KEYS", rgb(4, 16, 24));
  txt(99, 120, "waiting", rgb(4, 20, 32), 1);
}

void drawTerm() {
  Term t; xSemaphoreTake(gLock, portMAX_DELAY); t = gTerm; xSemaphoreGive(gLock);
  fb.fillScreen(0x0000);
  uint16_t fg = rgb(170, 255, 170);
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
  Status s; PcHist h;
  xSemaphoreTake(gLock, portMAX_DELAY); s = gStat; h = gHist; xSemaphoreGive(gLock);
  fb.fillScreen(rgb(12, 16, 24));
  if (page == P_TERM) drawTerm();
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
  pinMode(LCD_BL, OUTPUT); digitalWrite(LCD_BL, HIGH);
  SPI.begin(LCD_SCLK, -1, LCD_MOSI, LCD_CS);
  tft.init(135, 240); tft.setSPISpeed(40000000); tft.setRotation(1);
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
