// ESP32-S3-GEEK KVM panel: polls the Zero W's /api/status and shows paged, shaded stats
// with a button-driven menu. Networking runs in its own FreeRTOS task (core 0) so the
// UI loop (buttons + drawing) never blocks on HTTP. Frames are rendered into an
// off-screen canvas and pushed in one go, so there's no flicker.
//
// Buttons (all active-low, INPUT_PULLUP, wired to GND):
//   UP / DOWN : short = move;            long = jump to first / last
//   SELECT    : short = open / choose;   double = refresh now;  long = send highlighted macro
//   BOOT(back): short = back;            long = backlight on/off
// Default is one-button mode (BOOT only); three-button mode when UP/DOWN are set. See below.
// With BTN_UP/BTN_DOWN set to -1 the panel still works: SELECT short then cycles pages.
#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include "secrets.h"

// LCD pins: confirmed working on hardware (2026-10-13).
#define LCD_MOSI 11
#define LCD_SCLK 12
#define LCD_CS   10
#define LCD_DC    8
#define LCD_RST   9
#define LCD_BL    7

// Buttons. Override from platformio.ini build_flags (-DBTN_DOWN=xx) once pins are locked.
#ifndef BTN_BACK
#define BTN_BACK    0    // on-board BOOT button
#endif
#ifndef BTN_UP
#define BTN_UP     -1    // set to 6 (3-pin header GPIO6) once the button is wired
#endif
#ifndef BTN_DOWN
#define BTN_DOWN   -1    // second header pin, pending silkscreen check
#endif
#ifndef BTN_SELECT
#define BTN_SELECT -1    // -1 = three-button mode (see below)
#endif
// Three-button mode (BTN_SELECT == -1): BOOT short = select, double = refresh, long = back;
// UP long = backlight toggle. Four-button mode uses the mapping in the header comment.
// One-button mode (UP and DOWN both unset, the default): everything on BOOT.
//   short = next page / next item;  double = open menu / choose item;
//   in menu, choosing a macro arms it for 4 s: long press sends, short cancels;
//   very long (3 s) = backlight. Long on a page = refresh. Menu has a "Back" item.
#define ONE_BTN   (BTN_UP < 0 && BTN_DOWN < 0)
#define THREE_BTN (!ONE_BTN && BTN_SELECT < 0)

static const int W = 240, H = 135;
Adafruit_ST7789 tft(&SPI, LCD_CS, LCD_DC, LCD_RST);
GFXcanvas16 fb(W, H);
const String BASE = String("http://") + KVM_HOST + ":" + KVM_PORT;

// ---------- shared state (network task writes, UI reads) ----------
struct Status {
  bool wifi = false, api = false, hid = false, host = false;
  int httpCode = 0, rssi = 0;
  char udc[16] = "?", ip[16] = "?";
  float temp = NAN, load1 = NAN; int mem = -1; long upS = -1;
  uint32_t latencyMs = 0, stampMs = 0, polls = 0, fails = 0;
};
Status gStat;
SemaphoreHandle_t gLock;
QueueHandle_t gMacroQ;               // const char* macro names
volatile bool gRefresh = false;
volatile int  gLastMacroCode = 0;
volatile bool gMacroDone = false;   // set by net task when a macro POST finishes
bool gMacroBusy = false;            // UI-side: a macro is queued or in flight

static const char* MACROS[]      = {"ctrl_alt_del", "win_l", "alt_tab"};
static const char* MACRO_LABEL[] = {"Ctrl+Alt+Del", "Win+L (lock)", "Alt+Tab"};
static const int   NMACRO = 3;
static const int   NMENU = NMACRO + 1;   // last item = Back

void pollOnce() {
  Status s; s.wifi = WiFi.status() == WL_CONNECTED; s.rssi = s.wifi ? WiFi.RSSI() : 0;
  if (s.wifi) {
    HTTPClient http; http.setTimeout(1500); http.setConnectTimeout(1500);
    uint32_t t0 = millis(); http.begin(BASE + "/api/status");
    s.httpCode = http.GET(); s.latencyMs = millis() - t0;
    if (s.httpCode == 200) {
      JsonDocument d;
      if (!deserializeJson(d, http.getString())) {
        s.api = true; s.hid = d["hid_ready"] | false; s.host = d["host_connected"] | false;
        strlcpy(s.udc, d["udc_state"] | "?", sizeof s.udc); strlcpy(s.ip, d["ip"] | "?", sizeof s.ip);
        if (d["temp_c"].is<float>()) s.temp = d["temp_c"];
        if (d["load1"].is<float>())  s.load1 = d["load1"];
        s.mem = d["mem_pct"] | -1; s.upS = d["uptime_s"] | -1L;
      }
    }
    http.end();
  }
  xSemaphoreTake(gLock, portMAX_DELAY);
  s.polls = gStat.polls + 1; s.fails = gStat.fails + (s.api ? 0 : 1); s.stampMs = millis();
  gStat = s;
  xSemaphoreGive(gLock);
}

void netTask(void*) {
  uint32_t last = 0;
  for (;;) {
    const char* m;
    if (xQueueReceive(gMacroQ, &m, pdMS_TO_TICKS(50)) == pdTRUE) {
      int code = 0;                                   // 0 = not sent (Wi-Fi down)
      if (WiFi.status() == WL_CONNECTED) {
        HTTPClient http; http.setTimeout(1500); http.setConnectTimeout(1500); http.begin(BASE + "/api/macro");
        http.addHeader("Content-Type", "application/json");
        code = http.POST(String("{\"name\":\"") + m + "\"}"); http.end();
      }
      Serial.printf("macro %s -> %d\n", m, code);
      gLastMacroCode = code; gMacroDone = true;
    }
    if (gRefresh || millis() - last > 2000) { gRefresh = false; last = millis(); pollOnce(); }
  }
}

// ---------- buttons: debounced short / double / long ----------
enum Ev { EV_NONE, EV_SHORT, EV_DOUBLE, EV_LONG, EV_VLONG };
struct Btn {
  // vlong: long fires on release (0.6-3 s) and EV_VLONG fires while held at 3 s.
  Btn(int p, bool d, bool v = false) : pin(p), dbl(d), vlong(v) {}
  int pin; bool dbl, vlong; bool down = false, raw = false, longFired = false, pendingShort = false;
  uint32_t edgeMs = 0, pressMs = 0, releaseMs = 0;
  static const uint32_t DEB = 25, LONG = 600, VLONG = 3000, DBL = 280;
  Ev poll(uint32_t now) {
    if (pin < 0) return EV_NONE;
    bool r = digitalRead(pin) == LOW;
    if (r != raw) { raw = r; edgeMs = now; }
    if (now - edgeMs >= DEB && r != down) {           // stable edge
      down = r;
      if (down) { pressMs = now; longFired = false; }
      else if (!longFired) {
        if (vlong && now - pressMs >= LONG) { pendingShort = false; return EV_LONG; }
        if (!dbl) return EV_SHORT;                     // no double: fire immediately
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
Btn bBack{BTN_BACK, THREE_BTN || ONE_BTN, ONE_BTN}, bUp{BTN_UP, false}, bDown{BTN_DOWN, false}, bSel{BTN_SELECT, true};

// ---------- drawing helpers (shading = vertical gradients + bevels) ----------
uint16_t rgb(uint8_t r, uint8_t g, uint8_t b) { return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3); }
uint16_t mix(uint16_t a, uint16_t b, int t /*0..255*/) {
  int ar = a >> 11, ag = (a >> 5) & 63, ab = a & 31, br = b >> 11, bg = (b >> 5) & 63, bb = b & 31;
  return ((ar + (br - ar) * t / 255) << 11) | ((ag + (bg - ag) * t / 255) << 5) | (ab + (bb - ab) * t / 255);
}
const uint16_t C_BG0 = rgb(8, 10, 20), C_BG1 = rgb(20, 26, 44), C_PANEL = rgb(30, 38, 60),
               C_HI = rgb(70, 90, 130), C_LO = rgb(5, 6, 12), C_TXT = 0xFFFF, C_DIM = rgb(130, 140, 165),
               C_OK = rgb(60, 220, 120), C_WARN = rgb(255, 170, 40), C_BAD = rgb(255, 70, 70), C_ACC = rgb(80, 180, 255);

void vgrad(int x, int y, int w, int h, uint16_t top, uint16_t bot) {
  for (int i = 0; i < h; i++) fb.drawFastHLine(x, y + i, w, mix(top, bot, h > 1 ? i * 255 / (h - 1) : 0));
}
void panel(int x, int y, int w, int h, bool pressed = false, uint16_t tint = C_PANEL) {
  fb.fillRect(x + 2, y + 2, w, h, C_LO);                                     // drop shadow
  vgrad(x, y, w, h, pressed ? mix(tint, C_LO, 120) : mix(tint, C_HI, 70), pressed ? tint : mix(tint, C_LO, 90));
  fb.drawFastHLine(x, y, w, pressed ? C_LO : C_HI); fb.drawFastVLine(x, y, h, pressed ? C_LO : C_HI);
  fb.drawFastHLine(x, y + h - 1, w, pressed ? C_HI : C_LO); fb.drawFastVLine(x + w - 1, y, h, pressed ? C_HI : C_LO);
}
void txt(int x, int y, const String& s, uint16_t c, int size = 1) { fb.setTextSize(size); fb.setTextColor(c); fb.setCursor(x, y); fb.print(s); }
void dot(int x, int y, uint16_t c) { fb.fillCircle(x, y, 3, mix(c, C_LO, 100)); fb.fillCircle(x - 1, y - 1, 2, c); }
void bar(int x, int y, int w, int h, float frac, uint16_t c) {
  fb.fillRect(x, y, w, h, C_LO); frac = constrain(frac, 0.f, 1.f);
  if (frac > 0) vgrad(x, y, max(1, (int)(w * frac)), h, mix(c, 0xFFFF, 80), mix(c, C_LO, 80));
}
String fmtUp(long s) { if (s < 0) return "--"; char b[16]; snprintf(b, sizeof b, "%ldd%02ldh%02ldm", s / 86400, s / 3600 % 24, s / 60 % 60); return b; }

// ---------- screens ----------
enum Page { P_DASH, P_SYS, P_LINK, NPAGES };
const char* PAGE_NAME[] = {"KVM", "ZERO SYS", "LINK"};
int page = P_DASH, menuSel = 0; bool inMenu = false;
uint32_t pressFxUntil = 0, toastUntil = 0, armedUntil = 0; String toast;
int armed = -1;                       // one-button mode: macro index awaiting confirm

void header(const char* title, const Status& s) {
  vgrad(0, 0, W, 16, C_HI, C_PANEL); fb.drawFastHLine(0, 16, W, C_LO);
  txt(5, 4, title, C_TXT);
  for (int i = 0; i < NPAGES && !inMenu; i++) fb.fillCircle(W / 2 - 8 + i * 8, 8, 2, i == page ? C_ACC : C_DIM);
  uint16_t lc = !s.wifi ? C_BAD : !s.api ? C_WARN : C_OK;
  int bars = !s.wifi ? 0 : s.rssi > -55 ? 4 : s.rssi > -67 ? 3 : s.rssi > -78 ? 2 : 1;
  for (int i = 0; i < 4; i++) fb.fillRect(W - 26 + i * 5, 12 - i * 3, 3, 3 + i * 3, i < bars ? lc : C_LO);
}

void drawDash(int ox, const Status& s) {
  panel(ox + 4, 22, 112, 50, millis() < pressFxUntil);
  dot(ox + 14, 34, s.hid ? C_OK : C_BAD);  txt(ox + 22, 30, s.hid ? "HID ready" : "HID DOWN", C_TXT);
  dot(ox + 14, 50, s.host ? C_OK : C_WARN); txt(ox + 22, 46, s.host ? "Host linked" : "No host", C_TXT);
  txt(ox + 22, 60, String("udc ") + s.udc, C_DIM);
  panel(ox + 124, 22, 112, 50);
  txt(ox + 130, 28, "temp", C_DIM); txt(ox + 130, 40, isnan(s.temp) ? "--" : String(s.temp, 1) + "C", s.temp > 70 ? C_BAD : C_TXT, 2);
  txt(ox + 190, 28, "load", C_DIM); txt(ox + 190, 40, isnan(s.load1) ? "--" : String(s.load1, 2), C_TXT);
  txt(ox + 190, 52, "mem", C_DIM);  txt(ox + 214, 52, s.mem < 0 ? "--" : String(s.mem) + "%", C_TXT);
  bar(ox + 130, 62, 100, 5, s.mem / 100.f, s.mem > 85 ? C_BAD : C_ACC);
  panel(ox + 4, 78, 232, 52);
  txt(ox + 10, 84, String("zero ") + s.ip, C_TXT);
  txt(ox + 10, 96, String("up ") + fmtUp(s.upS), C_DIM);
  txt(ox + 10, 108, String("rtt ") + s.latencyMs + "ms  rssi " + s.rssi, C_DIM);
  uint32_t age = (millis() - s.stampMs) / 1000;
  txt(ox + 10, 120, String("polled ") + age + "s ago  fails " + s.fails + "/" + s.polls, age > 6 ? C_WARN : C_DIM);
}

void drawSys(int ox, const Status& s) {
  const char* lbl[] = {"CPU temp", "Load 1m", "Memory"};
  float frac[] = {isnan(s.temp) ? 0 : s.temp / 85.f, isnan(s.load1) ? 0 : s.load1 / 2.f, s.mem / 100.f};
  String val[] = {isnan(s.temp) ? "--" : String(s.temp, 1) + " C", isnan(s.load1) ? "--" : String(s.load1, 2), s.mem < 0 ? "--" : String(s.mem) + " %"};
  for (int i = 0; i < 3; i++) {
    int y = 22 + i * 30; panel(ox + 4, y, 232, 26);
    txt(ox + 10, y + 4, lbl[i], C_DIM); txt(ox + 170, y + 4, val[i], C_TXT);
    bar(ox + 10, y + 15, 220, 6, frac[i], frac[i] > .85f ? C_BAD : frac[i] > .6f ? C_WARN : C_OK);
  }
  txt(ox + 8, 116, String("uptime ") + fmtUp(s.upS), C_DIM);
}

void drawLink(int ox, const Status& s) {
  panel(ox + 4, 22, 232, 108);
  int y = 28;
  auto row = [&](const String& k, const String& v, uint16_t c) { txt(ox + 10, y, k, C_DIM); txt(ox + 90, y, v, c); y += 13; };
  row("wifi", s.wifi ? String(KVM_WIFI_SSID) : "down", s.wifi ? C_OK : C_BAD);
  row("rssi", s.wifi ? String(s.rssi) + " dBm" : "--", C_TXT);
  row("geek ip", s.wifi ? WiFi.localIP().toString() : "--", C_TXT);
  row("zero", String(KVM_HOST) + ":" + KVM_PORT, C_TXT);
  row("http", String(s.httpCode), s.httpCode == 200 ? C_OK : C_BAD);
  row("rtt", String(s.latencyMs) + " ms", C_TXT);
  row("heap", String(ESP.getFreeHeap() / 1024) + " KB", C_TXT);
  row("geek up", fmtUp(millis() / 1000), C_TXT);
}

void drawMenu(int ox) {
  bool arm = armed >= 0 && millis() < armedUntil;
  const char* hint = ONE_BTN ? (arm ? "HOLD to send, tap to cancel" : "tap next, x2 choose")
                   : THREE_BTN ? "BOOT x2 send, hold back" : "Hold SELECT to send";
  txt(ox + 8, 20, hint, arm ? C_WARN : C_DIM);
  // 4 rows (3 macros + Back) at 26 px; window scrolls if needed
  for (int i = 0; i < NMENU; i++) {
    int y = 31 + i * 26; bool sel = i == menuSel; bool isArmed = arm && i == armed;
    uint16_t tint = isArmed ? rgb(120, 60, 20) : sel ? rgb(40, 70, 120) : C_PANEL;
    panel(ox + 4 + (sel ? 0 : 6), y, 232 - (sel ? 0 : 12), 22, sel && millis() < pressFxUntil, tint);
    if (sel) fb.fillTriangle(ox + 10, y + 6, ox + 10, y + 16, ox + 16, y + 11, isArmed ? C_WARN : C_ACC);
    txt(ox + 22, y + 4, i < NMACRO ? MACRO_LABEL[i] : "< Back", sel ? C_TXT : C_DIM, 2);
  }
  if (arm) { int w = (int)(232L * (armedUntil - millis()) / 4000); fb.fillRect(ox + 4, 132, w, 2, C_WARN); }
}

void render(int ox, int pg, bool menu, const Status& s) {
  if (menu) drawMenu(ox);
  else if (pg == P_DASH) drawDash(ox, s); else if (pg == P_SYS) drawSys(ox, s); else drawLink(ox, s);
}

// Slide animation state: ~120 ms ease-out, rendered frame by frame without blocking input.
int animFromPage = 0; bool animFromMenu = false; int animDir = 0; uint32_t animStart = 0;
const uint32_t ANIM_MS = 120;

void frame() {
  Status s; xSemaphoreTake(gLock, portMAX_DELAY); s = gStat; xSemaphoreGive(gLock);
  vgrad(0, 0, W, H, C_BG1, C_BG0);
  uint32_t t = millis() - animStart;
  if (animDir && t < ANIM_MS) {
    float p = 1 - powf(1 - (float)t / ANIM_MS, 3);           // ease-out cubic
    int off = (int)(W * p) * animDir;
    render(-off, animFromPage, animFromMenu, s);
    render(W * animDir - off, page, inMenu, s);
  } else { animDir = 0; render(0, page, inMenu, s); }
  header(inMenu ? "MACROS" : PAGE_NAME[page], s);
  if (millis() < toastUntil) {
    int tw = toast.length() * 6 + 12; panel((W - tw) / 2, H - 22, tw, 18, false, rgb(30, 80, 50));
    txt((W - tw) / 2 + 6, H - 17, toast, C_TXT);
  }
  tft.drawRGBBitmap(0, 0, fb.getBuffer(), W, H);
}

void go(int newPage, bool newMenu, int dir) {
  animFromPage = page; animFromMenu = inMenu; animDir = dir; animStart = millis();
  page = newPage; inMenu = newMenu;
}
void flash(const String& m) { toast = m; toastUntil = millis() + 1200; }

void toggleBacklight() { static bool bl = true; bl = !bl; digitalWrite(LCD_BL, bl); }
void doBack() { armed = -1; if (inMenu) go(page, false, -1); }
void sendMacro(int i) {
  armed = -1;
  if (gMacroBusy) { flash("still sending..."); return; }   // one macro in flight at a time
  const char* m = MACROS[i];
  if (xQueueSend(gMacroQ, &m, 0) == pdTRUE) gMacroBusy = true, flash(String("sending ") + MACRO_LABEL[i] + "...");
  else flash("busy, try again");
}
void doSelect(Ev e) {
  if (e == EV_DOUBLE) { gRefresh = true; flash("refreshing"); }
  else if (e == EV_LONG && inMenu) { if (menuSel == NMACRO) doBack(); else sendMacro(menuSel); }
  else if (e == EV_SHORT) { if (!inMenu) go(page, true, 1); else if (menuSel == NMACRO) doBack(); }
}

void handleOne(Ev e) {
  bool arm = armed >= 0 && millis() < armedUntil;
  if (e == EV_VLONG) { armed = -1; toggleBacklight(); return; }
  if (!inMenu) {
    if (e == EV_SHORT) go((page + 1) % NPAGES, false, 1);
    else if (e == EV_DOUBLE) { menuSel = 0; armed = -1; go(page, true, 1); }
    else if (e == EV_LONG) { gRefresh = true; flash("refreshing"); }
    return;
  }
  if (arm) {                                   // confirm step
    if (e == EV_LONG) sendMacro(armed);
    else { armed = -1; flash("cancelled"); }
    return;
  }
  armed = -1;
  if (e == EV_SHORT) menuSel = (menuSel + 1) % NMENU;
  else if (e == EV_DOUBLE) {
    if (menuSel == NMACRO) doBack();
    else { armed = menuSel; armedUntil = millis() + 4000; }
  }
  else if (e == EV_LONG) doBack();             // long without arming = back
}

void handle(Btn& b, Ev e) {
  if (e == EV_NONE) return;
  pressFxUntil = millis() + 90;
  if (ONE_BTN) { if (&b == &bBack) handleOne(e); return; }
  if (&b == &bBack) {
    if (THREE_BTN) {
      // Outside menu: short = open macros, double = refresh. In menu: double = send
      // (deliberate, since long is back), short = hint only, long = back.
      if (e == EV_LONG) doBack();
      else if (!inMenu) doSelect(e);
      else if (e == EV_DOUBLE) doSelect(EV_LONG);
      else if (menuSel == NMACRO) doBack();
      else flash("double-press BOOT to send");
    } else if (e == EV_LONG) toggleBacklight();
    else doBack();
  } else if (&b == &bUp || &b == &bDown) {
    int d = &b == &bUp ? -1 : 1;
    if (THREE_BTN && &b == &bUp && e == EV_LONG) { toggleBacklight(); return; }
    if (inMenu) menuSel = e == EV_LONG ? (d < 0 ? 0 : NMENU - 1) : (menuSel + d + NMENU) % NMENU;
    else go(e == EV_LONG ? (d < 0 ? 0 : NPAGES - 1) : (page + d + NPAGES) % NPAGES, false, d);
  } else if (&b == &bSel) doSelect(e);
}

void setup() {
  Serial.begin(115200);
  for (int p : {BTN_BACK, BTN_UP, BTN_DOWN, BTN_SELECT}) if (p >= 0) pinMode(p, INPUT_PULLUP);
  pinMode(LCD_BL, OUTPUT); digitalWrite(LCD_BL, HIGH);
  SPI.begin(LCD_SCLK, -1, LCD_MOSI, LCD_CS);
  tft.init(135, 240); tft.setSPISpeed(40000000); tft.setRotation(1);
  if (!fb.getBuffer()) {                       // 64.8 KB frame buffer failed to allocate
    tft.fillScreen(ST77XX_RED); tft.setTextColor(ST77XX_WHITE); tft.setTextSize(2);
    tft.setCursor(6, 50); tft.print("no RAM for UI"); Serial.println("framebuffer alloc failed");
    for (;;) delay(1000);
  }
  gLock = xSemaphoreCreateMutex(); gMacroQ = xQueueCreate(1, sizeof(const char*));
  WiFi.mode(WIFI_STA); WiFi.setAutoReconnect(true); WiFi.begin(KVM_WIFI_SSID, KVM_WIFI_PASS);
  xTaskCreatePinnedToCore(netTask, "net", 8192, nullptr, 1, nullptr, 0);   // UI loop stays on core 1
}

void loop() {
  uint32_t now = millis();
  if (armed >= 0 && now >= armedUntil) { armed = -1; flash("cancelled"); }
  if (gMacroDone) {
    gMacroDone = false; gMacroBusy = false; int c = gLastMacroCode;
    flash(c == 200 ? "sent OK" : c == 0 ? "not sent: Wi-Fi down" : c == 409 ? "not sent: no host"
          : c == 503 ? "not sent: HID busy" : c < 0 ? "not sent: Zero unreachable" : String("not sent: HTTP ") + c);
  }
  handle(bBack, bBack.poll(now)); handle(bUp, bUp.poll(now));
  handle(bDown, bDown.poll(now)); handle(bSel, bSel.poll(now));
  static uint32_t lastFrame = 0;
  uint32_t period = (animDir || now < pressFxUntil) ? 16 : armed >= 0 ? 50 : 250;          // ~60 fps while animating
  if (now - lastFrame >= period) { lastFrame = now; frame(); }
  delay(2);
}
