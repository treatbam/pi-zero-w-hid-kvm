// ESP32-S3-GEEK KVM panel. Three pages, one short press to advance:
//   scoreboard → desktop still → terminal → scoreboard.
// A long press and a double press do nothing. There is no menu.
// The status word (LINKED / KEYS / NO HOST) comes from the Zero's /api/status.
// The desktop still is GET /api/frame, polled only while that page is showing.
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
    p.putString("ssid", srv.arg("ssid")); p.putString("host", srv.arg("host"));
    p.putInt("port", srv.arg("port").toInt() > 0 ? srv.arg("port").toInt() : 8080);
    if (srv.arg("pass").length()) p.putString("pass", srv.arg("pass")); else if (!p.isKey("pass")) p.putString("pass", cfg.pass);
    if (srv.arg("pin").length())  p.putString("pin", srv.arg("pin"));   else if (!p.isKey("pin"))  p.putString("pin", cfg.pin);
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
  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http; http.setTimeout(4000); http.setConnectTimeout(1500);
    http.begin(BASE + "/api/frame"); http.addHeader("X-KVM-PIN", cfg.pin);
    int code = http.GET();
    if (code == 200) {
      WiFiClient* stream = http.getStreamPtr();
      uint8_t* dst = (uint8_t*)gFrame;
      int need = W * H * 2, got = 0;
      uint32_t t0 = millis();
      while (got < need && millis() - t0 < 4000) {
        int n = stream->read(dst + got, need - got);
        if (n > 0) got += n;
        else if (!stream->connected()) break;
        else delay(1);
      }
      ok = got == need;
    }
    http.end();
  }
  xSemaphoreTake(gLock, portMAX_DELAY);
  gFrameOk = ok;
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
Btn bBack{BTN_BACK, false}, bUp{BTN_UP, false}, bDown{BTN_DOWN, false}, bSel{BTN_SELECT, false};

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
  int n = strlen(word), ch = 16, cw = 12;
  int y0 = y + max(0, (h - n * ch) / 2);
  int x0 = x + max(0, (w - cw) / 2);
  fb.setTextSize(2); fb.setTextColor(ink);
  for (int i = 0; i < n; i++) {
    fb.setCursor(x0, y0 + i * ch);
    char s[2] = {word[i], 0};
    fb.print(s);
  }
}
void fmtTemp(char* b, size_t n, float t) { if (isnan(t)) snprintf(b, n, "--"); else snprintf(b, n, "%.0fc", t); }
void fmtLoad(char* b, size_t n, float t) { if (isnan(t)) snprintf(b, n, "--"); else snprintf(b, n, t >= 10 ? "%.1f" : "%.2f", t); }
void fmtMem(char* b, size_t n, int m) { if (m < 0) snprintf(b, n, "--"); else snprintf(b, n, "%d%%", m); }

enum Page { P_BOARD, P_DESK, P_TERM, NPAGES };
int page = P_BOARD;

void drawBoard(const Status& s) {
  bool nums = !isnan(s.pcTemp) && !isnan(s.pcLoad) && s.pcMem >= 0;
  bool linked = s.api && strcmp(s.link, "LINKED") == 0 && nums;
  bool nohost = s.api && strcmp(s.link, "NO HOST") == 0;
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
  vgrad(0, 0, 72, H, top, bot);
  vword(0, 0, 72, H, word, ink);
  fb.fillRect(72, 0, W - 72, H, rgb(12, 16, 24));
  txt(80, 4, !s.api ? "" : linked ? "PC" : "PI", accent, 1);
  float ft = linked ? s.pcTemp : s.temp;
  float fl = linked ? s.pcLoad : s.load1;
  int fm = linked ? s.pcMem : s.mem;
  char a[16], b[16], c[16], l1[40], l2[40];
  fmtTemp(a, sizeof a, ft); fmtLoad(b, sizeof b, fl); fmtMem(c, sizeof c, fm);
  txt(80, 20, a, C_TXT, 2); txt(196, 24, "temp", C_DIM, 1);
  txt(80, 46, b, C_TXT, 2); txt(196, 50, "load", C_DIM, 1);
  txt(80, 72, c, C_TXT, 2); txt(196, 76, "mem", C_DIM, 1);
  if (!s.api) snprintf(l1, sizeof l1, "%s", s.wifi ? "no zero" : "wifi down");
  else if (linked) {
    char tc[8], mem[8];
    fmtTemp(tc, sizeof tc, s.temp); fmtMem(mem, sizeof mem, s.mem);
    snprintf(l1, sizeof l1, "pi %s %s %s", tc, mem, s.ip);
  } else if (nohost) snprintf(l1, sizeof l1, "zero %s", s.ip);
  else snprintf(l1, sizeof l1, "pc waiting");
  if (!s.wifi) snprintf(l2, sizeof l2, "geek --");
  else snprintf(l2, sizeof l2, "geek %s %ddBm", s.geekIp, s.rssi);
  txt(76, 104, l1, rgb(197, 208, 200), 1);
  txt(76, 118, l2, rgb(197, 208, 200), 1);
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
  Status s; xSemaphoreTake(gLock, portMAX_DELAY); s = gStat; xSemaphoreGive(gLock);
  fb.fillScreen(rgb(12, 16, 24));
  if (page == P_TERM) drawTerm();
  else drawBoard(s);
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
    if (xSemaphoreTake(gLock, pdMS_TO_TICKS(10)) == pdTRUE) {
      gFrameOk = false; xSemaphoreGive(gLock);
    }
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
  WiFi.mode(WIFI_STA); WiFi.setAutoReconnect(true); WiFi.begin(cfg.ssid.c_str(), cfg.pass.c_str());
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
