# Circuit Wiring Guide

This document summarizes the wiring connections needed to assemble the project electronics.

## Teensy 4.1 to Gravity Relay

| From | To | Purpose |
|---|---|---|
| Teensy 4.1 Pin 5 | Gravity Relay D (signal) | Relay control signal |
| Teensy 4.1 VIN (5V) | Gravity Relay + (VCC) | Relay coil power |
| Teensy 4.1 GND | Gravity Relay - (GND) | Common ground |

## Teensy 4.1 to MCP4131 Digital Potentiometer

| From | To | Purpose |
|---|---|---|
| Teensy 4.1 GND | MCP4131 Pin 4 (VSS) | Common ground |
| Teensy 4.1 3.3V | MCP4131 Pin 8 (VDD) | Digital potentiometer power |
| Teensy 4.1 Pin 10 (CS) | MCP4131 Pin 1 (CS) | SPI chip select |
| Teensy 4.1 Pin 11 (MOSI) | MCP4131 Pin 3 (SDI/SDO) | SPI data |
| Teensy 4.1 Pin 13 (SCK) | MCP4131 Pin 2 (SCK) | SPI clock |

## Mean Well Power Supply Connections

| From | To | Purpose |
|---|---|---|
| +V (Slot 1) | Sonication Board Location 1 | Main high power (24V) |
| -V (Slot 1) | Sonication Board Location 2 | Main power ground |
| +V (Slot 2) | Buck Converter A IN+ | Input for Raspberry Pi 5 power |
| -V (Slot 2) | Buck Converter A IN- | Input for Raspberry Pi 5 ground |
| +V (Slot 3) | Buck Converter B IN+ | Input for Teensy/relay power |
| -V (Slot 3) | Buck Converter B IN- | Input for Teensy/relay ground |

## Gravity Relay and MCP4131 to Sonicator Board

| From | To | Purpose |
|---|---|---|
| Relay COM | Sonicator Board Location 4 (Sonic GND) | Common ground for start logic |
| Relay NO | Sonicator Board Location 5 (Sonic Start) | Bridges start input to ground to start sonication |
| MCP4131 Pin 5 (P0A) | Sonicator Board Location 8 (V-Ref) | 5.1V reference for the potentiometer |
| MCP4131 Pin 6 (P0W) | Sonicator Board Location 7 (Amplitude) | Variable amplitude voltage (2V-5V) |
| MCP4131 Pin 7 (P0B) | Sonicator Board Location 6.5 (Sonic GND) | Potentiometer low side |

## Stepper Motor to Stepper Driver

| From | To |
|---|---|
| Stepper Motor Red | Stepper Driver A+ |
| Stepper Motor Red/White Striped | Stepper Driver A- |
| Stepper Motor Green | Stepper Driver B+ |
| Stepper Motor Green/White Striped | Stepper Driver B- |

## Power Supply to Stepper Driver

| From | To |
|---|---|
| Power Supply V+ | Stepper Driver Vdc+ |
| Power Supply V- | Stepper Driver GND |

## Stepper Driver Switch Settings

| Switch | Setting |
|---|---|
| SW1 | On (down) |
| SW2 | Off (up) |
| SW3 | On (down) |
| SW4 | Off (up) |
| SW5 | On (down) |
| SW6 | On (down) |

## Stepper Driver to Teensy 4.1

| From | To |
|---|---|
| Stepper Driver ENA | Not used / left blank |
| Stepper Driver OPTO | Teensy 4.1 3.3V |
| Stepper Driver DIR | Teensy 4.1 Pin 36 |
| Stepper Driver PUL | Teensy 4.1 Pin 34 |

## Stepper Motor Photoelectric Sensor to Teensy 4.1

| From | To |
|---|---|
| Brown to Orange (5-24V) | Teensy 4.1 5V |
| Blue to Blue (Ground) | Teensy 4.1 GND |
| Black to Black (Signal Line) | Any Teensy GPIO pin |

## AC Input Wiring

| Wire Color | Meaning |
|---|---|
| Black | L - Hot |
| White | N - Neutral |
| Green | FG - Ground |