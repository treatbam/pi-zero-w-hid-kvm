// ESP32-S3-GEEK KVM panel. Six pages (PC, cores+processes, network, ZERO|GEEK, desktop, terminal); every page uses
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
#include <ArduinoOTA.h>
#include <esp_memory_utils.h>
#include <ESPmDNS.h>
#include <Fonts/FreeSansBold9pt7b.h>
#include <Fonts/FreeSansBold12pt7b.h>
#include <Fonts/FreeSansBold18pt7b.h>
#include <Fonts/FreeSansBold24pt7b.h>
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

struct Cfg { String ssid, pass, host, pin, ota; int port; } cfg;
String BASE;
static void loadCfg() {
  Preferences p; p.begin("kvm", true);
  cfg.ssid = p.getString("ssid", KVM_WIFI_SSID); cfg.pass = p.getString("pass", KVM_WIFI_PASS);
  cfg.host = p.getString("host", KVM_HOST);      cfg.pin  = p.getString("pin", KVM_PIN);
  cfg.ota = p.getString("ota", "");   // no default: OTA stays off until set in the portal
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
    f("ota", "OTA update password (8+ chars; blank keeps saved)", "", "password");
    h += cfg.ota.length() ? F("<label><input type=checkbox name=ota_off style='width:auto'> turn OTA off (clear password)</label><br>")
                          : F("<small>OTA is off until a password is set.</small><br>");
    h += F("<small>Blank password/PIN keeps the saved value. OTA host: kvm-geek.local</small><br><br><button>Save &amp; reboot</button></form>");
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
    String ota = srv.arg("ota");
    bool otaShort = ota.length() && ota.length() < 8;
    if (srv.hasArg("ota_off")) p.remove("ota");
    else if (ota.length() >= 8) p.putString("ota", ota);
    p.end();
    srv.send(200, "text/html", otaShort ? "<h3>Saved, but the OTA password was under 8 characters and was ignored. GEEK is rebooting.</h3>"
                                        : "<h3>Saved. GEEK is rebooting.</h3>"); delay(800); ESP.restart();
  });
  srv.begin(); Serial.println("setup portal: AP KVM-GEEK-setup, http://192.168.4.1");
  for (;;) { srv.handleClient(); delay(2); }
}

struct Status {
  bool wifi = false, api = false, hid = false, host = false;
  bool ledNum = false, ledCaps = false, ledScroll = false;
  int httpCode = 0, rssi = 0;
  char udc[16] = "?", ip[16] = "--", geekIp[16] = "--", link[12] = "";
  char pcIp[16] = "", pcTopProc[16] = "", tsIp[16] = "", netIf[12] = "", netIp[16] = "";   // netIf: pc_net_iface, "" until the helper sends it   // tsIp: tailscale_ip, "" when null
  float temp = NAN, load1 = NAN, pcTemp = NAN, pcLoad = NAN;
  float pcRx = NAN, pcTx = NAN, pcNcpu = NAN, pcTopCpu = NAN;
  int mem = -1, pcMem = -1, pcDisk = -1, pcCoreCount = 0;
  double pcMemUsed = NAN, pcMemTot = NAN, pcDiskUsed = NAN, pcDiskTot = NAN;
  char pcDiskDev[16] = "";
  uint8_t pcCores[8] = {0};
  // KVM page (PIN-gated fields: the status GET sends X-KVM-PIN)
  int zRssi = 0;                 // Zero wlan0 dBm, 0 = unknown
  int8_t ts = -1;                // tailscale: -1 n/a, 0 down, 1 up
  char serialMode[8] = "";
  char mode[8] = "";             // /api/status "mode": hid | term | desktop; "" = Zero too old to say
  char helperState[12] = "";     // /api/status "helper_state": linked | streaming | probing | missing | no_host
  uint8_t uiTheme = 0;           // 0 = slate (default), 1 = braun, 2 = cobalt (synced from /api/status "ui_theme")
  // type-8 extended stats (absent -> NAN / 0)
  uint8_t coresAll[64] = {0};   // every core the helper sends (it caps at 64)
  char procN[8][16] = {}; float procP[8] = {}; int procCount = 0;   // pc_procs, busiest first
  char netpN[8][16] = {}; float netpB[8] = {}; int netpCount = 0;   // pc_net_procs (TCP bytes/s up+down), busiest first
  // cumulative per-process network history (Zero /data, since the last SD-file reset); absent -> histCount 0
  char pcId[17] = "", pcHost[24] = "";
  char histN[8][16] = {}; double histRx[8] = {}, histTx[8] = {}; int histCount = 0;
  long long histSince = 0;       // net_hist_since, Unix s, 0 = unknown
  int coresAllN = 0, coresTotal = 0;
  float pcGpu = NAN, pcCpuT = NAN, pcGpuT = NAN, pcNvmeT = NAN;
  long upS = -1, pcUpS = -1;
  uint32_t latencyMs = 0, stampMs = 0, polls = 0, fails = 0;
  uint32_t lastOkMs = 0;         // millis() of the last good /api/status, 0 = never
};
Status gStat;
// One sample per /api/status poll (2 s). Gaps are NAN so a quiet helper breaks the line
// instead of drawing across the outage. last* is the last live PC reading.
static const int HIST = 240;   // 240 x 2 s = 8 min, one sample per pixel column at full width
static const uint32_t RECONNECT_MS = 60000;
struct PcHist {
  float temp[HIST], load[HIST], mem[HIST], rx[HIST], tx[HIST];
  float cpuT[HIST], gpuT[HIST], nvT[HIST];   // type-8 sensor temps, NAN when absent
  float piLoad[HIST];                        // the Zero's own load1, NAN when the poll failed
  float geekRssi[HIST];                      // GEEK Wi-Fi dBm, NAN when Wi-Fi is down
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
  gHist.piLoad[gHist.next] = s.api ? s.load1 : NAN;
  gHist.geekRssi[gHist.next] = s.wifi && s.rssi ? (float)s.rssi : NAN;
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

static const int TCOLS = 39, TROWS = 14;   // 14 rows clear the shared status bar
struct Term { char row[TROWS][TCOLS + 1]; int n = 0, code = 0; bool open = false; uint32_t seq = 0, stampMs = 0; };
Term gTerm;

void pollOnce() {
  Status s; s.wifi = WiFi.status() == WL_CONNECTED; s.rssi = s.wifi ? WiFi.RSSI() : 0;
  static bool ntpOn = false;   // wall clock only for the net-history "since" span
  if (s.wifi && !ntpOn) { configTime(0, 0, "pool.ntp.org", "time.google.com"); ntpOn = true; }
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
        if (!d["pc_net_iface"].isNull()) copyStr(s.netIf, sizeof s.netIf, d["pc_net_iface"] | "");
        if (!d["pc_net_ip"].isNull()) copyStr(s.netIp, sizeof s.netIp, d["pc_net_ip"] | "");
        if (!d["pc_disk_pct"].isNull()) s.pcDisk = d["pc_disk_pct"].as<int>();
        if (!d["pc_mem_used"].isNull()) s.pcMemUsed = d["pc_mem_used"].as<double>();
        if (!d["pc_mem_total"].isNull()) s.pcMemTot = d["pc_mem_total"].as<double>();
        if (!d["pc_disk_used"].isNull()) s.pcDiskUsed = d["pc_disk_used"].as<double>();
        if (!d["pc_disk_total"].isNull()) s.pcDiskTot = d["pc_disk_total"].as<double>();
        if (d["pc_disk_dev"].is<const char*>()) copyStr(s.pcDiskDev, sizeof s.pcDiskDev, d["pc_disk_dev"] | "");
        if (!d["pc_top_proc"].isNull()) copyStr(s.pcTopProc, sizeof s.pcTopProc, d["pc_top_proc"] | "");
        if (!d["pc_top_cpu_pct"].isNull()) s.pcTopCpu = d["pc_top_cpu_pct"].as<float>();
        if (d["pin_ok"].is<bool>() && !(d["pin_ok"] | true)) pinFailed();
        if (!d["zero_wifi_rssi"].isNull()) s.zRssi = d["zero_wifi_rssi"].as<int>();
        if (d["tailscale_ip"].is<const char*>()) copyStr(s.tsIp, sizeof s.tsIp, d["tailscale_ip"] | "");
        if (d["tailscale"].is<const char*>()) s.ts = strcmp(d["tailscale"] | "", "up") == 0 ? 1 : 0;
        copyStr(s.serialMode, sizeof s.serialMode, d["serial_mode"] | "");
        copyStr(s.mode, sizeof s.mode, d["mode"] | "");
        copyStr(s.helperState, sizeof s.helperState, d["helper_state"] | "");
        const char* ut = d["ui_theme"] | "slate";
        s.uiTheme = strcmp(ut, "braun") == 0 ? 1 : strcmp(ut, "cobalt") == 0 ? 2 : 0;
        if (d["pc_cores_all"].is<JsonArray>()) {
          int ci = 0, tot = 0;
          for (JsonVariant v : d["pc_cores_all"].as<JsonArray>()) {
            tot++;
            if (ci >= 64) continue;
            float cv = v | 0.f;   // keep slot order: a bad entry reads 0, never shifts later cores
            s.coresAll[ci++] = (uint8_t)(constrain(cv, 0.f, 100.f) + 0.5f);
          }
          s.coresAllN = ci; s.coresTotal = tot;
        }
        if (d["pc_procs"].is<JsonArray>()) {
          int pi = 0;
          for (JsonVariant v : d["pc_procs"].as<JsonArray>()) {
            if (pi >= 8) break;
            JsonArray r = v.as<JsonArray>();
            if (r.size() < 2 || !r[0].is<const char*>()) continue;
            copyStr(s.procN[pi], sizeof s.procN[pi], r[0] | "");
            s.procP[pi++] = r[1] | 0.f;
          }
          s.procCount = pi;
        }
        if (d["pc_net_procs"].is<JsonArray>()) {
          int ni = 0;
          for (JsonVariant v : d["pc_net_procs"].as<JsonArray>()) {
            if (ni >= 8) break;
            JsonArray r = v.as<JsonArray>();
            if (r.size() < 2 || !r[0].is<const char*>()) continue;
            copyStr(s.netpN[ni], sizeof s.netpN[ni], r[0] | "");
            s.netpB[ni++] = r[1] | 0.f;
          }
          s.netpCount = ni;
        }
        copyStr(s.pcId, sizeof s.pcId, d["pc_id"] | "");
        copyStr(s.pcHost, sizeof s.pcHost, d["pc_host"] | "");
        s.histSince = d["net_hist_since"] | 0LL;
        s.histCount = 0;
        if (d["net_hist"].is<JsonArray>()) {
          int hi = 0;
          for (JsonObject o : d["net_hist"].as<JsonArray>()) {
            if (hi >= 8) break;
            if (!o["p"].is<const char*>()) continue;
            copyStr(s.histN[hi], sizeof s.histN[hi], o["p"] | "");
            s.histRx[hi] = max(0.0, o["rx"] | 0.0); s.histTx[hi] = max(0.0, o["tx"] | 0.0);
            hi++;
          }
          s.histCount = hi;
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
  s.lastOkMs = s.api ? s.stampMs : gStat.lastOkMs;
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

// OTA over Wi-Fi (ArduinoOTA / espota), only when an OTA password is saved in the portal.
// Runs in its own task so ArduinoOTA.handle() (and the blocking transfer once an upload
// starts) never stalls the UI loop; the UI shows progress from gOtaPct.
static const char* OTA_HOST = "kvm-geek";
volatile int gOtaPct = -1;           // -1 idle, 0..100 uploading
volatile bool gOtaErr = false;
void otaTask(void*) {
  while (WiFi.status() != WL_CONNECTED) delay(500);
  ArduinoOTA.setHostname(OTA_HOST);
  ArduinoOTA.setPassword(cfg.ota.c_str());
  ArduinoOTA.setRebootOnSuccess(true);
  ArduinoOTA.onStart([] { gOtaPct = 0; gOtaErr = false; gDeskOn = false; gTermOn = false; });
  ArduinoOTA.onProgress([](unsigned int done, unsigned int total) { if (total) gOtaPct = done * 100 / total; });
  ArduinoOTA.onError([](ota_error_t e) { Serial.printf("OTA error %u\n", e); gOtaErr = true; gOtaPct = -1; });
  ArduinoOTA.begin();
  Serial.printf("OTA ready: %s.local / %s\n", OTA_HOST, WiFi.localIP().toString().c_str());
  for (;;) { ArduinoOTA.handle(); delay(20); }
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
// Palette + ramp per theme. Pick with -DGEEK_THEME=n in platformio.ini build_flags:
//   0 Classic (default): dark navy, green->yellow->orange->red ramp
//     (GEEK_RAMP_LOW_BLUE=1 starts it at blue instead)
//   1 Synthwave: deep indigo, teal -> violet -> magenta -> hot pink
//   2 Amber CRT: near-black, dim amber -> bright amber -> white-hot
//   3 Nord/Arctic: slate, frost blue -> aurora purple -> warm coral
//   4 Mono+accent: true black, white/grey text, one cyan accent; stepped orange past warn, red past hot
//   5 Solarized dark: base03 bg, official base/accent colours, cyan -> yellow -> orange -> red
//   6 Light/paper: off-white bg, dark text, muted teal -> ochre -> brick
//   7 Mono+bands (default): theme 4, but every value is one of 4 flat colours,
//     green < yellow < orange < red, on fixed thresholds (no gradient):
//     temps  <50 / <65 / <80 / >=80 C;  load, cpu, cores, mem, gpu  <50 / <75 / <90 / >=90 %
// Rail rows are ZERO, LINKED, NO HOST, KEYS: gradient top, bottom, letter ink.
// The ramp's `warn` point always lands on the 0.75 stop, so "hot" sits in the same place.
#ifndef GEEK_THEME
#define GEEK_THEME 7
#endif
#ifndef GEEK_RAMP_LOW_BLUE
#define GEEK_RAMP_LOW_BLUE 0
#endif
struct Stop { float at; uint8_t r, g, b; };
struct Rgb { uint8_t r, g, b; };
#define P(r, g, b) rgb(r, g, b)
#if GEEK_THEME == 1
const uint16_t C_BG = P(20, 12, 40), C_TXT = P(255, 255, 255), C_DIM = P(160, 140, 210),
               C_VAL = P(235, 222, 255), C_SOFT = P(212, 200, 238),
               C_OK = P(60, 240, 220), C_WARN = P(255, 170, 70), C_BAD = P(255, 60, 130),
               C_EDGE = P(84, 50, 140), C_HDR = P(40, 24, 76), C_TRACK = P(36, 24, 64),
               C_LOW = P(60, 230, 220), C_WARM = P(220, 80, 230), C_HOT = P(255, 50, 140),
               C_RX = P(60, 230, 220), C_TX = P(255, 120, 220),
               C_RISE = P(255, 140, 220), C_FALL = P(90, 240, 230);
const Stop RAMP[] = {{0.f, 50, 220, 210}, {0.35f, 110, 160, 255}, {0.6f, 190, 90, 240},
                     {0.75f, 235, 70, 220}, {1.f, 255, 50, 130}};
const Rgb RAIL_C[4][3] = {{{78, 64, 104}, {34, 26, 52}, {200, 190, 225}},
                          {{80, 240, 220}, {20, 120, 130}, {8, 20, 36}},
                          {{255, 170, 100}, {150, 64, 50}, {36, 12, 8}},
                          {{180, 150, 255}, {72, 44, 170}, {12, 6, 32}}};
#elif GEEK_THEME == 2
const uint16_t C_BG = P(10, 8, 4), C_TXT = P(255, 224, 160), C_DIM = P(176, 124, 44),
               C_VAL = P(255, 204, 120), C_SOFT = P(232, 184, 104),
               C_OK = P(255, 190, 60), C_WARN = P(255, 232, 150), C_BAD = P(255, 252, 236),
               C_EDGE = P(104, 68, 12), C_HDR = P(40, 26, 4), C_TRACK = P(34, 22, 4),
               C_LOW = P(200, 130, 30), C_WARM = P(255, 190, 50), C_HOT = P(255, 252, 236),
               C_RX = P(255, 184, 64), C_TX = P(196, 128, 32),
               C_RISE = P(255, 240, 200), C_FALL = P(200, 146, 56);
const Stop RAMP[] = {{0.f, 170, 100, 14}, {0.4f, 232, 144, 24}, {0.75f, 255, 196, 60},
                     {0.9f, 255, 232, 160}, {1.f, 255, 255, 245}};
const Rgb RAIL_C[4][3] = {{{70, 50, 18}, {28, 20, 6}, {190, 136, 50}},
                          {{255, 190, 60}, {150, 90, 10}, {22, 12, 0}},
                          {{255, 240, 190}, {176, 136, 64}, {34, 22, 0}},
                          {{190, 126, 34}, {84, 52, 6}, {255, 226, 160}}};
#elif GEEK_THEME == 3
const uint16_t C_BG = P(46, 52, 64), C_TXT = P(236, 239, 244), C_DIM = P(156, 166, 186),
               C_VAL = P(229, 233, 240), C_SOFT = P(216, 222, 233),
               C_OK = P(163, 190, 140), C_WARN = P(235, 203, 139), C_BAD = P(220, 110, 118),
               C_EDGE = P(86, 96, 118), C_HDR = P(59, 66, 82), C_TRACK = P(36, 41, 52),
               C_LOW = P(136, 192, 208), C_WARM = P(180, 142, 173), C_HOT = P(240, 120, 100),
               C_RX = P(136, 192, 208), C_TX = P(208, 135, 112),
               C_RISE = P(235, 203, 139), C_FALL = P(143, 188, 187);
const Stop RAMP[] = {{0.f, 136, 200, 216}, {0.3f, 129, 161, 213}, {0.55f, 180, 142, 200},
                     {0.75f, 214, 132, 150}, {1.f, 246, 118, 96}};
const Rgb RAIL_C[4][3] = {{{96, 106, 128}, {59, 66, 82}, {226, 230, 238}},
                          {{163, 200, 140}, {88, 120, 78}, {28, 34, 42}},
                          {{235, 203, 139}, {168, 126, 66}, {40, 32, 18}},
                          {{136, 192, 208}, {78, 116, 150}, {24, 30, 40}}};
#elif GEEK_THEME == 4
#define RAMP_STEPPED 1   // flat colour per band: cyan < warn <= orange < hot <= red
const uint16_t C_BG = P(0, 0, 0), C_TXT = P(255, 255, 255), C_DIM = P(140, 140, 140),
               C_VAL = P(224, 224, 224), C_SOFT = P(190, 190, 190),
               C_OK = P(0, 200, 230), C_WARN = P(255, 150, 0), C_BAD = P(255, 50, 40),
               C_EDGE = P(72, 72, 72), C_HDR = P(26, 26, 26), C_TRACK = P(30, 30, 30),
               C_LOW = P(0, 200, 230), C_WARM = P(255, 150, 0), C_HOT = P(255, 50, 40),
               C_RX = P(0, 200, 230), C_TX = P(200, 200, 200),
               C_RISE = P(255, 150, 0), C_FALL = P(0, 200, 230);
const Stop RAMP[] = {{0.f, 0, 200, 230}, {0.75f, 255, 150, 0}, {0.9f, 255, 50, 40}};
const Rgb RAIL_C[4][3] = {{{64, 64, 64}, {30, 30, 30}, {210, 210, 210}},
                          {{0, 200, 230}, {0, 110, 130}, {0, 0, 0}},
                          {{255, 150, 0}, {150, 80, 0}, {0, 0, 0}},
                          {{210, 210, 210}, {110, 110, 110}, {0, 0, 0}}};
#elif GEEK_THEME == 7
#define RAMP_BANDS 1
const uint16_t C_BG = P(0, 0, 0), C_TXT = P(255, 255, 255), C_DIM = P(140, 140, 140),
               C_VAL = P(224, 224, 224), C_SOFT = P(190, 190, 190),
               C_OK = P(0, 200, 230), C_WARN = P(255, 150, 0), C_BAD = P(255, 50, 40),
               C_EDGE = P(72, 72, 72), C_HDR = P(26, 26, 26), C_TRACK = P(30, 30, 30),
               C_LOW = P(0, 200, 230), C_WARM = P(255, 150, 0), C_HOT = P(255, 50, 40),
               C_RX = P(0, 200, 230), C_TX = P(200, 200, 200),
               C_RISE = P(255, 150, 0), C_FALL = P(0, 200, 230);
// green, yellow, orange, red
const Stop RAMP[] = {{0.f, 40, 210, 80}, {0.f, 240, 220, 40}, {0.f, 255, 140, 0}, {0.f, 255, 45, 40}};
const float BAND_TEMP_C[3] = {50.f, 65.f, 80.f};
const float BAND_PCT[3] = {0.50f, 0.75f, 0.90f};
const Rgb RAIL_C[4][3] = {{{128, 128, 128}, {48, 48, 48}, {245, 245, 245}},
                          {{0, 200, 230}, {0, 110, 130}, {0, 0, 0}},
                          {{255, 150, 0}, {150, 80, 0}, {0, 0, 0}},
                          {{210, 210, 210}, {110, 110, 110}, {0, 0, 0}}};
#elif GEEK_THEME == 5
// Solarized: base03 002b36, base02 073642, base01 586e75, base0 839496, base1 93a1a1,
// base2 eee8d5, base3 fdf6e3; yellow b58900 orange cb4b16 red dc322f blue 268bd2 cyan 2aa198 green 859900.
const uint16_t C_BG = P(0x00, 0x2b, 0x36), C_TXT = P(0xee, 0xe8, 0xd5), C_DIM = P(0x58, 0x6e, 0x75),
               C_VAL = P(0x93, 0xa1, 0xa1), C_SOFT = P(0x83, 0x94, 0x96),
               C_OK = P(0x85, 0x99, 0x00), C_WARN = P(0xb5, 0x89, 0x00), C_BAD = P(0xdc, 0x32, 0x2f),
               C_EDGE = P(0x58, 0x6e, 0x75), C_HDR = P(0x07, 0x36, 0x42), C_TRACK = P(0x07, 0x36, 0x42),
               C_LOW = P(0x2a, 0xa1, 0x98), C_WARM = P(0xcb, 0x4b, 0x16), C_HOT = P(0xdc, 0x32, 0x2f),
               C_RX = P(0x2a, 0xa1, 0x98), C_TX = P(0xb5, 0x89, 0x00),
               C_RISE = P(0xcb, 0x4b, 0x16), C_FALL = P(0x2a, 0xa1, 0x98);
const Stop RAMP[] = {{0.f, 0x2a, 0xa1, 0x98}, {0.5f, 0xb5, 0x89, 0x00}, {0.75f, 0xcb, 0x4b, 0x16},
                     {1.f, 0xdc, 0x32, 0x2f}};
const Rgb RAIL_C[4][3] = {{{0x07, 0x36, 0x42}, {0x00, 0x2b, 0x36}, {0x93, 0xa1, 0xa1}},
                          {{0x85, 0x99, 0x00}, {80, 92, 0}, {0x00, 0x2b, 0x36}},
                          {{0xb5, 0x89, 0x00}, {110, 80, 0}, {0x00, 0x2b, 0x36}},
                          {{0x26, 0x8b, 0xd2}, {20, 80, 130}, {0xfd, 0xf6, 0xe3}}};
#elif GEEK_THEME == 6
const uint16_t C_BG = P(244, 240, 228), C_TXT = P(36, 36, 40), C_DIM = P(116, 112, 102),
               C_VAL = P(48, 48, 54), C_SOFT = P(78, 76, 70),
               C_OK = P(40, 130, 90), C_WARN = P(176, 116, 16), C_BAD = P(170, 50, 40),
               C_EDGE = P(168, 160, 142), C_HDR = P(224, 218, 200), C_TRACK = P(222, 216, 200),
               C_LOW = P(40, 128, 138), C_WARM = P(186, 132, 36), C_HOT = P(160, 52, 40),
               C_RX = P(30, 120, 130), C_TX = P(170, 110, 20),
               C_RISE = P(170, 70, 40), C_FALL = P(30, 120, 110);
const Stop RAMP[] = {{0.f, 44, 132, 140}, {0.5f, 186, 140, 44}, {0.75f, 184, 104, 40},
                     {1.f, 156, 46, 36}};
const Rgb RAIL_C[4][3] = {{{204, 200, 190}, {168, 162, 148}, {56, 54, 50}},
                          {{60, 150, 110}, {28, 98, 70}, {250, 248, 240}},
                          {{214, 154, 54}, {160, 100, 20}, {40, 24, 0}},
                          {{70, 130, 172}, {38, 88, 130}, {250, 248, 240}}};
#else
const uint16_t C_BG = P(12, 16, 24), C_TXT = 0xFFFF, C_DIM = P(130, 140, 165),
               C_VAL = P(220, 228, 240), C_SOFT = P(197, 208, 200),
               C_OK = P(60, 220, 120), C_WARN = P(255, 170, 40), C_BAD = P(255, 70, 70),
               C_EDGE = P(44, 56, 80), C_HDR = P(26, 34, 52), C_TRACK = P(22, 30, 44),
               C_LOW = P(48, 176, 255), C_WARM = P(255, 186, 48), C_HOT = P(255, 60, 60),
               C_RX = P(48, 224, 136), C_TX = P(245, 165, 45),
               C_RISE = P(255, 200, 90), C_FALL = P(110, 245, 190);
#if GEEK_RAMP_LOW_BLUE
const Stop RAMP[] = {{0.f, 48, 140, 255}, {0.25f, 40, 210, 200}, {0.5f, 230, 220, 50}, {0.75f, 255, 150, 40}, {1.f, 255, 60, 60}};
#else
const Stop RAMP[] = {{0.f, 40, 200, 90}, {0.45f, 230, 220, 50}, {0.75f, 255, 150, 40}, {1.f, 255, 60, 60}};
#endif
const Rgb RAIL_C[4][3] = {{{50, 52, 60}, {20, 22, 28}, {180, 184, 196}},
                          {{94, 234, 144}, {18, 122, 66}, {4, 34, 8}},
                          {{255, 210, 122}, {138, 90, 16}, {42, 24, 4}},
                          {{126, 182, 238}, {26, 78, 134}, {4, 16, 24}}};
#endif
#undef P
inline uint16_t railC(int st, int k) { const Rgb& c = RAIL_C[st][k]; return rgb(c.r, c.g, c.b); }
// One status colour for the rail and every panel header name, so they always agree.
// ST_ZERO/LINKED/NOHOST/KEYS are the rail states (their rail top colour); ST_OK / ST_WARN /
// ST_BAD are the per-device link states used by the ZERO and GEEK headers.
enum { ST_ZERO = 0, ST_LINKED = 1, ST_NOHOST = 2, ST_KEYS = 3, ST_OK, ST_WARN, ST_BAD };
uint16_t statusC(int st) {
  if (st <= ST_KEYS) return railC(st, 0);
  if (st == ST_OK) return railC(ST_LINKED, 0);
  if (st == ST_WARN) return railC(ST_NOHOST, 0);
  return C_BAD;
}
// Type and spacing. Small = 6x8 GFX font, big = 12x16.
const int FS_S = 1, FS_L = 2, CW = 6, LINE = 10, PAD = 4, GAP = 2, HDR_H = 10, RADIUS = 3;
// Shared scales: level in 0..1 plus the point where the ramp turns hot.
const float T_LO = 35.f, T_SPAN = 60.f;
inline float tempLevel(float c) { return (c - T_LO) / T_SPAN; }

// The single value -> colour ramp (theme RAMP above), used by every bar, spark, core and temp.
const int RAMP_N = sizeof RAMP / sizeof RAMP[0];
// isTemp: t is tempLevel(celsius); otherwise t is a 0..1 fraction (load/ncpu, cpu, mem, gpu).
uint16_t ramp(float t, float warn = 0.75f, bool isTemp = false) {
  if (isnan(t) || t < 0.f) t = 0.f;
  if (t > 1.f) t = 1.f;
#ifdef RAMP_BANDS
  (void)warn;
  int k = 0;
  for (int i = 0; i < 3; i++) if (t >= (isTemp ? tempLevel(BAND_TEMP_C[i]) : BAND_PCT[i])) k = i + 1;
  return rgb(RAMP[k].r, RAMP[k].g, RAMP[k].b);
#endif
  if (!(warn > 0.05f && warn < 0.95f)) warn = 0.75f;
  float u = t < warn ? t / warn * 0.75f : 0.75f + (t - warn) / (1.f - warn) * 0.25f;
#ifdef RAMP_STEPPED
  int k = 0;
  for (int i = 1; i < RAMP_N; i++) if (u >= RAMP[i].at) k = i;
  return rgb(RAMP[k].r, RAMP[k].g, RAMP[k].b);
#endif
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
void drawCapsule(int x, int y, int w, int h, float level, float peakLevel, float warnAt, bool dim, bool isTemp = false) {
  if (isnan(level) || level < 0.f) level = 0.f;
  float shown = level > 1.f ? 1.f : level;
  fb.fillRoundRect(x, y, w, h, h / 2, C_TRACK);
  int inner = w - 2;
  int fw = (int)(shown * inner + 0.5f);
  for (int dx = 0; dx < fw; dx++) {
    float p = inner > 1 ? (float)dx / (float)(inner - 1) : 0.f;
#if defined(RAMP_BANDS) || defined(RAMP_STEPPED)
    (void)p;   // stepped themes: one solid colour per bar, from the current value's step
    uint16_t col = dimmed(ramp(shown, warnAt, isTemp), dim);
#else
    uint16_t col = dimmed(ramp(p, warnAt, isTemp), dim);
#endif
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
float lvTemp(float v, float);
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
    uint16_t c = dimmed(ramp(level(v, k), warnAt, level == lvTemp), dim);
    if (prevX >= 0 && px >= prevX) {
      int span = max(1, px - prevX);
      uint16_t cTop = mix(C_BG, c, dim ? 65 : 115), cBot = mix(C_BG, c, dim ? 25 : 45);
#ifdef RAMP_BANDS
      cTop = cBot = mix(C_BG, c, dim ? 40 : 80);   // flat fill: no shading gradient
#endif
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
#ifdef RAMP_BANDS
    if (fh > 0) fb.fillRect(bx, y + h - 1 - fh, bw, fh, col);
#else
    if (fh > 0) vgrad(bx, y + h - 1 - fh, bw, fh, mix(col, C_TXT, 60), col);
#endif
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
    if (on[i]) { fb.fillCircle(cx, cy, 3, C_FALL); fb.drawCircle(cx, cy, 3, ink); }
    else fb.drawCircle(cx, cy, 2, ink);
  }
}

// Shared ZERO / GEEK detail panels (both pages). `rows` = how many text lines fit.
void drawZeroPanel(int x, int y, int w, int h, const Status& s, int rows) {
  Box b = panel(x, y, w, h, "ZERO", statusC(s.api ? ST_OK : ST_BAD));
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
  Box b = panel(x, y, w, h, "GEEK", statusC(!s.wifi ? ST_BAD : gPinBad ? ST_WARN : ST_OK));
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

// ---- v2 display primitives (groundwork for the next skin) -------------------------------
// Stale: no good /api/status for STALE_MS. The backlight dims and pages show a stale badge;
// both recover on the next good poll.
static const uint32_t STALE_MS = 6000;
static const uint8_t BL_FULL = 255, BL_DIM = 60;
bool isStale(const Status& s, uint32_t now) { return !s.lastOkMs || now - s.lastOkMs > STALE_MS; }
void fmtAge(char* b, size_t n, const Status& s, uint32_t now) {
  if (!s.lastOkMs) snprintf(b, n, "never");
  else { uint32_t a = (now - s.lastOkMs) / 1000; if (a < 120) snprintf(b, n, "%us", (unsigned)a); else snprintf(b, n, "%um", (unsigned)(a / 60)); }
}

// Heat colour, t in 0..1: green -> yellow -> red, smooth, RGB565.
uint16_t heatC(float t) {
  if (isnan(t) || t < 0.f) t = 0.f;
  if (t > 1.f) t = 1.f;
  if (t < 0.5f) return rgb((uint8_t)(40 + 215 * t * 2.f), 220, 60);
  return rgb(255, (uint8_t)(220 - 170 * (t - 0.5f) * 2.f), (uint8_t)(60 - 20 * (t - 0.5f) * 2.f));
}

// Filled history graph: one column per sample (newest at the right), each column in the heat
// colour of its own value (lv maps value -> 0..1), fading toward the bottom, brighter top pixel.
// NAN samples leave a gap. Uses the last min(n, w) samples.
void drawHeatGraph(int x, int y, int w, int h, const float* series, int n, int next,
                   float (*lv)(float, float), float k, bool dim) {
  if (n <= 0 || w <= 0 || h <= 1) return;
  int start = (n < HIST) ? 0 : next;
  int cols = min(n, w);
  for (int i = 0; i < cols; i++) {
    float v = series[(start + n - cols + i) % HIST];
    if (isnan(v)) continue;
    float t = lv(v, k);
    if (t < 0.f) t = 0.f;
    if (t > 1.f) t = 1.f;
    int ch = max(1, (int)(t * (h - 1) + 0.5f));
    int cx = x + w - cols + i, top = y + h - ch;
    uint16_t c = dimmed(heatC(t), dim);
    for (int yy = top + 1; yy < y + h; yy++) fb.drawPixel(cx, yy, mix(c, C_BG, (yy - top) * 200 / h));
    fb.drawPixel(cx, top, mix(c, 0xFFFF, dim ? 0 : 70));
  }
}

// Beveled dark card: lit top/left edge, shaded bottom/right, soft vertical gradient, 3 px corners.
void bevelCard(int x, int y, int w, int h, uint16_t base = 0) {
  if (!base) base = rgb(34, 38, 46);
  uint16_t hi = mix(base, 0xFFFF, 70), lo = mix(base, 0x0000, 150);
  fb.fillRoundRect(x, y, w, h, RADIUS, lo);                        // bottom/right shade
  fb.fillRoundRect(x, y, w - 1, h - 1, RADIUS, hi);                // top/left light
  for (int i = 1; i < h - 2; i++)                                  // face
    fb.drawFastHLine(x + (i < RADIUS ? RADIUS - i + 1 : 1), y + i,
                     w - 3 - 2 * (i < RADIUS ? RADIUS - i : 0), mix(mix(base, 0xFFFF, 18), mix(base, 0x0000, 60), i * 255 / h));
}

// ---- v2 skin: shared 3-palette instrument system (synced live with web console) -----------
// 0 = Instrument Slate (default), 1 = Warm Braun, 2 = Machined Cobalt.
struct VTheme {
  uint16_t bg, cell, cellHi, cellLo, well, zebra;
  uint16_t lab, white, grey, dimg, div;
  uint16_t rx, tx, ok, warn, hot, bad;
  uint8_t baseR, baseG, baseB;
};
static const VTheme VTHEMES[3] = {
  // 0: GEEK Hybrid Default (#2 matte charcoal ground + #3 studio graphs/bars/warning ramp)
  { rgb(9,9,10),    rgb(22,21,25),  rgb(41,39,48),  rgb(4,4,5),     rgb(13,12,15),  rgb(18,17,22),
    rgb(116,168,238), rgb(244,239,230), rgb(150,145,139), rgb(58,56,66), rgb(34,32,40),
    rgb(45,212,191), rgb(129,140,248), rgb(52,211,153), rgb(255,202,68), rgb(255,140,52), rgb(255,78,96),
    236, 232, 224 },
  // 1: Warm Braun
  { rgb(16,15,14),  rgb(26,24,22),  rgb(48,44,39),  rgb(8,7,6),     rgb(11,10,9),   rgb(21,19,17),
    rgb(238,168,72), rgb(246,241,230), rgb(148,140,128), rgb(62,56,50), rgb(42,38,34),
    rgb(245,172,56), rgb(118,206,180), rgb(108,208,118), rgb(248,196,64), rgb(246,122,42), rgb(238,68,58),
    236, 229, 216 },
  // 2: Machined Cobalt
  { rgb(8,13,24),   rgb(15,23,40),  rgb(32,48,78),  rgb(4,7,14),    rgb(5,9,18),    rgb(12,19,34),
    rgb(114,186,255), rgb(238,244,255), rgb(124,144,176), rgb(42,56,82), rgb(26,40,68),
    rgb(76,220,255), rgb(168,132,255), rgb(64,224,152), rgb(255,202,68), rgb(255,140,52), rgb(255,78,96),
    224, 234, 248 }
};
static uint8_t gUiTheme = 0;
inline const VTheme& VT() { return VTHEMES[gUiTheme < 3 ? gUiTheme : 0]; }
#define V_BG     (VT().bg)
#define V_CELL   (VT().cell)
#define V_WELL   (VT().well)
#define V_ZEBRA  (VT().zebra)
#define V_LAB    (VT().lab)
#define V_WHITE  (VT().white)
#define V_GREY   (VT().grey)
#define V_DIMG   (VT().dimg)
#define V_DIV    (VT().div)
#define V_OK     (VT().ok)
#define V_WARN   (VT().warn)
#define V_BAD    (VT().bad)
#define V_RX     (VT().rx)
#define V_TX     (VT().tx)
const uint16_t V_SHADOW = 0x0000;

// Raised beveled metric cell (well=false) or recessed graph/bar well (well=true).
void vCell(int x, int y, int w, int h, bool well = false) {
  if (w <= 2 || h <= 2) return;
  const VTheme& vt = VT();
  fb.fillRect(x, y, w, h, well ? vt.well : vt.cell);
  uint16_t tl = well ? vt.cellLo : vt.cellHi;
  uint16_t br = well ? vt.div : vt.cellLo;
  fb.drawFastHLine(x, y, w - 1, tl);
  fb.drawFastVLine(x, y, h - 1, tl);
  fb.drawFastHLine(x + 1, y + h - 1, w - 1, br);
  fb.drawFastVLine(x + w - 1, y + 1, h - 1, br);
}

uint16_t vHeat(float t) {   // theme off-white until 0.55, then warn -> hot -> bad
  const VTheme& vt = VT();
  if (isnan(t) || t < 0.f) t = 0.f;
  if (t > 1.f) t = 1.f;
  if (t < 0.55f) return rgb(vt.baseR, vt.baseG, vt.baseB);
  if (t < 0.72f) return mix(vt.warn, vt.hot, (int)((t - 0.55f) / 0.17f * 255.f));
  return mix(vt.hot, vt.bad, (int)((t - 0.72f) / 0.28f * 255.f));
}
// Each value's warm point lands on t = 0.55 (where vHeat leaves off-white), red at 1.
float vWarm(float v, float warm, float hot) { return 0.55f + 0.45f * (v - warm) / (hot - warm); }
float vT(float c) { return vWarm(c, 70.f, 90.f); }          // CPU/GPU/NVMe: warm 70 C, red 90 C
float vPct(int p) { return vWarm(p, 75.f, 98.f); }        // generic %: warm 75
float vMemP(int p) { return vWarm(p, 80.f, 97.f); }      // RAM: warm 80 %
float vDiskP(int p) { return vWarm(p, 90.f, 99.f); }     // disk: warm 90 %
int ftW(const GFXfont* f, const char* s) {
  int16_t x1, y1; uint16_t w, h; fb.setFont(f); fb.setTextSize(1); fb.getTextBounds(s, 0, 0, &x1, &y1, &w, &h);
  fb.setFont(nullptr); return f ? (int)w + x1 : (int)strlen(s) * CW;
}
void ft(int x, int y, const char* s, uint16_t c, const GFXfont* f) {   // y = baseline (top for nullptr)
  fb.setFont(f); fb.setTextSize(1); fb.setTextColor(c); fb.setCursor(x, y); fb.print(s); fb.setFont(nullptr);
}
// Headline numbers: 1px dark drop shadow under the glyphs for crisp depth on raised cells.
void ftS(int x, int y, const char* s, uint16_t c, const GFXfont* f) { ft(x + 1, y + 1, s, V_SHADOW, f); ft(x, y, s, c, f); }
void ftR(int right, int y, const char* s, uint16_t c, const GFXfont* f) { ft(right - ftW(f, s), y, s, c, f); }
void vDot(int x, int y, uint16_t c) { fb.fillCircle(x, y, 2, c); }
void pageDots(int cur) {   // pill indicator for the 4 stats pages (PC, cores, network, ZERO|GEEK)
  fb.fillRoundRect(W - 30, 1, 28, 7, 3, V_WELL);
  int x = W - 27;
  for (int i = 0; i < 4; i++) {
    if (i == cur) { fb.fillRoundRect(x, 3, 6, 3, 1, V_LAB); x += 8; }
    else { fb.fillRect(x, 3, 2, 3, V_DIMG); x += 5; }
  }
}
// Filled sparkline in one colour: bright top pixel, fill fading to the background.
void vSpark(int x, int y, int w, int h, const float* ser, int n, int next, float lo, float hi, uint16_t c, bool dim,
            bool logScale = false, bool invert = false) {   // invert: low values are the warning (RSSI)
  if (n <= 0) return;
  if (dim) c = mix(V_BG, c, 110);
  int start = (n < HIST) ? 0 : next, cols = min(n, w);
  for (int i = 0; i < cols; i++) {
    float v = ser[(start + n - cols + i) % HIST];
    if (isnan(v)) continue;
    float t = logScale ? logRate(v) : (v - lo) / (hi - lo);
    t = t < 0 ? 0 : t > 1 ? 1 : t;
    int ch = max(1, (int)(t * (h - 1) + 0.5f)), cx = x + w - cols + i, top = y + h - ch;
    float ht = invert ? 1.f - t : t;   // severity for colour; height still follows t
    uint16_t cc = !dim && ht >= 0.55f ? vHeat(ht) : c;   // a peak column takes the heat ramp, whole height
    for (int yy = top + 1; yy < y + h; yy++) fb.drawPixel(cx, yy, mix(mix(V_BG, cc, 150), V_BG, (yy - top) * 180 / h));
    fb.drawPixel(cx, top, dim ? c : ht >= 0.55f ? vHeat(ht) : mix(c, V_WHITE, 60));
  }
}
// Same as vSpark but hanging from the top edge (page 1 mirrored net graph: RX down from the top).
void vHang(int x, int y, int w, int h, const float* ser, int n, int next, uint16_t c, bool dim) {
  if (n <= 0) return;
  if (dim) c = mix(V_BG, c, 110);
  int start = (n < HIST) ? 0 : next, cols = min(n, w);
  for (int i = 0; i < cols; i++) {
    float v = ser[(start + n - cols + i) % HIST];
    if (isnan(v)) continue;
    float t = logRate(v); t = t < 0 ? 0 : t > 1 ? 1 : t;
    int ch = max(1, (int)(t * (h - 1) + 0.5f)), cx = x + w - cols + i, bot = y + ch - 1;
    uint16_t cc = !dim && t >= 0.55f ? vHeat(t) : c;
    for (int yy = y; yy < bot; yy++) fb.drawPixel(cx, yy, mix(mix(V_BG, cc, 150), V_BG, (bot - yy) * 180 / h));
    fb.drawPixel(cx, bot, dim ? c : t >= 0.55f ? vHeat(t) : mix(c, V_WHITE, 60));
  }
}
// Bytes as a short size: used 22.7 / 412, totals 32G / 931G / 1.8T.
void vFmtSize(char* b, size_t n, double v, bool unit) {
  const char* u = "G"; double g = v / 1073741824.0;
  if (g >= 1000.0) { g /= 1024.0; u = "T"; }
  // used (unit=false) keeps a decimal below 100; totals keep one only below 10 so "12.6/16G" fits the column
  if (unit ? g < 10.0 : g < 100.0) snprintf(b, n, "%.1f%s", g, unit ? u : "");
  else snprintf(b, n, "%.0f%s", g, unit ? u : "");
}
// Faint heat-coloured fill behind the page 1 hero.
void vHeatBg(int x, int y, int w, int h, const float* ser, int n, int next, float (*lv)(float), bool dim) {
  if (n <= 0) return;
  int start = (n < HIST) ? 0 : next, cols = min(n, w);
  for (int i = 0; i < cols; i++) {
    float v = ser[(start + n - cols + i) % HIST];
    if (isnan(v)) continue;
    float t = lv(v); t = t < 0 ? 0 : t > 1 ? 1 : t;
    int ch = max(2, (int)((0.25f + 0.75f * t) * (h - 1))), cx = x + w - cols + i, top = y + h - ch;
    uint16_t c = vHeat(t);
    for (int yy = top + 1; yy < y + h; yy++) fb.drawPixel(cx, yy, mix(V_BG, c, (dim ? 40 : 90) - (yy - top) * 50 / h));
    fb.drawPixel(cx, top, mix(V_BG, c, dim ? 90 : 200));
  }
}
void vFmtRate(char* b, size_t n, float bps) {
  if (isnan(bps)) snprintf(b, n, "--");
  else if (bps >= 1e6f) snprintf(b, n, "%.1fM/s", bps / 1e6f);
  else if (bps >= 1e3f) snprintf(b, n, "%.0fK/s", bps / 1e3f);
  else snprintf(b, n, "%.0fB/s", bps);
}
void vFmtUp(char* b, size_t n, long s) {
  if (s < 0) snprintf(b, n, "--");
  else if (s < 3600) snprintf(b, n, "%ldm", s / 60);
  else if (s < 86400) snprintf(b, n, "%ldh %ldm", s / 3600, (s % 3600) / 60);
  else snprintf(b, n, "%ldd %ldh", s / 86400, (s % 86400) / 3600);
}
// HID: green ready + host + configured, yellow ready but not enumerated, red not ready, grey unknown.
uint16_t hidC(const Status& s) {
  if (!s.api) return V_GREY;
  if (!s.hid) return V_BAD;
  return s.host && strcmp(s.udc, "configured") == 0 ? V_OK : V_WARN;
}
uint16_t rssiC(bool up, int dbm) { return !up || !dbm ? V_BAD : dbm >= -65 ? V_OK : dbm >= -78 ? V_WARN : V_BAD; }
uint16_t ageC(const Status& s, uint32_t now) {
  if (!s.lastOkMs) return V_BAD;
  uint32_t a = now - s.lastOkMs; return a < 4000 ? V_OK : a <= STALE_MS ? V_WARN : V_BAD;
}
// Bottom row: three evenly spaced "dot LABEL value" items, centred in thirds.

void drawPc(const Status& s, const PcHist& h, uint32_t now) {
  fb.fillScreen(V_BG);
  bool stale = isStale(s, now);
  bool linked = s.api && strcmp(s.link, "LINKED") == 0;
  float t = !isnan(s.pcCpuT) ? s.pcCpuT : s.pcTemp;
  bool have = linked && !isnan(t);
  const float* ts = h.n && !isnan(lastValue(h.cpuT, h.n, h.next)) ? h.cpuT : h.temp;
  char a[24], b[24];

  // Top-left beveled hero cell (CPU temp + thermal sparkline backdrop)
  const int HW = have ? 98 : 236;
  vCell(2, 2, HW, 53, false);
  vHeatBg(3, 3, HW - 2, 51, ts, h.n, h.next, vT, stale || !have);
  if (have) {
    txt(6, 5, "TEMP", V_LAB);
    snprintf(a, sizeof a, "%.0f", t);
    uint16_t c = stale ? V_GREY : vHeat(vT(t));
    ftS(6, 48, a, c, &FreeSansBold24pt7b);
    int dx = 6 + ftW(&FreeSansBold24pt7b, a) + 3;
    fb.drawCircle(dx + 4, 18, 4, c); fb.drawCircle(dx + 4, 18, 3, c);
  } else {
    ftS(8, 30, "OFFLINE", V_GREY, &FreeSansBold12pt7b);
    uint32_t ago = h.had ? (now - h.lastMs) / 1000 : 0;
    if (strcmp(s.helperState, "streaming") == 0) snprintf(b, sizeof b, "streaming helper...");
    else if (strcmp(s.helperState, "probing") == 0) snprintf(b, sizeof b, "probing USB serial...");
    else if (strcmp(s.helperState, "missing") == 0) snprintf(b, sizeof b, "helper unpaired");
    else if (h.had && ago >= 120) snprintf(b, sizeof b, "pc data %um ago", (unsigned)(ago / 60));
    else if (h.had) snprintf(b, sizeof b, "pc data %us ago", (unsigned)ago);
    else snprintf(b, sizeof b, "no pc data yet");
    txt(8, 40, b, V_LAB);
  }

  // Top-right beveled CPU load + top process + N/C/S lock pips
  if (have) {
    const int RX = 102, RW = W - 2 - RX;
    vCell(RX, 2, RW, 53, false);
    txt(RX + 5, 5, "CPU LOAD", V_LAB);
    if (s.host) {
      const char LCH[3] = {'N', 'C', 'S'};
      const bool LON[3] = {s.ledNum, s.ledCaps, s.ledScroll};
      for (int li = 0; li < 3; li++) {
        int lx = W - 56 + li * 8;
        char ls[2] = {LCH[li], 0};
        txt(lx, 5, ls, LON[li] ? V_RX : V_DIMG);
      }
    }
    fmtLoad(a, sizeof a, s.pcLoad); snprintf(b, sizeof b, "%s load", a);
    ftS(RX + 5, 34, b, stale ? V_GREY : V_WHITE, &FreeSansBold12pt7b);
    if (s.pcTopProc[0]) {
      char pct[8] = "";
      if (!isnan(s.pcTopCpu)) snprintf(pct, sizeof pct, " %.0f%%", s.pcTopCpu);
      int avail = W - 6 - (RX + 5) - ftW(&FreeSansBold9pt7b, pct);
      char nm[20]; strlcpy(nm, s.pcTopProc, sizeof nm);
      if (ftW(&FreeSansBold9pt7b, nm) > avail) {
        int L = strlen(nm);
        while (L > 1) { nm[--L] = 0; char tmp[24]; snprintf(tmp, sizeof tmp, "%s..", nm); if (ftW(&FreeSansBold9pt7b, tmp) <= avail) break; }
        strlcat(nm, "..", sizeof nm);
      }
      ft(RX + 5, 50, nm, V_LAB, &FreeSansBold9pt7b);
      ft(RX + 5 + ftW(&FreeSansBold9pt7b, nm), 50, pct, stale ? V_GREY : vHeat(s.pcTopCpu / 100.f), &FreeSansBold9pt7b);
    }
  }

  // Bottom-left stacked beveled cells: hot GPU/NVMe sensors first, else RAM & DISK
  const int NX = 70;
  const float T_WARM = 70.f;
  bool gpuHot = linked && !isnan(s.pcGpuT) && s.pcGpuT >= T_WARM;
  bool nvHot = linked && !isnan(s.pcNvmeT) && s.pcNvmeT >= T_WARM;
  int slot[2]; int ns = 0;
  if (gpuHot) slot[ns++] = 0;
  if (nvHot && ns < 2) slot[ns++] = 1;
  if (ns < 2) slot[ns++] = 2;
  if (ns < 2) slot[ns++] = 3;
  for (int r = 0; r < ns; r++) {
    int cy = r ? 88 : 57, kind = slot[r];
    vCell(2, cy, NX - 4, 29, false);
    if (kind <= 1) {
      float tv = kind ? s.pcNvmeT : s.pcGpuT;
      txt(6, cy + 3, kind ? "NVME" : "GPU", V_LAB);
      snprintf(a, sizeof a, "%.0f", tv);
      ftS(6, cy + 25, a, stale ? V_GREY : vHeat(vT(tv)), &FreeSansBold9pt7b);
      int dx = 6 + ftW(&FreeSansBold9pt7b, a) + 2;
      fb.drawCircle(dx + 2, cy + 15, 2, stale ? V_GREY : vHeat(vT(tv)));
      continue;
    }
    bool disk = kind == 3;
    double u = disk ? s.pcDiskUsed : s.pcMemUsed, tot = disk ? s.pcDiskTot : s.pcMemTot;
    int pct = linked ? (disk ? s.pcDisk : s.pcMem) : -1;
    char lab[12];
    if (disk && s.pcDiskDev[0]) {
      strlcpy(lab, s.pcDiskDev, sizeof lab);
      for (char* p = lab; *p; p++) if (*p >= 'a' && *p <= 'z') *p = (char)(*p - 'a' + 'A');
    } else strlcpy(lab, disk ? "DISK" : "RAM", sizeof lab);
    if ((int)strlen(lab) > (NX - 10) / CW) lab[(NX - 10) / CW] = 0;
    txt(6, cy + 3, lab, V_LAB);
    bool bytes = linked && !isnan(u) && !isnan(tot) && tot > 0;
    if (bytes) {
      float f = (float)(u / tot); char t2[12];
      vFmtSize(a, sizeof a, u, false); snprintf(t2, sizeof t2, "/"); vFmtSize(t2 + 1, sizeof t2 - 1, tot, true);
      ftS(6, cy + 25, a, stale ? V_GREY : vHeat(disk ? vDiskP((int)(f * 100)) : vMemP((int)(f * 100))), &FreeSansBold9pt7b);
      txt(6 + ftW(&FreeSansBold9pt7b, a) + 1, cy + 17, t2, V_GREY);
    } else {
      fmtMem(a, sizeof a, pct);
      ftS(6, cy + 25, a, pct < 0 || stale ? V_GREY : vHeat(disk ? vDiskP(pct) : vMemP(pct)), &FreeSansBold9pt7b);
    }
  }

  // Bottom-right beveled network card with recessed duplex waterfall well
  vCell(NX, 57, W - 2 - NX, 60, false);
  const char* ifn = s.netIf[0] ? s.netIf : "NET";
  char ifb[9]; strlcpy(ifb, ifn, sizeof ifb); txt(NX + 4, 60, ifb, V_LAB);
  float rx = linked ? s.pcRx : NAN, tx = linked ? s.pcTx : NAN;
  vFmtRate(b, sizeof b, tx); int x2 = W - 6 - strlen(b) * CW; txt(x2, 60, b, stale || isnan(tx) ? V_GREY : V_WHITE);
  int ax = x2 - 5; fb.drawFastVLine(ax, 60, 7, V_TX); fb.drawLine(ax - 2, 62, ax, 60, V_TX); fb.drawLine(ax + 2, 62, ax, 60, V_TX);
  vFmtRate(a, sizeof a, rx); int x1 = ax - 6 - strlen(a) * CW; txt(x1, 60, a, stale || isnan(rx) ? V_GREY : V_WHITE);
  ax = x1 - 5; fb.drawFastVLine(ax, 60, 7, V_RX); fb.drawLine(ax - 2, 64, ax, 66, V_RX); fb.drawLine(ax + 2, 64, ax, 66, V_RX);
  const int WX = NX + 3, WY = 70, WW = W - 8 - NX, WH = 44, GH = 20;
  vCell(WX, WY, WW, WH, true);
  fb.drawFastHLine(WX + 1, WY + WH / 2, WW - 2, V_DIV);
  vHang(WX + 1, WY + 1, WW - 2, GH, h.rx, h.n, h.next, V_RX, stale);
  vSpark(WX + 1, WY + WH - 1 - GH, WW - 2, GH, h.tx, h.n, h.next, 0, 1, V_TX, stale, true);
  pageDots(0);
}

void drawZg(const Status& s, const PcHist& h, uint32_t now) {
  fb.fillScreen(V_BG);
  bool stale = isStale(s, now);
  char a[24], b[24];
  const int MID = 120;
  vCell(2, 2, MID - 3, 115, false);
  vCell(MID + 1, 2, W - 3 - MID, 115, false);

  // ZERO card
  int L = 6, R = MID - 5;
  txt(L, 5, "ZERO", V_LAB);
  ftS(L, 36, s.api ? "HID" : "--", stale || !s.api ? V_GREY : V_WHITE, &FreeSansBold18pt7b);
  uint16_t val = stale ? V_GREY : V_WHITE;
  const char* zip = s.tsIp[0] ? s.tsIp : s.ip;
  txt(L, 42, "load", V_LAB); fmtLoad(a, sizeof a, s.api ? s.load1 : NAN); txtR(R, 42, a, val);
  txt(L, 52, "temp", V_LAB); fmtTemp(a, sizeof a, s.api ? s.temp : NAN);
  txtR(R, 52, a, stale || !s.api || isnan(s.temp) ? V_GREY : vHeat(vT(s.temp)));
  txt(L, 62, "up", V_LAB); vFmtUp(a, sizeof a, s.api ? s.upS : -1); txtR(R, 62, a, val);
  txt(L, 72, "ip", V_LAB); txtR(R, 72, s.api && zip[0] ? zip : "--", s.api && zip[0] ? V_WHITE : V_GREY);
  if (s.ts >= 0) vDot(L + 16, 75, s.ts == 1 ? V_OK : V_DIMG);
  txt(L, 84, "load hist", V_RX);
  vCell(L, 94, R - L, 19, true);
  vSpark(L + 1, 95, R - L - 2, 17, h.piLoad, h.n, h.next, 0, 1.5f, V_RX, stale);

  // GEEK card
  L = MID + 5; R = W - 6;
  txt(L, 5, "GEEK", V_LAB);
  if (s.wifi && s.rssi) snprintf(a, sizeof a, "%d", s.rssi); else strcpy(a, "--");
  ftS(L, 36, a, s.wifi ? V_WHITE : V_GREY, &FreeSansBold18pt7b);
  txt(L + ftW(&FreeSansBold18pt7b, a) + 3, 29, "dBm", V_LAB);
  txt(L, 42, "ping", V_LAB);
  if (s.api) snprintf(a, sizeof a, "%ums", (unsigned)s.latencyMs); else strcpy(a, "--");
  txtR(R, 42, a, s.api ? V_WHITE : V_GREY);
  auto k = [](char* o, size_t n, uint32_t v) { if (v >= 10000) snprintf(o, n, "%.1fk", v / 1000.f); else snprintf(o, n, "%u", (unsigned)v); };
  txt(L, 52, "link", V_LAB);
  if (s.polls) {
    k(a, sizeof a, s.polls - s.fails); k(b, sizeof b, s.polls);
    int bw = CW * (1 + strlen(b)); txtR(R, 52, b, V_WHITE); txtR(R - CW * strlen(b), 52, "/", V_GREY); txtR(R - bw, 52, a, V_WHITE);
  } else txtR(R, 52, "--", V_GREY);
  txt(L, 62, "up", V_LAB); vFmtUp(a, sizeof a, (long)(millis() / 1000)); txtR(R, 62, a, V_WHITE);
  txt(L, 72, "ip", V_LAB); txtR(R, 72, s.wifi ? s.geekIp : "--", s.wifi ? V_WHITE : V_GREY);
  txt(L, 84, "wifi hist", V_TX);
  vCell(L, 94, R - L, 19, true);
  vSpark(L + 1, 95, R - L - 2, 17, h.geekRssi, h.n, h.next, -90, -40, V_TX, false, false, true);
  pageDots(3);
}

// v2 page 2: per-core bars in a recessed well on the left, busiest processes on a zebra-striped
// raised card on the right with 1px proportional CPU load underlines.
void drawCores(const Status& s, uint32_t now) {
  fb.fillScreen(V_BG);
  bool stale = isStale(s, now);
  bool linked = s.api && strcmp(s.link, "LINKED") == 0;
  int n = linked ? s.coresAllN : 0;
  char a[24];
  const int X = 5, TOP = 15, BOT = 113, BWID = 6, GAP = 2;
  int per = n ? (n + 15) / 16 : 1, bars = n ? (n + per - 1) / per : 8;
  int cw = bars * (BWID + GAP) - GAP;
  txt(3, 1, "CORES", V_LAB);
  snprintf(a, sizeof a, "%d", n); txt(3 + 34, 1, n ? a : "--", n && !stale ? V_WHITE : V_GREY);
  vCell(2, 12, cw + 6, 105, true);
  if (n) {
    for (int b = 0; b < bars; b++) {
      int lo = b * per, hi = min(n, lo + per), acc = 0;
      for (int i = lo; i < hi; i++) acc += s.coresAll[i];
      float v = acc / (float)(hi - lo) / 100.f;
      int x0 = X + b * (BWID + GAP);
      fb.fillRect(x0, TOP, BWID, BOT - TOP + 1, V_BG);
      int ht = (int)((BOT - TOP) * v + 0.5f);
      for (int y = BOT - ht; y <= BOT; y++) {
        uint16_t c = vHeat((BOT - y) / (float)(BOT - TOP));
        fb.drawFastHLine(x0, y, BWID, stale ? mix(V_BG, c, 110) : c);
      }
      if (ht > 1 && !stale) fb.drawFastHLine(x0, BOT - ht, BWID, V_WHITE);
    }
  }
  const int PX = cw + 10, LX = PX + 4, R = W - 6;
  txt(PX + 2, 1, "PROCESSES", V_LAB);
  vCell(PX, 12, W - 2 - PX, 105, false);
  int pn = linked ? s.procCount : 0;
  if (!pn) txt(LX, 18, linked ? "idle" : "--", V_GREY);
  else {
    int rh = min(15, (BOT - TOP + 2) / pn);
    for (int i = 0; i < pn; i++) {
      int y = TOP + i * rh;
      if (i & 1) fb.fillRect(PX + 1, y - 1, W - 4 - PX, rh - 1, V_ZEBRA);
      float p = s.procP[i];
      snprintf(a, sizeof a, p >= 9.5f || p == 0.f ? "%.0f%%" : "%.1f%%", p);
      int pw = 6 * strlen(a), maxc = (R - LX - pw - 6) / 6;
      char nm[24]; strlcpy(nm, s.procN[i], max(1, min((int)sizeof nm, maxc + 1)));
      txt(LX, y, nm, stale ? V_GREY : V_WHITE);
      uint16_t pc = stale ? V_GREY : vHeat(min(1.f, p / 100.f));
      txtR(R, y, a, pc);
      int barW = (int)((R - LX) * min(1.f, p / 100.f) + 0.5f);
      if (barW > 0 && rh >= 11) fb.drawFastHLine(LX, y + 9, barW, stale ? V_DIMG : mix(V_LAB, pc, 140));
    }
  }
  pageDots(1);
}

// Byte totals: B/K/M/G/T, at most 5 chars.
void fmtBytes(char* b, size_t n, double v) {
  const char* u = "BKMGT"; int i = 0;
  while (v >= 999.5 && i < 4) { v /= 1000.0; i++; }
  if (i && v < 9.95) snprintf(b, n, "%.1f%c", v, u[i]); else snprintf(b, n, "%.0f%c", v, u[i]);
}
// Window since the last reset: "45m", "5h12m", "3d 4h"; "" when the clock or the field is unknown.
void fmtSpan(char* b, size_t n, long long since) {
  time_t t = time(nullptr); b[0] = 0;
  if (since <= 0 || t < 1700000000 || t < since) return;   // NTP not synced yet
  long long d = t - since;
  if (d < 3600) snprintf(b, n, "%lldm", d / 60);
  else if (d < 86400) snprintf(b, n, "%lldh%02lldm", d / 3600, (d % 3600) / 60);
  else snprintf(b, n, "%lldd %lldh", d / 86400, (d % 86400) / 3600);
}
// Cumulative view: host + window on the first row, then each process' down/up totals.
void drawNetHist(const Status& s, bool stale, int X, int R, int TOP, int BOT) {
  uint16_t c = stale ? V_GREY : V_WHITE;
  char span[16], a[12], b[12], hdr[48];
  fmtSpan(span, sizeof span, s.histSince);
  const char* host = s.pcHost[0] ? s.pcHost : s.pcId[0] ? s.pcId : "PC";
  snprintf(hdr, sizeof hdr, "TOTAL %s", span[0] ? "since" : "");
  txt(X, TOP + 2, hdr, V_LAB);
  int hx = X + 6 * (int)strlen(hdr) + 6;
  if (span[0]) { txt(hx, TOP + 2, span, c); hx += 6 * (int)strlen(span) + 6; }
  int maxc = (R - hx) / 6; char hn[24]; strlcpy(hn, host, max(1, min((int)sizeof hn, maxc + 1)));
  if (maxc > 0) txtR(R, TOP + 2, hn, V_LAB);
  fb.drawFastHLine(X, TOP + 11, R - X, V_DIV);
  int n = min(s.histCount, 7), y0 = TOP + 14, rh = min(13, (BOT - y0) / max(1, n));
  const int BX = R - 5 * 6, AX = BX - 10 - 5 * 6;
  for (int i = 0; i < n; i++) {
    int y = y0 + i * rh;
    if (i & 1) fb.fillRect(X - 2, y - 1, R - X + 4, rh - 1, V_ZEBRA);
    fmtBytes(a, sizeof a, s.histRx[i]); fmtBytes(b, sizeof b, s.histTx[i]);
    int mc = (AX - 8 - X) / 6; char nm[16]; strlcpy(nm, s.histN[i], max(1, min((int)sizeof nm, mc + 1)));
    txt(X, y, nm, c);
    fb.drawFastVLine(AX - 5, y, 7, V_RX); fb.drawLine(AX - 7, y + 4, AX - 5, y + 6, V_RX); fb.drawLine(AX - 3, y + 4, AX - 5, y + 6, V_RX);
    txtR(BX - 10, y, a, c);
    fb.drawFastVLine(BX - 5, y, 7, V_TX); fb.drawLine(BX - 7, y + 2, BX - 5, y, V_TX); fb.drawLine(BX - 3, y + 2, BX - 5, y, V_TX);
    txtR(R, y, b, c);
  }
}

// v2 page 3: bandwhich-style network use inside a beveled instrument card.
void drawNet(const Status& s, uint32_t now) {
  fb.fillScreen(V_BG);
  bool stale = isStale(s, now);
  bool linked = s.api && strcmp(s.link, "LINKED") == 0;
  char a[24], b[24];
  const int X = 6, R = W - 6;
  txt(3, 1, "NET", V_LAB);
  vFmtRate(a, sizeof a, linked ? s.pcRx : NAN); vFmtRate(b, sizeof b, linked ? s.pcTx : NAN);
  const uint32_t NET_TITLE_MS = 5000;
  bool showIp = linked && s.netIp[0] && (now / NET_TITLE_MS) % 2;
  char ifn[16]; strlcpy(ifn, showIp ? s.netIp : (linked && s.netIf[0] ? s.netIf : "eth"), sizeof ifn);
  const int TX0 = 26, R_HDR = W - 34;
  auto fit = [&]() { int bw = 6 * strlen(b), aw = 6 * strlen(a); return R_HDR - bw - 12 - aw - 6 >= TX0 + 6 * (int)strlen(ifn) + 4; };
  if (!fit()) for (char* t : {a, b}) { char* q = strstr(t, "/s"); if (q) *q = 0; }
  int bw = 6 * strlen(b), aw = 6 * strlen(a), bx = R_HDR - bw, ax = bx - 12 - aw;
  int maxc = (ax - 8 - TX0) / 6; if ((int)strlen(ifn) > maxc) ifn[max(1, maxc)] = 0;
  txt(TX0, 1, ifn, stale ? V_GREY : V_WHITE);
  fb.drawFastVLine(ax - 5, 1, 7, V_RX); fb.drawLine(ax - 7, 5, ax - 5, 7, V_RX); fb.drawLine(ax - 3, 5, ax - 5, 7, V_RX);
  txt(ax, 1, a, stale ? V_GREY : V_WHITE);
  fb.drawFastVLine(bx - 5, 1, 7, V_TX); fb.drawLine(bx - 7, 3, bx - 5, 1, V_TX); fb.drawLine(bx - 3, 3, bx - 5, 1, V_TX);
  txt(bx, 1, b, stale ? V_GREY : V_WHITE);

  vCell(2, 12, W - 4, 105, false);
  const int TOP = 14, BOT = 115;
  const uint32_t NET_VIEW_MS = 10000;
  if (linked && s.histCount && (now / NET_VIEW_MS) % 2) { drawNetHist(s, stale, X, R, TOP, BOT); pageDots(2); return; }
  int n = linked ? s.netpCount : 0;
  if (!n) txt(X, TOP + 4, linked ? "no active TCP flows" : "--", V_GREY);
  else {
    float mx = 1.f; for (int i = 0; i < n; i++) mx = max(mx, s.netpB[i]);
    int rh = min(16, (BOT - TOP) / n);
    for (int i = 0; i < n; i++) {
      int y = TOP + 1 + i * rh;
      if (i & 1) fb.fillRect(4, y - 1, W - 8, rh - 1, V_ZEBRA);
      vFmtRate(a, sizeof a, s.netpB[i]);
      int mc = (R - X - 6 * (int)strlen(a) - 6) / 6;
      char nm[24]; strlcpy(nm, s.netpN[i], max(1, min((int)sizeof nm, mc + 1)));
      txt(X, y, nm, stale ? V_GREY : V_WHITE);
      txtR(R, y, a, stale ? V_GREY : V_RX);
      int len = (int)((R - X) * s.netpB[i] / mx + 0.5f);
      fb.fillRect(X, y + 9, R - X, 2, V_WELL);
      for (int x = 0; x < len; x++) fb.drawFastVLine(X + x, y + 9, 2, stale ? V_GREY : mix(V_RX, V_TX, 255 * x / max(1, R - X)));
    }
  }
  pageDots(2);
}

// Page control. Short press: next page (and acknowledges an alert). Long press: auto-rotate the
// 4 stats pages every 10 s, on/off. Something going red jumps to the page that shows it and
// returns once it clears; the PC going offline drops to the dimmed ZERO|GEEK page until it's back.
bool gAuto = false;
uint32_t gAutoMs = 0;
int gHome = -1;          // page to return to after an alert/offline jump (-1 = not jumped)
int gAck = -1;           // alert page the user dismissed with a press; stays quiet until it clears
const uint32_t AUTO_MS = 10000, OFFLINE_MS = 10000;
// Shared status bar, drawn on every page (stats, desktop, terminal) so it never changes:
// ZERO + HID state, a colour-coded LINK block in the middle, GEEK + Wi-Fi dBm.
// LINK block: green = polls coming back clean, yellow = recent misses or PC not linked, red = Zero gone.
void statusBar(const Status& s, const PcHist& h, uint32_t now) {
  const int Y = 119;
  fb.fillRect(0, Y, W, H - Y, V_WELL);
  fb.drawFastHLine(0, Y, W, VT().cellHi);
  bool stale = isStale(s, now);
  int bad = 0, win = min(h.n, 30), st = (h.n < HIST) ? 0 : h.next;
  for (int i = h.n - win; i < h.n; i++) if (isnan(h.piLoad[(st + i) % HIST])) bad++;
  bool linked = s.api && strcmp(s.link, "LINKED") == 0;
  uint16_t lc = !s.api || stale || bad >= 3 ? V_BAD : bad || !linked ? V_WARN : V_OK;
  const int ty = Y + 4;
  // ZERO (left)
  vDot(6, ty + 3, hidC(s)); txt(11, ty, "ZERO", V_LAB); txt(11 + 5 * CW, ty, s.api ? "HID" : "--", hidC(s));
  // LINK pill (centre)
  const int BW = 36, BH = 10;
  fb.fillRoundRect((W - BW) / 2, ty - 1, BW, BH, 2, lc);
  txt((W - 4 * CW) / 2, ty, linked ? "LINK" : (s.host ? "KEYS" : "IDLE"), V_WELL);
  if (gAuto) { fb.drawCircle((W + BW) / 2 + 6, ty + 3, 2, V_LAB); fb.drawPixel((W + BW) / 2 + 6, ty + 3, V_LAB); }
  // GEEK (right): "GEEK -52dBm"
  char rs[12]; if (s.wifi && s.rssi) snprintf(rs, sizeof rs, "%ddBm", s.rssi); else strcpy(rs, "--");
  uint16_t gc = rssiC(s.wifi, s.rssi);
  int rw = strlen(rs) * CW, gx = W - 4 - rw - 5 * CW - 7;
  vDot(gx + 2, ty + 3, gc); txt(gx + 7, ty, "GEEK", V_LAB); txtR(W - 4, ty, rs, gc);
}

enum Page { P_PC, P_CORES, P_NET, P_ZG, P_DESK, P_TERM, NPAGES };
int page = P_PC;   // boot into the v2 PC page

void drawWaiting() {
  fb.fillScreen(V_BG);
  vCell(14, 14, W - 28, 96, false);
  ftS(58, 56, "DESKTOP", V_LAB, &FreeSansBold12pt7b);
  if (gPinBad) txt(46, 76, "bad PIN (check portal)", V_BAD);
  else txt(58, 76, "waiting for still...", V_GREY);
}

void drawTerm() {
  Term t; xSemaphoreTake(gLock, portMAX_DELAY); t = gTerm; xSemaphoreGive(gLock);
  fb.fillScreen(V_BG);
  vCell(1, 1, W - 2, 117, true);
  uint16_t fg = V_RX;
  if (gPinBad) { txt(6, 6, "bad PIN: set it in setup portal", V_BAD); txt(6, 18, "(retrying in a few minutes)", V_GREY); return; }
  if (t.stampMs == 0) txt(6, 6, "loading serial terminal...", V_GREY);
  else if (t.code != 200) {
    const char* msg = t.code == 401 ? "wrong PIN (setup portal)" : t.code == 429 ? "PIN locked out"
                    : t.code <= 0 ? "Zero unreachable" : nullptr;
    if (msg) txt(6, 6, msg, V_BAD);
    else { char b[24]; snprintf(b, sizeof b, "HTTP %d", t.code); txt(6, 6, b, V_BAD); }
  } else if (t.n == 0) txt(6, 6, t.open ? "(serial idle)" : "serial port closed", V_GREY);
  for (int i = 0; i < t.n; i++) txt(4, 3 + i * 8, t.row[i], fg);
  if (t.code == 200) fb.fillCircle(W - 6, 6, 2, t.open ? V_OK : V_WARN);
}

void drawOta() {
  fb.fillScreen(V_BG);
  vCell(18, 22, W - 36, 90, false);
  txt(26, 28, "OTA FIRMWARE UPDATE", V_LAB);
  char t[16]; snprintf(t, sizeof t, "%d%%", (int)gOtaPct);
  ftS(26, 66, t, V_WHITE, &FreeSansBold18pt7b);
  int bw = W - 52;
  vCell(26, 76, bw, 12, true);
  int fw = (bw - 2) * max(0, min(100, (int)gOtaPct)) / 100;
  if (fw > 0) fb.fillRect(27, 77, fw, 10, V_OK);
  txt(26, 94, "do not power off", V_GREY);
}

int alertPage(const Status& s, uint32_t now, bool* offline) {
  static uint32_t unlinkedSince = 0;
  bool linked = s.api && !isStale(s, now) && strcmp(s.link, "LINKED") == 0;
  if (linked) unlinkedSince = 0; else if (!unlinkedSince) unlinkedSince = now;
  *offline = unlinkedSince && now - unlinkedSince >= OFFLINE_MS && s.polls > 2;
  if (*offline || (s.api && !s.hid)) return P_ZG;
  if (!linked) return -1;
  float t = !isnan(s.pcCpuT) ? s.pcCpuT : s.pcTemp;
  if ((!isnan(t) && t >= 90.f) || (!isnan(s.pcGpuT) && s.pcGpuT >= 90.f) || (!isnan(s.pcNvmeT) && s.pcNvmeT >= 90.f)
      || s.pcMem >= 97 || s.pcDisk >= 99) return P_PC;
  if (!isnan(s.pcTopCpu) && s.pcTopCpu >= 95.f) return P_CORES;
  return -1;
}
// Desktop / terminal pages follow the Zero's active mode ("mode" in /api/status).
// No field (older Zero) or no good poll yet -> both stay visible, as before.
bool pageVisible(int p, const Status& s) {
  if (p != P_DESK && p != P_TERM) return true;
  if (!s.api || !s.mode[0]) return true;
  return strcmp(s.mode, p == P_DESK ? "desktop" : "term") == 0;
}
int nextVisible(int from, const Status& s) {   // first visible page after `from` (P_PC is always visible)
  for (int i = 1; i <= NPAGES; i++) { int p = ((from + i) % NPAGES + NPAGES) % NPAGES; if (pageVisible(p, s)) return p; }
  return P_PC;
}
void go(int n);
void pageLogic(const Status& s, uint32_t now, bool* offline) {
  int a = alertPage(s, now, offline);
  if (a < 0) gAck = -1;
  if (a >= 0 && a != gAck && page != P_DESK && page != P_TERM) {
    if (gHome < 0) gHome = page;
    if (page != a) go(a);
    return;
  }
  if (a < 0 && gHome >= 0) { int h = gHome; gHome = -1; go(h); gAutoMs = now; return; }
  if (gAuto && gHome < 0 && page <= P_ZG && now - gAutoMs >= AUTO_MS) { gAutoMs = now; go((page + 1) % (P_ZG + 1)); }
}
void paint() {
  fb.setTextWrap(false);
  if (gOtaPct >= 0) { drawOta(); tft.drawRGBBitmap(0, 0, fb.getBuffer(), W, H); return; }
  static Status s; static PcHist h;   // static: PcHist is ~9 KB, keep it off the loop stack
  xSemaphoreTake(gLock, portMAX_DELAY); s = gStat; h = gHist; xSemaphoreGive(gLock);
  if (s.api) gUiTheme = s.uiTheme;
  if (!pageVisible(page, s)) go(nextVisible(page, s));   // mode changed under us: move on
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
    statusBar(s, h, millis());
    tft.drawRGBBitmap(0, 0, fb.getBuffer(), W, H);
    return;
  }
  // Stale dims the backlight (PWM) and recovers on the next good poll.
  bool offline = false; pageLogic(s, millis(), &offline);
  static int bl = -1; int want = (isStale(s, millis()) && s.polls > 2) || (offline && page == P_ZG) ? BL_DIM : BL_FULL;
  if (want != bl) { analogWrite(LCD_BL, want); bl = want; }
  fb.fillScreen(C_BG);
  if (page == P_TERM) drawTerm();
  else if (page == P_ZG) drawZg(s, h, millis());
  else if (page == P_CORES) drawCores(s, millis());
  else if (page == P_NET) drawNet(s, millis());
  else drawPc(s, h, millis());
  statusBar(s, h, millis());
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
  Serial.printf("fb %dx%d in %s; free heap %u, largest block %u, psram %u\n", W, H,
                esp_ptr_external_ram(fb.getBuffer()) ? "PSRAM" : "internal RAM",
                (unsigned)ESP.getFreeHeap(), (unsigned)ESP.getMaxAllocHeap(), (unsigned)ESP.getPsramSize());
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
  if (cfg.ota.length() >= 8) xTaskCreatePinnedToCore(otaTask, "ota", 8192, nullptr, 1, nullptr, 0);
  else Serial.println("OTA off: set an OTA password in the setup portal (hold BOOT at power-on)");
}

void loop() {
  uint32_t now = millis();
  Btn* btns[] = {&bBack, &bUp, &bDown, &bSel};
  for (Btn* b : btns) {
    Ev e = b->poll(now);
    if (e == EV_SHORT) {
      if (gHome >= 0) { gAck = page; int h = gHome; gHome = -1; go(h); }   // dismiss the alert jump
      else { static Status st; xSemaphoreTake(gLock, portMAX_DELAY); st = gStat; xSemaphoreGive(gLock); go(nextVisible(page, st)); }
      gAutoMs = now;
    } else if (e == EV_LONG) { gAuto = !gAuto; gAutoMs = now; }
  }
  static uint32_t lastPaint = 0;
  if (now - lastPaint >= 200) { lastPaint = now; paint(); }
  delay(2);
}
