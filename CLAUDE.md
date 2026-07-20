# pbpad

Software for a portable Raspberry Pi Zero W device. See hardware context in Claude memory: `project-pi-portable`.

## Hardware

- **MCU:** Raspberry Pi Zero W
- **Display:** SH1106 1.3" OLED (I2C)
- **Battery:** 3.7V 5000mAh LiPo
- **Power:** MCP73871 (charger + power path) → TPS61023 (boost to 5V)
- **Input:** USB-C (5V)

## Notes

- Pi is powered via GPIO pins, not microUSB
- Display uses SH1106 driver — not SSD1306 compatible
