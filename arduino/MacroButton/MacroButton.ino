// Macro Button firmware for Arduino Pro Micro
// Supports single, double, triple tap and long press (>1s)
// EEPROM-backed macros per tap, serial protocol with PC, LED on pin 4, button on pins 7 and 6 (pin 6 acts as GND)
// Uses OneButton library for debounce and multi-click
// Emulates keyboard macros via Keyboard.h

// ---------------- Configuration ----------------
//#define CLEAR_EEPROM  // Uncomment to reset EEPROM to defaults on boot

#include <Arduino.h>
#include <Keyboard.h>
#include <EEPROM.h>
#include <OneButton.h>

// Pins
const uint8_t PIN_LED = 4;
const uint8_t PIN_BUTTON = 7;
const uint8_t PIN_BUTTON_GND = 6; // driven LOW

// Timing
const unsigned long LONG_PRESS_MS = 1000;
const unsigned long CLICK_TICKS_MS = 400; // max interval between clicks

// Macro constraints
const uint8_t MAX_COMBOS = 20;      // maximum number of key combinations per macro
const uint8_t MAX_ACTIONS = 60;     // maximum total actions (keys + delays)
const uint16_t DEFAULT_DELAY_MS = 10; // default delay between combos when no explicit delay present

// Modifiers bitmask
const uint8_t MOD_SHIFT = 0x01;
const uint8_t MOD_CTRL  = 0x02;
const uint8_t MOD_ALT   = 0x04;
const uint8_t MOD_GUI   = 0x08;

// Modes
const uint8_t MODE_MACRO = 1;
const uint8_t MODE_APP = 2;

// EEPROM layout
// [0..2] signature 'MKB'
// [3]    version 0x01
// For each tap (1..4): block of 192 bytes
//   [0] mode
//   [1] appCode (1..6 map to F1..F3,Q1..Q3) - currently using Q1..Q3
//   [2..3] defaultDelayMs (uint16)
//   [4] actionsCount
//   [5] combosCount
//   [6..] actions encoded as: type(1) + payload
//         type=0 (delay): uint16 ms
//         type=1 (key): uint8 mods + uint8 keyToken

const uint16_t EEPROM_BASE = 0;
const uint8_t EEPROM_VERSION = 0x01;
const uint16_t TAP_BLOCK_SIZE = 192;

// Action types
const uint8_t ACT_DELAY = 0;
const uint8_t ACT_KEY   = 1;

struct TapHeader {
  uint8_t mode;
  uint8_t appCode; // 1..3 -> Q1..Q3 (reserved 4..6 for F1..F3)
  uint16_t defaultDelayMs;
  uint8_t actionsCount;
  uint8_t combosCount;
};

// Runtime state
bool pcConnected = false;
bool programmingMode = false;
uint8_t lastClicks = 0;

OneButton button(PIN_BUTTON, true /* activeLow */);

// ---------------- Utilities ----------------

uint16_t eepromOffsetForTap(uint8_t tap) {
  // tap: 1..3
  return 4 + (tap - 1) * TAP_BLOCK_SIZE;
}

void eepromWriteU16(int addr, uint16_t v) {
  EEPROM.update(addr, v & 0xFF);
  EEPROM.update(addr + 1, (v >> 8) & 0xFF);
}

uint16_t eepromReadU16(int addr) {
  uint16_t lo = EEPROM.read(addr);
  uint16_t hi = EEPROM.read(addr + 1);
  return (hi << 8) | lo;
}

void eepromWriteHeader(uint8_t tap, const TapHeader &h) {
  int base = eepromOffsetForTap(tap);
  EEPROM.update(base + 0, h.mode);
  EEPROM.update(base + 1, h.appCode);
  eepromWriteU16(base + 2, h.defaultDelayMs);
  EEPROM.update(base + 4, h.actionsCount);
  EEPROM.update(base + 5, h.combosCount);
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
  // iterate to correct offset by scanning previous actions
  for (uint8_t i = 0; i < index; i++) {
    uint8_t t = EEPROM.read(p); p++;
    if (t == ACT_DELAY) { p += 2; }
    else if (t == ACT_KEY) { p += 2; }
  }
  EEPROM.update(p++, type);
  if (type == ACT_DELAY) {
    EEPROM.update(p++, a); // low byte ms
    EEPROM.update(p++, b); // high byte ms
  } else if (type == ACT_KEY) {
    EEPROM.update(p++, a); // mods
    EEPROM.update(p++, b); // key token
  }
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
  if (type == ACT_DELAY) {
    a = EEPROM.read(p++); // ms lo
    b = EEPROM.read(p++); // ms hi
  } else if (type == ACT_KEY) {
    a = EEPROM.read(p++); // mods
    b = EEPROM.read(p++); // key token
  }
}

// Key token to Keyboard press
void pressModifiers(uint8_t mods) {
  if (mods & MOD_SHIFT) Keyboard.press(KEY_LEFT_SHIFT);
  if (mods & MOD_CTRL)  Keyboard.press(KEY_LEFT_CTRL);
  if (mods & MOD_ALT)   Keyboard.press(KEY_LEFT_ALT);
  if (mods & MOD_GUI)   Keyboard.press(KEY_LEFT_GUI);
}

void releaseAll() {
  Keyboard.releaseAll();
}

void pressKeyToken(uint8_t token) {
  // Letters A..Z -> tokens 1..26
  if (token >= 1 && token <= 26) {
    char c = 'a' + (token - 1);
    Keyboard.press(c);
    return;
  }
  // Digits 0..9 -> tokens 27..36
  if (token >= 27 && token <= 36) {
    char c = '0' + (token - 27);
    Keyboard.press(c);
    return;
  }
  // F1..F12 -> tokens 64..75
  if (token >= 64 && token <= 75) {
    uint8_t idx = token - 64; // 0..11
    switch (idx) {
      case 0: Keyboard.press(KEY_F1); break;
      case 1: Keyboard.press(KEY_F2); break;
      case 2: Keyboard.press(KEY_F3); break;
      case 3: Keyboard.press(KEY_F4); break;
      case 4: Keyboard.press(KEY_F5); break;
      case 5: Keyboard.press(KEY_F6); break;
      case 6: Keyboard.press(KEY_F7); break;
      case 7: Keyboard.press(KEY_F8); break;
      case 8: Keyboard.press(KEY_F9); break;
      case 9: Keyboard.press(KEY_F10); break;
      case 10: Keyboard.press(KEY_F11); break;
      case 11: Keyboard.press(KEY_F12); break;
    }
    return;
  }
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
  if (s[0] == 'F' && s.length() >= 2) {
    int n = s.substring(1).toInt();
    if (n >= 1 && n <= 12) return 64 + (n - 1);
  }
  if (s == "ESC") return 80;
  if (s == "TAB") return 81;
  if (s == "ENTER") return 82;
  if (s == "SPACE") return 83;
  if (s == "HOME") return 84;
  if (s == "END") return 85;
  if (s == "PAGEUP") return 86;
  if (s == "PAGEDOWN") return 87;
  if (s == "LEFT") return 88;
  if (s == "RIGHT") return 89;
  if (s == "UP") return 90;
  if (s == "DOWN") return 91;
  if (s == "BACKSPACE") return 92;
  if (s == "DELETE") return 93;
  return 0; // unknown
}

// Execute macro for a tap
void executeMacro(uint8_t tap) {
  TapHeader h = eepromReadHeader(tap);
  uint8_t actions = h.actionsCount;
  uint8_t combosSeen = 0;
  for (uint8_t i = 0; i < actions; i++) {
    uint8_t type, a, b;
    eepromReadAction(tap, i, type, a, b);
    if (type == ACT_DELAY) {
      uint16_t ms = (uint16_t)b << 8 | a;
      delay(ms);
    } else if (type == ACT_KEY) {
      pressModifiers(a);
      pressKeyToken(b);
      delay(5);
      releaseAll();
      combosSeen++;
      // insert default delay after key if no explicit delay follows
      // peek next action type; if not delay, add default
      if (i + 1 < actions) {
        uint8_t nt, na, nb;
        eepromReadAction(tap, i + 1, nt, na, nb);
        if (nt != ACT_DELAY) {
          delay(h.defaultDelayMs);
        }
      } else {
        delay(h.defaultDelayMs);
      }
    }
  }
  // Blink LED number of taps
  for (uint8_t i = 0; i < tap; i++) {
    digitalWrite(PIN_LED, HIGH);
    delay(100);
    digitalWrite(PIN_LED, LOW);
    delay(150);
  }
}

void setDefaults() {
  // Signature and version
  EEPROM.update(0, 'M');
  EEPROM.update(1, 'K');
  EEPROM.update(2, 'B');
  EEPROM.update(3, EEPROM_VERSION);
  // Defaults: Macro mode, default delay, and macros
  // Tap 1: Win+E
  TapHeader h1 = {MODE_MACRO, 1, DEFAULT_DELAY_MS, 1, 1};
  eepromWriteHeader(1, h1);
  // action 0: mods=MOD_GUI, key='E'
  eepromWriteAction(1, 0, ACT_KEY, MOD_GUI, tokenFromKeyString("E"));

  // Tap 2: Win+R
  TapHeader h2 = {MODE_MACRO, 2, DEFAULT_DELAY_MS, 1, 1};
  eepromWriteHeader(2, h2);
  eepromWriteAction(2, 0, ACT_KEY, MOD_GUI, tokenFromKeyString("R"));

  // Tap 3: Ctrl+Shift+Esc
  TapHeader h3 = {MODE_MACRO, 3, DEFAULT_DELAY_MS, 1, 1};
  eepromWriteHeader(3, h3);
  eepromWriteAction(3, 0, ACT_KEY, (MOD_CTRL | MOD_SHIFT), tokenFromKeyString("ESC"));

  // Tap 4: Win+D (Show Desktop)
  TapHeader h4 = {MODE_MACRO, 4, DEFAULT_DELAY_MS, 1, 1};
  eepromWriteHeader(4, h4);
  eepromWriteAction(4, 0, ACT_KEY, MOD_GUI, tokenFromKeyString("D"));
}

bool checkSignature() {
  return EEPROM.read(0) == 'M' && EEPROM.read(1) == 'K' && EEPROM.read(2) == 'B' && EEPROM.read(3) == EEPROM_VERSION;
}

// ---------------- Serial protocol ----------------

String inLine;

void sendLine(const String &s) {
  Serial.println(s);
}

void handleWriteMacro(uint8_t tap) {
  // Read lines until WRITE_MACRO_END; store actions sequentially
  uint8_t actions = 0;
  uint8_t combos = 0;
  uint16_t base = eepromOffsetForTap(tap);
  int actionsStart = base + 6;
  int p = actionsStart;
  unsigned long startMs = millis();
  while (true) {
    // timeout safety
    if (millis() - startMs > 3000) {
      sendLine("ERR:TIMEOUT");
      return;
    }
    if (!Serial.available()) { delay(5); continue; }
    String l = Serial.readStringUntil('\n');
    l.trim();
    if (l.length() == 0) continue;
    if (l == "WRITE_MACRO_END") break;
    if (!l.startsWith("A:")) continue;
    if (actions >= MAX_ACTIONS) continue; // ignore overflow
    if (l.charAt(2) == 'K') {
      // A:K:mods:key
      int p1 = l.indexOf(':', 4);
      if (p1 < 0) continue;
      uint8_t mods = (uint8_t) l.substring(4, p1).toInt();
      String keyStr = l.substring(p1 + 1);
      uint8_t token = tokenFromKeyString(keyStr);
      EEPROM.update(p++, ACT_KEY);
      EEPROM.update(p++, mods);
      EEPROM.update(p++, token);
      actions++;
      combos++;
    } else if (l.charAt(2) == 'D') {
      // A:D:ms
      uint16_t ms = (uint16_t) l.substring(4).toInt();
      EEPROM.update(p++, ACT_DELAY);
      EEPROM.update(p++, ms & 0xFF);
      EEPROM.update(p++, (ms >> 8) & 0xFF);
      actions++;
    }
  }
  // Update header
  TapHeader h = eepromReadHeader(tap);
  h.actionsCount = actions;
  h.combosCount = combos;
  eepromWriteHeader(tap, h);
  sendLine("OK");
}

void handleReadMacro(uint8_t tap) {
  TapHeader h = eepromReadHeader(tap);
  sendLine(String("MACRO_BEGIN:") + tap);
  for (uint8_t i = 0; i < h.actionsCount; i++) {
    uint8_t type, a, b;
    eepromReadAction(tap, i, type, a, b);
    if (type == ACT_DELAY) {
      uint16_t ms = (uint16_t)b << 8 | a;
      sendLine(String("A:D:") + ms);
    } else if (type == ACT_KEY) {
      // a=mods, b=token -> convert token back to string key
      String keyStr;
      if (b >= 1 && b <= 26) keyStr = String(char('A' + (b - 1)));
      else if (b >= 27 && b <= 36) keyStr = String(char('0' + (b - 27)));
      else if (b >= 64 && b <= 75) keyStr = String("F") + (b - 63);
      else {
        switch (b) {
          case 80: keyStr = "ESC"; break;
          case 81: keyStr = "TAB"; break;
          case 82: keyStr = "ENTER"; break;
          case 83: keyStr = "SPACE"; break;
          case 84: keyStr = "HOME"; break;
          case 85: keyStr = "END"; break;
          case 86: keyStr = "PAGEUP"; break;
          case 87: keyStr = "PAGEDOWN"; break;
          case 88: keyStr = "LEFT"; break;
          case 89: keyStr = "RIGHT"; break;
          case 90: keyStr = "UP"; break;
          case 91: keyStr = "DOWN"; break;
          case 92: keyStr = "BACKSPACE"; break;
          case 93: keyStr = "DELETE"; break;
          default: keyStr = ""; break;
        }
      }
      sendLine(String("A:K:") + a + ":" + keyStr);
    }
  }
  sendLine(String("MACRO_END:") + tap);
}

void setMode(uint8_t tap, uint8_t mode) {
  TapHeader h = eepromReadHeader(tap);
  h.mode = mode;
  eepromWriteHeader(tap, h);
  sendLine(String("MODE:") + tap + ":" + mode);
}

void setAppCode(uint8_t tap, const String &code) {
  // codes Q1, Q2, Q3 -> map to 1,2,3
  uint8_t app = 0;
  if (code == "Q1") app = 1; else if (code == "Q2") app = 2; else if (code == "Q3") app = 3; else if (code == "Q4") app = 4;
  TapHeader h = eepromReadHeader(tap);
  h.appCode = app;
  eepromWriteHeader(tap, h);
  sendLine("OK");
}

void handleLine(const String &l) {
  // Inbound data implies an active PC connection
  pcConnected = true;
  if (l == "HELLO_PC") { sendLine("HELLO_ARDUINO"); pcConnected = true; return; }
  if (l == "HELLO_ACK") { pcConnected = true; return; }
  if (l.startsWith("SET_MODE:")) {
    int i1 = l.indexOf(':', 0);
    int i2 = l.indexOf(':', i1 + 1);
    uint8_t tap = (uint8_t) l.substring(i1 + 1, i2).toInt();
    uint8_t mode = (uint8_t) l.substring(i2 + 1).toInt();
    setMode(tap, mode);
    return;
  }
  if (l.startsWith("GET_MODE:")) {
    uint8_t tap = (uint8_t) l.substring(9).toInt();
    TapHeader h = eepromReadHeader(tap);
    sendLine(String("MODE:") + tap + ":" + h.mode);
    return;
  }
  if (l.startsWith("READ_MACRO:")) {
    uint8_t tap = (uint8_t) l.substring(11).toInt();
    handleReadMacro(tap);
    return;
  }
  if (l.startsWith("WRITE_MACRO_BEGIN:")) {
    uint8_t tap = (uint8_t) l.substring(18).toInt();
    handleWriteMacro(tap);
    return;
  }
  if (l.startsWith("SET_APP_CODE:")) {
    int i1 = l.indexOf(':', 0);
    int i2 = l.indexOf(':', i1 + 1);
    uint8_t tap = (uint8_t) l.substring(i1 + 1, i2).toInt();
    String code = l.substring(i2 + 1);
    setAppCode(tap, code);
    return;
  }
  if (l == "PROG_ACK") {
    programmingMode = true;
    digitalWrite(PIN_LED, HIGH);
    return;
  }
  if (l == "PROG_EXIT_ACK") {
    programmingMode = false;
    digitalWrite(PIN_LED, LOW);
    return;
  }
}

// ---------------- Button handling ----------------

// We'll rely solely on MultiClick to correctly differentiate single and double taps.

void onLongPressStart() {
  // Request programming mode from PC
  sendLine("PROG_REQ");
}

void onMultiClick() {
  uint8_t clicks = button.getNumberClicks();
  if (clicks == 0) return;
  if (programmingMode) {
    // Exit programming mode and inform PC how many taps were used to exit
    String msg = String("PROG_EXIT_TAPS:") + clicks;
    sendLine(msg);
    // Locally clear programming mode and LED
    programmingMode = false;
    digitalWrite(PIN_LED, LOW);
    return;
  }
  // Regular operation (support up to 4 taps)
  uint8_t tap = clicks > 4 ? 4 : clicks;
  TapHeader h = eepromReadHeader(tap);
  if (pcConnected) {
    if (h.mode == MODE_APP) {
      // send app trigger to PC
      String code = (h.appCode == 1) ? "Q1" : (h.appCode == 2) ? "Q2" : (h.appCode == 3) ? "Q3" : (h.appCode == 4) ? "Q4" : "Q1";
      sendLine(String("APP_TRIGGER:") + tap + ":" + code);
      sendLine(String("TAP:") + tap);
      return;
    }
    // MODE_MACRO under pcConnected -> execute macro
    executeMacro(tap);
    sendLine(String("TAP:") + tap);
    return;
  }
  // No PC connection: always execute stored macro regardless of mode
  executeMacro(tap);
  sendLine(String("TAP:") + tap);
}

// ---------------- Setup & Loop ----------------

void setup() {
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);
  pinMode(PIN_BUTTON_GND, OUTPUT);
  digitalWrite(PIN_BUTTON_GND, LOW);
  pinMode(PIN_BUTTON, INPUT_PULLUP);

  Serial.begin(115200);
  Keyboard.begin();

  // OneButton configuration
  button.setClickTicks(CLICK_TICKS_MS);
  button.setPressTicks(LONG_PRESS_MS);
  button.attachLongPressStart(onLongPressStart);
  button.attachMultiClick(onMultiClick);

#ifdef CLEAR_EEPROM
  setDefaults();
#else
  if (!checkSignature()) {
    setDefaults();
  }
#endif

  sendLine("HELLO_ARDUINO");
}

void loop() {
  button.tick();

  // If host closed the serial port, consider app connection lost
  if (!Serial) { pcConnected = false; }

  // Read serial lines
  while (Serial.available()) {
    String l = Serial.readStringUntil('\n');
    l.trim();
    if (l.length() == 0) continue;
    handleLine(l);
  }
}
