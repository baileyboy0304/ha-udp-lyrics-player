# ha-udp-lyrics-player — Project Rules

Home Assistant custom integration that creates **dummy "sendspin" media_player
entities**. Each dummy entity can join a Music Assistant / sendspin group;
when MA pushes audio to it, the integration decodes the stream, resamples to
**16 kHz mono 16-bit PCM**, and forwards it to **NewLyricsJukebox (NLJ)** over
**RTP/UDP** so NLJ can recognise the track and serve synced lyrics.

This integration exists because NLJ needs an audio source for recognition.
Real speakers don't usually expose their raw output. By dropping a dummy
"speaker" into the MA group, MA itself does the audio fan-out for us.

## Place in the ecosystem

See the master ecosystem map in `baileyboy0304/newlyricsjukebox` →
`CLAUDE.md`. In short:

```
MA group ── audio ──> this dummy player ── RTP/UDP 16k mono ──> NLJ :6056
                                                                  │
                                                                  ↓
                                                              recognition
```

The integration does **not** do recognition, does **not** display lyrics, and
does **not** talk back to NLJ over HTTP. It is purely an audio forwarder
that pretends to be a media_player.

## Files

| Path | Role |
|------|------|
| `custom_components/udp_lyrics_player/manifest.json` | Domain `udp_lyrics_player`. Requires `aiosendspin` and `av` (PyAV for resampling). |
| `custom_components/udp_lyrics_player/__init__.py` | HA platform setup; forwards entries to the `media_player` platform. |
| `custom_components/udp_lyrics_player/const.py` | Constants. UDP output is **hard-coded** to 16 kHz / mono / 16-bit PCM — NLJ requires this. |
| `custom_components/udp_lyrics_player/media_player.py` | Core: dummy player + Sendspin client + audio worker + RTP packetiser. |
| `custom_components/udp_lyrics_player/config_flow.py` | HA UI: 4 fields — `player_name`, `sendspin_server_url`, `udp_host`, `udp_port`. |
| `hacs.json` | HACS metadata. |

## How it advertises itself to MA

- **Supported sample formats**: `48000` and `44100` stereo PCM are advertised
  to the sendspin server (media_player.py ~line 447). The internal target
  (16 kHz mono) is **not** advertised — if it were, MA would try to negotiate
  it as the group's common format and real speakers couldn't play it.
- **Supported commands**: `VOLUME`, `MUTE` only (it's a sink, not a
  controller).
- **Role**: `PLAYER`. `client_id` is a UUIDv5 derived from the HA
  `config_entry.entry_id` so the same entity always presents the same id
  to the sendspin server.

## State reporting

- HA control methods (`async_media_play`, `async_media_pause`,
  `async_media_stop`, etc.) update `_attr_state` and push a sendspin
  `PlayerStateType.SYNCHRONIZED` frame.
- A **2-second heartbeat** (`_PLAYER_STATE_HEARTBEAT_SECONDS`) republishes
  state so MA does not mark the player stale.
- Group transitions (`_on_group_update`) are authoritative — they override
  the conservative "paused on stream_end" assumption.

## Audio path

1. `_on_audio_chunk` callback receives PCM blocks from the sendspin server.
2. PyAV `AudioResampler` converts to **16-bit / mono / 16 kHz**. The resampler
   is created on `_on_stream_start` and flushed/destroyed on `_on_stream_end`
   so trailing samples are not lost.
3. A single async worker drains an internal queue in FIFO order, packetises
   into RTP and sends to `(udp_host, udp_port)`.
4. RTP details:
   - Payload type `96`, L16 mono 16 kHz.
   - 1024 samples per packet (2048-byte payload).
   - SSRC randomised per stream; sequence + timestamp incremented per packet.
   - Marker bit set on the first packet of a stream.
   - **RFC 8285 header extensions** carry `player_name` (ext 1) and
     `player_id` (ext 2). Sent in an initial 5-packet burst, then heartbeated
     every 2 s.

The UDP socket is **blocking** to avoid drops on OS buffer overflow under
load.

## Configuration

Four fields, all required (config_flow.py):

| Field | Default | Notes |
|-------|---------|-------|
| `player_name` | — | Must be unique across entries; surfaces in NLJ as the player name via RTP ext 1. |
| `sendspin_server_url` | `ws://192.168.1.137:8927/sendspin` | Must start `ws://` or `wss://`. |
| `udp_host` | — | Hostname or IP of the NLJ host. |
| `udp_port` | `6056` | NLJ's UDP listener. |

Options flow lets all four be edited after setup.

## Hard rules

- **Do not change the UDP wire format** without coordinating with NLJ. NLJ
  parses RTP v2 + RFC 8285 ext headers and expects 16 kHz mono 16-bit PCM at
  PT 96. If this changes, recognition breaks for every device that depends
  on it (sendspin firmware, atom_echo, this integration).
- **No HTTP feedback path.** This integration must remain a pure audio
  forwarder. Lyrics/now-playing UI lives in NLJ and the display devices.
- **Don't advertise 16 kHz mono to MA.** Keep advertising 48k/44.1k stereo
  so MA can negotiate a common format with real speakers in the group.
- **One sendspin role only**: `PLAYER`. Don't add coordinator/controller
  responsibilities here — they belong elsewhere.
- **No new dependencies without asking.** Currently only `aiosendspin` and
  `av` (PyAV).

## Debugging

Logger name: `custom_components.udp_lyrics_player`. Useful diagnostics:

- `_on_stream_start` / `_on_stream_end` — when MA opens/closes audio.
- `_on_group_update` — group state transitions.
- Worker queue size — back-pressure indicator if NLJ can't keep up.
- RTP sequence/SSRC log lines — confirm packets are leaving the host.

NLJ side: `recognition.udp_capture` logs the player_name / player_id read
from the RFC 8285 extension on each new stream, which is how you confirm this
integration's packets are arriving and being demuxed.

## Branching

Develop on branch `claude/pensive-brown-q8zsyz` per session instructions.
