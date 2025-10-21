// Macro Button firmware for ESP32-S3 (USB HID)
// Mirrors Arduino Pro Micro behavior and PC protocol.
// Long press offline sends Wake-on-LAN magic packet.
// Taps (1..4): single/double/triple/quadruple
//
// Pins (adjust to your board):
//   - Button: GPIO 7 (active low)
//   - LED:    GPIO 10
// You may change these at the top section below.

//#define CLEAR_EEPROM  // Uncomment to reset EEPROM to defaults on boot

#include <Arduino.h>
#include <USB.h>
#include <USBHIDKeyboard.h>
#include <EEPROM.h>
#include <OneButton.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <string.h>

// ---------------- Pins (adjust as needed) ----------------
const uint8_t PIN_LED = 10;
const uint8_t PIN_BUTTON = 7;

// ---------------- Timing ----------------
const unsigned long LONG_PRESS_MS = 1000;     // for programming / WoL
const unsigned long CLICK_TICKS_MS = 400;     // max interval between multi-clicks

// ---------------- Macro constraints ----------------
const uint8_t MAX_COMBOS = 20;      // maximum number of key combinations per macro
const uint8_t MAX_ACTIONS = 60;     // maximum total actions (keys + delays)
const uint16_t DEFAULT_DELAY_MS = 10; // default delay between combos when no explicit delay present

// ---------------- Modifiers bitmask ----------------
const uint8_t MOD_SHIFT = 0x01;
const uint8_t MOD_CTRL  = 0x02;
const uint8_t MOD_ALT   = 0x04;
const uint8_t MOD_GUI   = 0x08;

// ---------------- Modes ----------------
const uint8_t MODE_MACRO = 1;
const uint8_t MODE_APP = 2;

// ---------------- EEPROM layout (signature + 4 tap blocks) ----------------
const uint8_t EEPROM_VERSION = 0x01;
const uint16_t TAP_BLOCK_SIZE = 192;
const uint16_t EEPROM_SIZE = 4 + TAP_BLOCK_SIZE * 4;

// Action types
const uint8_t ACT_DELAY = 0;
const uint8_t ACT_KEY   = 1;

struct TapHeader {
  uint8_t mode;            // 1=macro, 2=app
  uint8_t appCode;         // 1..4 (Q1..Q4)
  uint16_t defaultDelayMs; // default delay between key combos
  uint8_t actionsCount;    // total actions (keys+delays)
  uint8_t combosCount;     // number of key combos
};

// ---------------- State ----------------
OneButton button(PIN_BUTTON, true /* activeLow */);
USBHIDKeyboard Keyboard;
WiFiUDP Udp;
bool pcConnected = false;
bool programmingMode = false;

// -------------------- Wake-on-LAN (WOL) settings --------------------
// Libraries required: USB, USBHIDKeyboard, EEPROM, OneButton, WiFi, WiFiUdp
#define WOL_ENABLED 1
const char* WIFI_SSID = "YourSSID";      // TODO: set SSID
const char* WIFI_PASS = "YourPassword";  // TODO: set password
const uint8_t WOL_TARGET_MAC[6] = { 0xAA, 0xBB, 0xCC, 0x11, 0x22, 0x33 }; // PC NIC MAC to wake
const char* WOL_BROADCAST_IP = "255.255.255.255"; // or your subnet broadcast, e.g., "192.168.1.255"
const uint16_t WOL_PORT = 9; // common: 9 or 7

// ---------------- Serial helpers ----------------
void sendLine(const String &s) { Serial.println(s); }

// ---------------- EEPROM helpers ----------------
uint16_t eepromOffsetForTap(uint8_t tap) { return 4 + (tap - 1) * TAP_BLOCK_SIZE; }

void eepromWriteU16(int addr, uint16_t v) { EEPROM.write(addr, v & 0xFF); EEPROM.write(addr + 1, (v >> 8) & 0xFF); }
uint16_t eepromReadU16(int addr) { return (EEPROM.read(addr + 1) << 8) | EEPROM.read(addr); }

void eepromWriteHeader(uint8_t tap, const TapHeader &h) {
  int base = eepromOffsetForTap(tap);
  EEPROM.write(base + 0, h.mode);
  EEPROM.write(base + 1, h.appCode);
  eepromWriteU16(base + 2, h.defaultDelayMs);
  EEPROM.write(base + 4, h.actionsCount);
  EEPROM.write(base + 5, h.combosCount);
}

TapHeader eepromReadHeader(uint8_t tap) {
  TapHeader h;
  int base = eepromOffsetForTap(tap);
  h.mode = EEPROM.read(base + 0);
  h.appCode = EEPROM.read(base + 1);
  h.defaultDelayMs = eepromReadU16(base + 2);
  h.actionsCount = EEPROM.read(base + 4);
  h.combosCount = EEPROM.read(base + 5);
  return h;
}

void eepromWriteAction(uint8_t tap, uint8_t index, uint8_t type, uint8_t a, uint8_t b) {
  int base = eepromOffsetForTap(tap) + 6;
  int p = base;
  for (uint8_t i = 0; i < index; i++) {
    uint8_t t = EEPROM.read(p); p++;
    if (t == ACT_DELAY) { p += 2; }
    else if (t == ACT_KEY) { p += 2; }
  }
  EEPROM.write(p++, type);
  if (type == ACT_DELAY) { EEPROM.write(p++, a); EEPROM.write(p++, b); }
  else if (type == ACT_KEY) { EEPROM.write(p++, a); EEPROM.write(p++, b); }
}

void eepromReadAction(uint8_t tap, uint8_t index, uint8_t &type, uint8_t &a, uint8_t &b) {
  int base = eepromOffsetForTap(tap) + 6;
  int p = base;
  for (uint8_t i = 0; i < index; i++) {
    uint8_t t = EEPROM.read(p); p++;
    if (t == ACT_DELAY) { p += 2; }
    else if (t == ACT_KEY) { p += 2; }
  }
  type = EEPROM.read(p++);
  if (type == ACT_DELAY) { a = EEPROM.read(p++); b = EEPROM.read(p++); }
  else if (type == ACT_KEY) { a = EEPROM.read(p++); b = EEPROM.read(p++); }
}

// ---------------- HID helpers ----------------
void pressModifiers(uint8_t mods) {
  if (mods & MOD_SHIFT) Keyboard.press(KEY_LEFT_SHIFT);
  if (mods & MOD_CTRL)  Keyboard.press(KEY_LEFT_CTRL);
  if (mods & MOD_ALT)   Keyboard.press(KEY_LEFT_ALT);
  if (mods & MOD_GUI)   Keyboard.press(KEY_LEFT_GUI);
}

void releaseAll() { Keyboard.releaseAll(); }

void pressKeyToken(uint8_t token) {
  if (token >= 1 && token <= 26) { char c = 'a' + (token - 1); Keyboard.press(c); return; }
  if (token >= 27 && token <= 36) { char c = '0' + (token - 27); Keyboard.press(c); return; }
  if (token >= 64 && token <= 75) { uint8_t idx = token - 64; switch (idx) {
      case 0: Keyboard.press(KEY_F1); break; case 1: Keyboard.press(KEY_F2); break; case 2: Keyboard.press(KEY_F3); break; case 3: Keyboard.press(KEY_F4); break; case 4: Keyboard.press(KEY_F5); break; case 5: Keyboard.press(KEY_F6); break; case 6: Keyboard.press(KEY_F7); break; case 7: Keyboard.press(KEY_F8); break; case 8: Keyboard.press(KEY_F9); break; case 9: Keyboard.press(KEY_F10); break; case 10: Keyboard.press(KEY_F11); break; case 11: Keyboard.press(KEY_F12); break; }
    return; }
  switch (token) {
    case 80: Keyboard.press(KEY_ESC); break;
    case 81: Keyboard.press(KEY_TAB); break;
    case 82: Keyboard.press(KEY_RETURN); break;
    case 83: Keyboard.press(' '); break; // SPACE
    case 84: Keyboard.press(KEY_HOME); break;
    case 85: Keyboard.press(KEY_END); break;
    case 86: Keyboard.press(KEY_PAGE_UP); break;
    case 87: Keyboard.press(KEY_PAGE_DOWN); break;
    case 88: Keyboard.press(KEY_LEFT_ARROW); break;
    case 89: Keyboard.press(KEY_RIGHT_ARROW); break;
    case 90: Keyboard.press(KEY_UP_ARROW); break;
    case 91: Keyboard.press(KEY_DOWN_ARROW); break;
    case 92: Keyboard.press(KEY_BACKSPACE); break;
    case 93: Keyboard.press(KEY_DELETE); break;
  }
}

uint8_t tokenFromKeyString(const String &s) {
  if (s.length() == 1) {
    char c = s[0];
    if (c >= 'A' && c <= 'Z') return 1 + (c - 'A');
    if (c >= '0' && c <= '9') return 27 + (c - '0');
  }
  if (s[0] == 'F' && s.length() >= 2) { int n = s.substring(1).toInt(); if (n >= 1 && n <= 12) return 64 + (n - 1); }
  if (s == "ESC") return 80; if (s == "TAB") return 81; if (s == "ENTER") return 82; if (s == "SPACE") return 83;
  if (s == "HOME") return 84; if (s == "END") return 85; if (s == "PAGEUP") return 86; if (s == "PAGEDOWN") return 87;
  if (s == "LEFT") return 88; if (s == "RIGHT") return 89; if (s == "UP") return 90; if (s == "DOWN") return 91; if (s == "BACKSPACE") return 92; if (s == "DELETE") return 93;
  return 0;
}

// ---------------- Macro exec ----------------
void executeMacro(uint8_t tap) {
  TapHeader h = eepromReadHeader(tap);
  uint8_t actions = h.actionsCount;
  for (uint8_t i = 0; i < actions; i++) {
    uint8_t type, a, b; eepromReadAction(tap, i, type, a, b);
    if (type == ACT_DELAY) { uint16_t ms = (uint16_t)b << 8 | a; delay(ms); }
    else if (type == ACT_KEY) {
      pressModifiers(a); pressKeyToken(b); delay(5); releaseAll();
      if (i + 1 < actions) { uint8_t nt, na, nb; eepromReadAction(tap, i + 1, nt, na, nb); if (nt != ACT_DELAY) delay(h.defaultDelayMs); }
      else { delay(h.defaultDelayMs); }
    }
  }
  for (uint8_t i = 0; i < tap; i++) { digitalWrite(PIN_LED, HIGH); delay(100); digitalWrite(PIN_LED, LOW); delay(150); }
}

// ---------------- Defaults ----------------
void setDefaults() {
  EEPROM.write(0, 'M'); EEPROM.write(1, 'K'); EEPROM.write(2, 'B'); EEPROM.write(3, EEPROM_VERSION);
  TapHeader h1 = {MODE_MACRO, 1, DEFAULT_DELAY_MS, 1, 1}; eepromWriteHeader(1, h1); eepromWriteAction(1, 0, ACT_KEY, MOD_GUI, tokenFromKeyString("E"));
  TapHeader h2 = {MODE_MACRO, 2, DEFAULT_DELAY_MS, 1, 1}; eepromWriteHeader(2, h2); eepromWriteAction(2, 0, ACT_KEY, MOD_GUI, tokenFromKeyString("R"));
  TapHeader h3 = {MODE_MACRO, 3, DEFAULT_DELAY_MS, 1, 1}; eepromWriteHeader(3, h3); eepromWriteAction(3, 0, ACT_KEY, (MOD_CTRL | MOD_SHIFT), tokenFromKeyString("ESC"));
  TapHeader h4 = {MODE_MACRO, 4, DEFAULT_DELAY_MS, 1, 1}; eepromWriteHeader(4, h4); eepromWriteAction(4, 0, ACT_KEY, MOD_GUI, tokenFromKeyString("D"));
}

bool checkSignature() { return EEPROM.read(0) == 'M' && EEPROM.read(1) == 'K' && EEPROM.read(2) == 'B' && EEPROM.read(3) == EEPROM_VERSION; }

// ---------------- Protocol ----------------
void handleLine(const String &l) {
  if (l == "HELLO_PC") { sendLine("HELLO_ARDUINO"); pcConnected = true; return; }
  if (l == "HELLO_ACK") { pcConnected = true; return; }
  if (l.startsWith("SET_MODE:")) { int i1 = l.indexOf(':'), i2 = l.indexOf(':', i1 + 1); uint8_t tap = (uint8_t) l.substring(i1 + 1, i2).toInt(); uint8_t mode = (uint8_t) l.substring(i2 + 1).toInt(); TapHeader h = eepromReadHeader(tap); h.mode = mode; eepromWriteHeader(tap, h); sendLine(String("MODE:") + tap + ":" + mode); return; }
  if (l.startsWith("GET_MODE:")) { uint8_t tap = (uint8_t) l.substring(9).toInt(); TapHeader h = eepromReadHeader(tap); sendLine(String("MODE:") + tap + ":" + h.mode); return; }
  if (l.startsWith("READ_MACRO:")) { uint8_t tap = (uint8_t) l.substring(11).toInt(); TapHeader h = eepromReadHeader(tap); sendLine(String("MACRO_BEGIN:") + tap); for (uint8_t i = 0; i < h.actionsCount; i++) { uint8_t type, a, b; eepromReadAction(tap, i, type, a, b); if (type == ACT_DELAY) { uint16_t ms = (uint16_t)b << 8 | a; sendLine(String("A:D:") + ms); } else if (type == ACT_KEY) { String keyStr; if (b >= 1 && b <= 26) keyStr = String(char('A' + (b - 1))); else if (b >= 27 && b <= 36) keyStr = String(char('0' + (b - 27))); else if (b >= 64 && b <= 75) keyStr = String("F") + (b - 63); else { switch (b) { case 80: keyStr = "ESC"; break; case 81: keyStr = "TAB"; break; case 82: keyStr = "ENTER"; break; case 83: keyStr = "SPACE"; break; case 84: keyStr = "HOME"; break; case 85: keyStr = "END"; break; case 86: keyStr = "PAGEUP"; break; case 87: keyStr = "PAGEDOWN"; break; case 88: keyStr = "LEFT"; break; case 89: keyStr = "RIGHT"; break; case 90: keyStr = "UP"; break; case 91: keyStr = "DOWN"; break; case 92: keyStr = "BACKSPACE"; break; case 93: keyStr = "DELETE"; break; default: keyStr = ""; break; } } sendLine(String("A:K:") + a + ":" + keyStr); } } sendLine(String("MACRO_END:") + tap); return; }
  if (l.startsWith("WRITE_MACRO_BEGIN:")) { uint8_t tap = (uint8_t) l.substring(18).toInt(); uint8_t actions = 0, combos = 0; int p = eepromOffsetForTap(tap) + 6; unsigned long startMs = millis(); while (true) { if (millis() - startMs > 3000) { sendLine("ERR:TIMEOUT"); return; } if (!Serial.available()) { delay(5); continue; } String x = Serial.readStringUntil('\n'); x.trim(); if (x.length() == 0) continue; if (x == "WRITE_MACRO_END") break; if (!x.startsWith("A:")) continue; if (x.charAt(2) == 'K') { int p1 = x.indexOf(':', 4); if (p1 < 0) continue; uint8_t mods = (uint8_t) x.substring(4, p1).toInt(); uint8_t token = tokenFromKeyString(x.substring(p1 + 1)); EEPROM.write(p++, ACT_KEY); EEPROM.write(p++, mods); EEPROM.write(p++, token); actions++; combos++; } else if (x.charAt(2) == 'D') { uint16_t ms = (uint16_t) x.substring(4).toInt(); EEPROM.write(p++, ACT_DELAY); EEPROM.write(p++, ms & 0xFF); EEPROM.write(p++, (ms >> 8) & 0xFF); actions++; } } TapHeader h = eepromReadHeader(tap); h.actionsCount = actions; h.combosCount = combos; eepromWriteHeader(tap, h); sendLine("OK"); return; }
  if (l.startsWith("SET_APP_CODE:")) { int i1 = l.indexOf(':'), i2 = l.indexOf(':', i1 + 1); uint8_t tap = (uint8_t) l.substring(i1 + 1, i2).toInt(); String code = l.substring(i2 + 1); TapHeader h = eepromReadHeader(tap); uint8_t app = 0; if (code == "Q1") app = 1; else if (code == "Q2") app = 2; else if (code == "Q3") app = 3; else if (code == "Q4") app = 4; h.appCode = app; eepromWriteHeader(tap, h); sendLine("OK"); return; }
  if (l == "PROG_ACK") { programmingMode = true; digitalWrite(PIN_LED, HIGH); return; }
  if (l == "PROG_EXIT_ACK") { programmingMode = false; digitalWrite(PIN_LED, LOW); return; }
}

// ---------------- Button handling ----------------
void onLongPressStart() {
  // If PC is offline, long press sends WoL
  if (!pcConnected) {
#if WOL_ENABLED
    if (WiFi.status() != WL_CONNECTED) {
      WiFi.mode(WIFI_STA);
      WiFi.persistent(false);
      WiFi.setAutoReconnect(false);
      unsigned long start = millis();
      WiFi.begin(WIFI_SSID, WIFI_PASS);
      while (WiFi.status() != WL_CONNECTED && (millis() - start) < 4000) { delay(50); }
      if (WiFi.status() == WL_CONNECTED) { Udp.begin(12345); }
    }
    // Build magic packet
    uint8_t packet[102]; for (int i = 0; i < 6; i++) packet[i] = 0xFF; for (int i = 0; i < 16; i++) { memcpy(packet + 6 + i * 6, (const void*)WOL_TARGET_MAC, 6); }
    IPAddress bcast; bcast.fromString(WOL_BROADCAST_IP);
    if (Udp.beginPacket(bcast, WOL_PORT)) { size_t n = Udp.write(packet, sizeof(packet)); bool ok = Udp.endPacket(); sendLine(ok && (n == sizeof(packet)) ? "WOL:OK" : "WOL:ERR_SEND"); }
    else { sendLine("WOL:ERR_WIFI"); }
    for (int i = 0; i < 2; ++i) { digitalWrite(PIN_LED, HIGH); delay(100); digitalWrite(PIN_LED, LOW); delay(100); }
    return;
#endif
  }
  // Otherwise request programming mode from PC
  sendLine("PROG_REQ");
}

void onMultiClick() {
  uint8_t clicks = button.getNumberClicks(); if (clicks == 0) return;
  if (programmingMode) { sendLine(String("PROG_EXIT_TAPS:") + clicks); programmingMode = false; digitalWrite(PIN_LED, LOW); return; }
  uint8_t tap = clicks > 4 ? 4 : clicks;
  TapHeader h = eepromReadHeader(tap);
  if (pcConnected) { if (h.mode == MODE_APP) { String code = (h.appCode == 1) ? "Q1" : (h.appCode == 2) ? "Q2" : (h.appCode == 3) ? "Q3" : (h.appCode == 4) ? "Q4" : "Q1"; sendLine(String("APP_TRIGGER:") + tap + ":" + code); sendLine(String("TAP:") + tap); return; } executeMacro(tap); sendLine(String("TAP:") + tap); return; }
  // No PC connection: always execute stored macro regardless of mode
  executeMacro(tap); sendLine(String("TAP:") + tap);
}

// ---------------- Setup & Loop ----------------
void setup() {
  pinMode(PIN_LED, OUTPUT); digitalWrite(PIN_LED, LOW);
  pinMode(PIN_BUTTON, INPUT_PULLUP);
  Serial.begin(115200);
  USB.begin(); Keyboard.begin();
  // OneButton 2.x: prefer setClickMs/setPressMs
  button.setClickMs(CLICK_TICKS_MS);
  button.setPressMs(LONG_PRESS_MS);
  button.attachLongPressStart(onLongPressStart); button.attachMultiClick(onMultiClick);
  EEPROM.begin(EEPROM_SIZE);
#ifdef CLEAR_EEPROM
  setDefaults();
#else
  if (!checkSignature()) setDefaults();
#endif
  sendLine("HELLO_ARDUINO");
}

void loop() {
  button.tick();
  if (!Serial) { pcConnected = false; }
  while (Serial.available()) { String l = Serial.readStringUntil('\n'); l.trim(); if (l.length() == 0) continue; handleLine(l); }
}
