# pbpad power board — connection list

Pin numbers verified from EasyEDA library symbols (LCSC parts).
Status: **PAUSED (2026-06-28)** — schematic ~complete; user evaluating IP5310 pre-made
boards as a possible replacement for this custom PCB. Open: J5 I2C leveling undecided;
PCB layout not started. Section B (charger mode pins) RESOLVED below.

## Parts
- U1 MCP73871-4CAI/ML (C637761) — charger + power path
- U2 TPS61023DRLR (C919459) — boost to 5V
- U3 MAX17048G+T10 (C2682616) — fuel gauge
- J1 USB-C TYPE-C-31-M-12 (C165948)
- J2 LiPo connector (JST-PH 2-pin)
- Battery: 1S LiPo 5000mAh

## A. Confident connections

### Power-path spine
- **VBUS_5V**: J1.VBUS  →  U1.IN(18,19)  →  U1.CE(17, tie high = charge enabled)  →  C1+ (input cap)
- **SYS_OUT**: U1.OUT(1,20)  →  U2.VIN(3)  →  L1  →  U2.SW(5)  →  C2+ (boost input cap)
  - (boost inductor L1 sits between the input rail and SW)
- **+5V**: U2.VOUT(6)  →  Pi 5V (header pin 2)  →  C3+ (output cap)  →  R4 (FB divider top)
- **EN**: U2.EN(2)  →  tie to U2.VIN (boost always on)

### Battery / sense
- **VBAT**: U1.VBAT(14,15)  ↔  J2.1 (batt +)  ↔  U1.VBAT_SENSE(16)  ↔  U3.CELL(2)  ↔  U3.VDD(3)  ↔  C4+ (fuel-gauge decouple)
- **GND**: J1.GND, U1.VSS(10,11), U1.EP(21), U2.GND(4), U3.GND(4), U3.EP(9),
  C1-,C2-,C3-,C4-, R5 (FB bottom), J2.2 (batt -), Pi GND (header pin 6)

### Charge current
- **PROG1**: U1.PROG1(13)  →  R3 (1k, 1%)  →  GND   → sets ~1A fast charge (I = 1000V / R_PROG1)

### USB-C (J1 = USBC1, pins now mapped)
- **VBUS_5V**: J1.VBUS(A4B9) + J1.VBUS(B4A9)  →  U1.IN(18,19)  (tie both VBUS pins together)
- **GND**: J1.GND(A1B12) + J1.GND(B1A12) + J1.EH1..EH4 (shield tabs)  →  GND
- **CC1**: J1.CC1(A5)  →  R1 (5.1k)  →  GND
- **CC2**: J1.CC2(B5)  →  R2 (5.1k)  →  GND
  - presents board as sink; source supplies default 5V (no PD IC, stays under MCP73871 6V max)
- **No connect**: J1 DP1/DN1/DP2/DN2, SBU1/SBU2 (charging only, no USB data)

### I2C (shared with SH1106 OLED on the Pi)
- **SDA**: U3.SDA(8)  →  Pi SDA (header pin 3 / GPIO2)
- **SCL**: U3.SCL(7)  →  Pi SCL (header pin 5 / GPIO3)
  - Pi has onboard 1.8k pull-ups to 3.3V; OLED shares this bus. MAX17048 addr 0x36 (no clash with OLED 0x3C/0x3D)

### Status (optional)
- U1.STAT1(8), U1.STAT2(7), U1.PG#(6): open-drain; resistor pull-up to OUT + optional LED

## B. MCP73871 mode pins — RESOLVED from datasheet (DS20002090F)

PART: use MCP73871-**2**CAI/ML (C185603) = 4.20V. NOT the -4 (4.40V, overcharges LiPo).

- **VPCC(2)** -> IN (VBUS). Datasheet: VPCC feature disabled by tying to IN; ICLC still gives system priority.
- **SEL(3)**  -> HIGH (tie to OUT). AC-adapter mode = up to 1.65A total input, enables ~1A charge.
- **PROG2(4)** -> GND. USB current select; don't-care in AC mode but must not float.
- **THERM(5)** -> R6 (10k) -> VSS. Disables temp monitoring (no NTC).
- **TE#(9)**  -> HIGH (tie to OUT). Disables safety timer -- recommended for big cell + load sharing. [tradeoff]
- **PROG3(12)** -> R7 (10k) -> VSS. ~100mA termination current.
- **PROG1(13)** -> R3 (1k) -> VSS. 1A fast charge. [done]
- **CE(17)** -> HIGH (VBUS). Charge enable. [done]
- Add **C7 = 4.7uF** on VBAT -> GND near U1 (datasheet typical app).
- Status LEDs (STAT1/STAT2/PG) optional, open-drain, pull-up to OUT.

## (former section B placeholder)

- ~~U2 FB divider~~ — RESOLVED: VREF=595mV typ; VOUT=VREF*(1+R4/R5).
  TI Fig 8-1 Li-ion->5V ref design: R4=732k (VOUT->FB), R5=100k (FB->GND), 2x22uF out.
- ~~L1 inductor + C2/C3 caps~~ — CONFIRMED: L1=1uH (Isat>=3A), C2=10uF in, C3=22uF out.
- **U1.VPCC(2)** — input power-path control pin; typical-app connection unconfirmed.
- **U1.SEL(3), U1.PROG2(4), U1.PROG3(12)** — input-current-mode select + input current limit; affects charge behavior.
- **U1.THERM(5)** — if no battery NTC, tie via resistor (likely 10k) to GND to disable; confirm.
- **U1.TE#(9)** — safety-timer enable; confirm tie (GND vs high).
- **U3.CTG(1), U3.QSTRT(6), U3.ALRT#(5)** — fuel-gauge config/quick-start/alert; confirm tie or NC.

## Open items
- Confirm TPS61023 FB ref → compute R4/R5.
- Resolve MCP73871 mode pins (section B) from datasheet typical-app figure.
