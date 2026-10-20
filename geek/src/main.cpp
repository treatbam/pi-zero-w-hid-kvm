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
  int httpCode = 0, rssi = 0;
  char udc[16] = "?", ip[16] = "--", geekIp[16] = "--", link[12] = "";
  float temp = NAN, load1 = NAN, pcTemp = NAN, pcLoad = NAN;
  int mem = -1, pcMem = -1;
  long upS = -1;
  uint32_t latencyMs = 0, stampMs = 0, polls = 0, fails = 0;
};
Status gStat;
// One sample per /api/status poll (2 s). Gaps are NAN so a quiet helper breaks the line
// instead of drawing across the outage. last* is the last live PC reading.
static const int HIST = 60;
static const uint32_t RECONNECT_MS = 60000;
struct PcHist {
  float temp[HIST], load[HIST], mem[HIST];
  int n, next;
  uint32_t lastMs;
  bool had;
  float lastT, lastL, lastM;
};
PcHist gHist;
static void pushHist(float t, float l, float m, bool live) {
  gHist.temp[gHist.next] = t;
  gHist.load[gHist.next] = l;
  gHist.mem[gHist.next] = m;
  gHist.next = (gHist.next + 1) % HIST;
  if (gHist.n < HIST) gHist.n++;
  if (live) {
    gHist.lastMs = millis();
    gHist.had = true;
    gHist.lastT = t; gHist.lastL = l; gHist.lastM = m;
  }
}
SemaphoreHandle_t gLock;
volatile bool gTermOn = false;
volatile bool gDeskOn = false;
volatile bool gFramePull = false;

static const int TCOLS = 39, TROWS = 16;
struct Term { char row[TROWS][TCOLS + 1]; int n = 0, code = 0; bool open = false; uint32_t seq = 0, stampMs = 0; };
Term gTerm;

static void copyStr(char* dst, size_t n, const char* src) {
  if (!src) src = "";
  strlcpy(dst, src, n);
}

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
      }
    }
    http.end();
  }
  xSemaphoreTake(gLock, portMAX_DELAY);
  s.polls = gStat.polls + 1; s.fails = gStat.fails + (s.api ? 0 : 1); s.stampMs = millis();
  gStat = s;
  bool live = s.api && strcmp(s.link, "LINKED") == 0
              && !isnan(s.pcTemp) && !isnan(s.pcLoad) && s.pcMem >= 0;
  if (live) pushHist(s.pcTemp, s.pcLoad, (float)s.pcMem, true);
  else pushHist(NAN, NAN, NAN, false);
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
  int ar = a >> 11, ag = (a >> 5) & 63, ab = a & 31, br = b >> 11, bg = (b >> 5) & 63, bb = b & 31;
  return ((ar + (br - ar) * t / 255) << 11) | ((ag + (bg - ag) * t / 255) << 5) | (ab + (bb - ab) * t / 255);
}
const uint16_t C_TXT = 0xFFFF, C_DIM = rgb(130, 140, 165),
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

// Oldest sample at the left. NAN breaks the stroke. lo/hi are the value scale.
void spark(int x, int y, int w, int h, const float* series, int n, int next, float lo, float hi, uint16_t col) {
  if (n <= 0 || !(hi > lo)) return;
  int start = (n < HIST) ? 0 : next;
  int prevX = -1, prevY = 0;
  for (int i = 0; i < n; i++) {
    float v = series[(start + i) % HIST];
    if (isnan(v)) { prevX = -1; continue; }
    float t = (v - lo) / (hi - lo);
    if (t < 0) t = 0; if (t > 1) t = 1;
    int px = x + (n == 1 ? w - 1 : i * (w - 1) / (n - 1));
    int py = y + h - 1 - (int)(t * (h - 1));
    if (prevX >= 0) fb.drawLine(prevX, prevY, px, py, col);
    else fb.drawPixel(px, py, col);
    prevX = px; prevY = py;
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
  // 52px rail (not a third of 240) so a figure, its label, and a trace fit on one row.
  const int RAIL = 52, NUM_R = 116, LAB_X = 122, SP_X = 152, SP_W = 82, SP_H = 16;
  const int ROW_Y[3] = {12, 44, 76};
  vgrad(0, 0, RAIL, H, top, bot);
  vword(0, 0, RAIL, H, word, ink);
  fb.fillRect(RAIL, 0, W - RAIL, H, rgb(12, 16, 24));
  uint16_t tagC = reconnecting ? rgb(255, 186, 80) : accent;
  txt(RAIL + 6, 1, !s.api ? "" : showPc ? "PC" : "PI", tagC, 1);

  float ft, fl; int fm;
  uint16_t numC = C_TXT;
  if (showPc && linked) { ft = s.pcTemp; fl = s.pcLoad; fm = s.pcMem; }
  else if (showPc) { ft = h.lastT; fl = h.lastL; fm = isnan(h.lastM) ? -1 : (int)(h.lastM + 0.5f); numC = rgb(176, 186, 200); }
  else { ft = s.temp; fl = s.load1; fm = s.mem; }
  char fig[3][16];
  fmtTemp(fig[0], sizeof fig[0], ft); fmtLoad(fig[1], sizeof fig[1], fl); fmtMem(fig[2], sizeof fig[2], fm);
  const char* lab[3] = {"temp", "load", "mem"};
  for (int i = 0; i < 3; i++) {
    txtR(NUM_R, ROW_Y[i], fig[i], numC, 2);
    txt(LAB_X, ROW_Y[i] + 4, lab[i], C_DIM, 1);
  }
  if (showPc) {
    float lo, hi;
    uint16_t cT = reconnecting ? rgb(150, 116, 70) : rgb(255, 170, 80);
    uint16_t cL = reconnecting ? rgb(90, 116, 150) : rgb(110, 190, 255);
    uint16_t cM = reconnecting ? rgb(120, 108, 150) : rgb(186, 160, 255);
    if (seriesRange(h.temp, h.n, h.next, 8, lo, hi))
      spark(SP_X, ROW_Y[0], SP_W, SP_H, h.temp, h.n, h.next, lo, hi, cT);
    if (seriesRange(h.load, h.n, h.next, 0.5f, lo, hi))
      spark(SP_X, ROW_Y[1], SP_W, SP_H, h.load, h.n, h.next, lo, hi, cL);
    spark(SP_X, ROW_Y[2], SP_W, SP_H, h.mem, h.n, h.next, 0, 100, cM);
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
  if (!s.wifi) snprintf(l2, sizeof l2, "geek --");
  else snprintf(l2, sizeof l2, "geek %s %ddBm", s.geekIp, s.rssi);
  txt(RAIL + 6, 110, l1, rgb(197, 208, 200), 1);
  txt(RAIL + 6, 122, l2, rgb(197, 208, 200), 1);
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
