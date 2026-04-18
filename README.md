# UDP Lyrics dummy Player (for SyncLyrics)

A Home Assistant custom component that creates a Sendspin-compatible dummy media player. 

## What does it do?
This component impersonates a network speaker on your system. It is designed to join a normal audio group alongside your real hardware speakers. Once grouped, it receives the playing audio, downsamples to 16kHz mono, encapsulates it as RTP/L16, and forwards it over a configurable UDP port to the synclyrics integration.

## Why use it?
This player is purpose-built to provide a stable, synchronised audio stream to the [SyncLyrics Add-on](https://github.com/AnshulJ999/SyncLyrics). 

SyncLyrics runs separately and provides metadata and syncronised lyrics.

## Installation

This integration is compliant with [HACS](https://hacs.xyz/).

1. Navigate to HACS -> Integrations.
2. Click the three dots in the top right -> Custom Repositories.
3. Paste the URL to this repository, and select `Integration` as the category.
4. Click `Download` to fetch the code.
5. Restart Home Assistant.
6. Navigate to `Settings -> Devices & Services -> Add Integration` and search for **UDP Lyrics Player**.

### Configuration Parameters
- **Player Name**: The name displayed natively inside Home Assistant & Sendspin for this dummy client.
- **Sendspin Server URL**: The Websocket URL bridging into your Sendspin server (Example: `ws://192.168.1.100:8095/ws`).
- **UDP Target Host**: The IP Address / hostname where the SyncLyrics add-on is listening.
- **UDP Target Port**: The configured port for the SyncLyrics add-on (Default: `6056`).

## Troubleshooting
- **No Sync**: Ensure this dummy speaker is added to the exact same Sendspin audio group playing your music. 
- **Boot Fails**: This requires `aiosendspin` and `PyAV`. This should automatically resolve natively via PIP inside the Home Assistant Docker framework when HACS resolves the installation footprint.
