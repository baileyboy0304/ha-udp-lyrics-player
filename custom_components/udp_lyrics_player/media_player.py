"""UDP Lyrics Player – media_player platform.

Connects to a Sendspin server as a PLAYER client so that it participates in
the synchronised playback group.  Every PCM audio chunk received from the
server is resampled to 16-bit mono 16 kHz and forwarded over UDP to the
configured destination, making the audio available to the Music Companion
lyrics-recognition (tagging) service.

Audio pipeline
--------------
Sendspin server  →  aiosendspin client  →  av.AudioFrame   →  PyAV resample
    (any PCM)            (WebSocket)       → interleaved    → 16 kHz s16le mono
                                                             → UDP socket
"""

from __future__ import annotations

import asyncio
import logging
import random
import socket
import struct
import time
import uuid
from typing import Any

import av
from aiosendspin.client import SendspinClient
from aiosendspin.models import (
    AudioCodec,
    DeviceInfo,
    MediaCommand,
    PlayerCommand,
    PlayerStateType,
    Roles,
)
from aiosendspin.models.core import GoodbyeReason
from aiosendspin.models.player import (
    ClientHelloPlayerSupport,
    SupportedAudioFormat,
)

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_PLAYER_NAME,
    CONF_SENDSPIN_SERVER_URL,
    CONF_UDP_HOST,
    CONF_UDP_PORT,
    DOMAIN,
    UDP_AUDIO_BIT_DEPTH,
    UDP_AUDIO_CHANNELS,
    UDP_AUDIO_SAMPLE_RATE,
)

_LOGGER = logging.getLogger(__name__)

# RTP payload chunking ceiling. Payloads are split to this size at most.
# 1024 mono s16 samples = 2048 bytes = 64 ms at 16 kHz.
_RTP_MAX_PAYLOAD_BYTES = 2048

# RTP (RFC 3550) constants.
# Payload type 96 is the first dynamic slot — used here for L16 mono 16 kHz.
# The clock rate equals the audio sample rate (16 000 Hz), so the timestamp
# increments by the number of PCM samples contained in each packet.
_RTP_PAYLOAD_TYPE = 96

# Queue bounds: ~6 s of 25 ms input chunks. Beyond this we drop the oldest
# chunk so a transient stall cannot turn into permanent latency.
_QUEUE_MAX = 256

# Drop chunks whose scheduled play time is already this far in the past.
_STALE_DROP_THRESHOLD_US = 1_000_000


# ── Platform setup ────────────────────────────────────────────────────────────


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the UDP Lyrics Player entity for this config entry."""
    async_add_entities([UDPLyricsPlayer(config_entry)], update_before_add=False)


# ── Entity ────────────────────────────────────────────────────────────────────


class UDPLyricsPlayer(MediaPlayerEntity):
    """Sendspin-compatible player that forwards audio over UDP."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = None

    # ── Construction ──────────────────────────────────────────────────────────

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

        self._player_name: str = config_entry.data[CONF_PLAYER_NAME]
        self._server_url: str = config_entry.data[CONF_SENDSPIN_SERVER_URL]
        self._udp_host: str = config_entry.data[CONF_UDP_HOST]
        self._udp_port: int = config_entry.data[CONF_UDP_PORT]

        self._client_id: str = str(
            uuid.uuid5(uuid.NAMESPACE_DNS, config_entry.entry_id)
        )

        # HA entity attributes
        self._attr_unique_id = config_entry.entry_id
        self._attr_state = MediaPlayerState.IDLE
        self._attr_volume_level: float = 1.0
        self._attr_is_volume_muted: bool = False
        self._attr_media_title: str | None = None
        self._attr_media_artist: str | None = None
        self._attr_supported_features = (
            MediaPlayerEntityFeature.PLAY
            | MediaPlayerEntityFeature.PAUSE
            | MediaPlayerEntityFeature.STOP
            | MediaPlayerEntityFeature.NEXT_TRACK
            | MediaPlayerEntityFeature.PREVIOUS_TRACK
            | MediaPlayerEntityFeature.VOLUME_SET
            | MediaPlayerEntityFeature.VOLUME_MUTE
        )

        # Runtime state
        self._sendspin: SendspinClient | None = None
        self._udp_sock: socket.socket | None = None
        self._stream: dict[str, Any] = {}
        self._connect_task: asyncio.Task | None = None
        self._worker_task: asyncio.Task | None = None
        self._listener_removers: list = []

        # Throughput diagnostics — periodically log chunks in vs packets out
        # so pacing problems can be distinguished from input starvation.
        self._stats_start_us: int | None = None
        self._stats_chunks_in: int = 0
        self._stats_packets_out: int = 0
        self._stats_process_us: int = 0
        self._stats_last_log_us: int = 0
        self._stats_bytes_in: int = 0
        self._stats_audio_us_in: int = 0
        self._stats_last_ts_us: int | None = None

        # Audio pipeline state — only touched by the single worker task.
        self._audio_queue: asyncio.Queue[tuple[int, bytes]] = asyncio.Queue(
            maxsize=_QUEUE_MAX
        )
        self._in_buffer: bytearray = bytearray()
        self._resampler: av.AudioResampler | None = None
        self._passthrough: bool = False

        # RTP session state — reset on every stream_start.
        # Initialised to zero here; _reset_rtp_state() assigns random values
        # before the first packet is ever sent.
        self._rtp_seq: int = 0
        self._rtp_ts: int = 0
        self._rtp_ssrc: int = 0
        self._rtp_first_packet: bool = True

    # ── Device info ───────────────────────────────────────────────────────────

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._config_entry.entry_id)},
            "name": self._player_name,
            "manufacturer": "Music Companion",
            "model": "UDP Lyrics Player",
            "sw_version": "1.0.0",
        }

    # ── HA lifecycle hooks ────────────────────────────────────────────────────

    async def async_added_to_hass(self) -> None:
        """Open the UDP socket, start the audio worker, and connect."""
        self._open_udp_socket()
        self._worker_task = self.hass.async_create_task(
            self._audio_worker_loop(),
            name=f"udp_lyrics_worker_{self._client_id}",
        )
        self._connect_task = self.hass.async_create_task(
            self._run_sendspin(),
            name=f"udp_lyrics_conn_{self._client_id}",
        )

    async def async_will_remove_from_hass(self) -> None:
        """Cancel tasks and release resources."""
        for task in (self._connect_task, self._worker_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        await self._teardown_sendspin()
        self._close_udp_socket()

    # ── UDP socket ────────────────────────────────────────────────────────────

    def _open_udp_socket(self) -> None:
        """Create a blocking UDP socket.

        Blocking is correct here — sendto runs in an executor thread.
        A non-blocking socket raises BlockingIOError and silently drops
        packets when the OS send buffer is momentarily full.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp_sock = sock
        _LOGGER.debug(
            "UDP socket opened → %s:%d", self._udp_host, self._udp_port
        )

    def _close_udp_socket(self) -> None:
        if self._udp_sock:
            self._udp_sock.close()
            self._udp_sock = None

    # ── RTP helpers ───────────────────────────────────────────────────────────

    def _reset_rtp_state(self) -> None:
        """Randomise RTP sequence number, timestamp, and SSRC for a new stream.

        RFC 3550 §5.1 recommends random initial values so that streams are
        harder to predict and multiple concurrent streams are distinguishable.
        A fresh SSRC is chosen per stream so that the receiver sees a clean
        synchronisation source each time.
        """
        self._rtp_seq = random.randint(0, 0xFFFF)
        self._rtp_ts = random.randint(0, 0xFFFFFFFF)
        self._rtp_ssrc = random.randint(1, 0xFFFFFFFF)  # 0 is reserved
        self._rtp_first_packet = True

    def _make_rtp_packet(self, payload: bytes) -> bytes:
        """Prepend a 12-byte RTP header (RFC 3550) to *payload* and return the
        resulting packet.

        Header layout (network byte order)::

             0                   1                   2                   3
             0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
            +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
            |V=2|P|X|  CC   |M|     PT      |       sequence number         |
            +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
            |                           timestamp                           |
            +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
            |           synchronization source (SSRC) identifier           |
            +-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+

        * V=2, P=0, X=0, CC=0  → first byte is always 0x80.
        * Marker bit is set on the very first packet of a stream (talk-spurt
          start), then cleared for the remainder per RFC 3551 §4.1.
        * Payload type 96 (dynamic) for L16 mono 16 kHz.
        * Timestamp clock runs at the audio sample rate (16 000 Hz); it is
          advanced by the number of PCM samples contained in *payload*.
        * Sequence number wraps at 65 535 → 0 as required by the RFC.
        """
        marker = 1 if self._rtp_first_packet else 0
        self._rtp_first_packet = False

        header = struct.pack(
            "!BBHII",
            0x80,                            # V=2, P=0, X=0, CC=0
            (marker << 7) | _RTP_PAYLOAD_TYPE,
            self._rtp_seq & 0xFFFF,
            self._rtp_ts & 0xFFFFFFFF,
            self._rtp_ssrc,
        )

        # Advance counters *after* building the header so the values written
        # above match what the receiver will decode for this packet.
        self._rtp_seq = (self._rtp_seq + 1) & 0xFFFF
        # Each s16 mono sample is 2 bytes; advance timestamp by sample count.
        self._rtp_ts = (self._rtp_ts + len(payload) // 2) & 0xFFFFFFFF

        return header + payload

    # ── Sendspin connection ───────────────────────────────────────────────────

    async def _run_sendspin(self) -> None:
        """Connect to the Sendspin server and keep the connection alive."""
        # Format priority: ask the server for the SOURCE format first so it
        # can passthrough without transcoding. Earlier we put 16 kHz mono
        # first, which forced the server to decode → resample → downmix →
        # re-encode every chunk. That transcode could not keep up with real
        # time, so the server dropped chunks via _check_late_binary, leaving
        # us with only ~42 % of the audio bytes (ffmpeg saw speed=0.444x).
        # The reference sendspin-cli player never asks for 16 kHz mono — it
        # advertises native rates only. We resample 48k stereo → 16k mono
        # client-side in _process_chunk (PyAV soxr), which is cheap.
        supported_formats = [
            SupportedAudioFormat(
                codec=AudioCodec.PCM,
                sample_rate=48000,
                bit_depth=16,
                channels=2,
            ),
            SupportedAudioFormat(
                codec=AudioCodec.PCM,
                sample_rate=44100,
                bit_depth=16,
                channels=2,
            ),
            SupportedAudioFormat(
                codec=AudioCodec.PCM,
                sample_rate=UDP_AUDIO_SAMPLE_RATE,
                bit_depth=UDP_AUDIO_BIT_DEPTH,
                channels=UDP_AUDIO_CHANNELS,
            ),
        ]

        player_support = ClientHelloPlayerSupport(
            supported_formats=supported_formats,
            # Match the reference sendspin-cli player. A small capacity makes
            # the server's BufferTracker block sends once ~half a second of
            # audio is in flight, which throttles our input rate far below
            # real time. HA is not memory-constrained, so mirror the CLI.
            buffer_capacity=32_000_000,
            supported_commands=[PlayerCommand.VOLUME, PlayerCommand.MUTE],
        )

        self._sendspin = SendspinClient(
            client_id=self._client_id,
            client_name=self._player_name,
            roles=[Roles.PLAYER],
            player_support=player_support,
            device_info=DeviceInfo(
                manufacturer="Music Companion",
                software_version="1.0.0",
            ),
            initial_volume=int(self._attr_volume_level * 100),
            initial_muted=self._attr_is_volume_muted,
        )

        self._listener_removers = [
            self._sendspin.add_stream_start_listener(self._on_stream_start),
            self._sendspin.add_audio_chunk_listener(self._on_audio_chunk),
            self._sendspin.add_stream_end_listener(self._on_stream_end),
            self._sendspin.add_stream_clear_listener(self._on_stream_clear),
            self._sendspin.add_group_update_listener(self._on_group_update),
            self._sendspin.add_metadata_listener(self._on_metadata),
            self._sendspin.add_server_command_listener(self._on_server_command),
            self._sendspin.add_disconnect_listener(self._on_disconnect),
        ]

        try:
            _LOGGER.info(
                "UDP Lyrics Player '%s' connecting to %s",
                self._player_name,
                self._server_url,
            )
            await self._sendspin.connect(self._server_url)
            _LOGGER.info(
                "UDP Lyrics Player '%s' connected to Sendspin",
                self._player_name,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOGGER.error(
                "UDP Lyrics Player '%s' failed to connect to %s: %s",
                self._player_name,
                self._server_url,
                exc,
            )
            self._attr_state = MediaPlayerState.IDLE
            self.async_write_ha_state()

    async def _teardown_sendspin(self) -> None:
        """Remove listeners and disconnect gracefully."""
        for remove_fn in self._listener_removers:
            if callable(remove_fn):
                try:
                    remove_fn()
                except Exception:
                    pass
        self._listener_removers.clear()

        if self._sendspin is not None:
            try:
                if self._sendspin.connected:
                    await self._sendspin.send_goodbye(GoodbyeReason.SHUTDOWN)
                    await self._sendspin.disconnect()
            except Exception as exc:
                _LOGGER.debug("Error during Sendspin disconnect: %s", exc)
            self._sendspin = None

    # ── Audio worker (single task, strict FIFO) ──────────────────────────────

    async def _audio_worker_loop(self) -> None:
        """Process audio chunks one-by-one in strict chronological order.

        Pacing model: every incoming chunk is scheduled against its own
        ``compute_play_time(timestamp)`` anchor, then split into RTP payloads.
        Each payload's send time is derived from emitted sample count within
        that chunk. This keeps UDP output aligned to the Sendspin group clock
        while still reflecting real payload cadence (no synthetic fixed-rate
        packet clock that can drift from chunk timing).
        """
        loop = asyncio.get_event_loop()
        dest = (self._udp_host, self._udp_port)

        while True:
            try:
                timestamp, data = await self._audio_queue.get()

                if self._udp_sock is None:
                    continue

                # Overload policy: drop chunks whose scheduled play time is
                # already more than _STALE_DROP_THRESHOLD_US in the past.
                # Without this the worker can never catch up once it falls
                # behind, because every subsequent chunk inherits the lag.
                if self._sendspin is not None:
                    try:
                        target_us = self._sendspin.compute_play_time(int(timestamp))
                        now_us = int(time.monotonic() * 1_000_000)
                        if target_us + _STALE_DROP_THRESHOLD_US < now_us:
                            _LOGGER.debug(
                                "Dropping stale audio chunk (%.2fs behind)",
                                (now_us - target_us) / 1_000_000.0,
                            )
                            continue
                    except Exception:
                        pass

                # Decode, mix to mono, resample — all in an executor thread.
                proc_start_us = int(time.monotonic() * 1_000_000)
                pcm_out: bytes = await loop.run_in_executor(
                    None, self._process_chunk, data
                )
                proc_elapsed_us = int(time.monotonic() * 1_000_000) - proc_start_us

                # Throughput diagnostics
                if self._stats_start_us is None:
                    self._stats_start_us = int(time.monotonic() * 1_000_000)
                    self._stats_last_log_us = self._stats_start_us
                self._stats_chunks_in += 1
                self._stats_process_us += proc_elapsed_us
                self._stats_bytes_in += len(data)
                if self._stats_last_ts_us is not None:
                    ts_delta = int(timestamp) - self._stats_last_ts_us
                    if 0 < ts_delta < 1_000_000:
                        self._stats_audio_us_in += ts_delta
                self._stats_last_ts_us = int(timestamp)

                if not pcm_out:
                    continue

                # Anchor this chunk to Sendspin's playback clock.
                if self._sendspin is not None:
                    try:
                        chunk_start_us = self._sendspin.compute_play_time(int(timestamp))
                    except Exception:
                        chunk_start_us = int(time.monotonic() * 1_000_000)
                else:
                    chunk_start_us = int(time.monotonic() * 1_000_000)

                # Split this chunk into RTP payloads and pace each payload
                # from sample offsets within this chunk, not from a synthetic
                # fixed packet cadence.
                byte_cursor = 0
                while byte_cursor < len(pcm_out):
                    end = min(byte_cursor + _RTP_MAX_PAYLOAD_BYTES, len(pcm_out))
                    payload = pcm_out[byte_cursor:end]
                    samples_before = byte_cursor // 2  # mono s16 = 2 bytes/sample
                    send_us = chunk_start_us + (
                        samples_before * 1_000_000 // UDP_AUDIO_SAMPLE_RATE
                    )
                    now_us = int(time.monotonic() * 1_000_000)
                    delay_sec = (send_us - now_us) / 1_000_000.0
                    if delay_sec > 0:
                        await asyncio.sleep(delay_sec)

                    self._udp_sock.sendto(self._make_rtp_packet(payload), dest)
                    self._stats_packets_out += 1
                    byte_cursor = end

                # Log throughput every 5s so input-vs-output rate is visible
                # without needing an external capture.
                now_stats_us = int(time.monotonic() * 1_000_000)
                if now_stats_us - self._stats_last_log_us >= 5_000_000:
                    elapsed_us = now_stats_us - (self._stats_start_us or now_stats_us)
                    if elapsed_us > 0 and self._stats_chunks_in > 0:
                        avg_proc_ms = (
                            self._stats_process_us / self._stats_chunks_in / 1000.0
                        )
                        in_rate = self._stats_chunks_in * 1_000_000 / elapsed_us
                        out_rate = self._stats_packets_out * 1_000_000 / elapsed_us
                        avg_chunk_bytes = (
                            self._stats_bytes_in / self._stats_chunks_in
                        )
                        audio_ratio = self._stats_audio_us_in / elapsed_us
                        _LOGGER.info(
                            "UDP pacing: in=%.1f chunks/s out=%.1f pkts/s "
                            "avg_proc=%.1fms avg_chunk=%.0fB audio_in/realtime=%.2fx "
                            "buffer=%dB queue=%d passthrough=%s",
                            in_rate,
                            out_rate,
                            avg_proc_ms,
                            avg_chunk_bytes,
                            audio_ratio,
                            0,
                            self._audio_queue.qsize(),
                            self._passthrough,
                        )
                    self._stats_last_log_us = now_stats_us

            except asyncio.CancelledError:
                break
            except Exception as exc:
                _LOGGER.debug("Audio worker error: %s", exc)

    def _process_chunk(self, data: bytes) -> bytes:
        """Decode raw PCM, resample via PyAV, and encode to s16le mono.

        Called from an executor thread by the single worker task.
        """
        in_rate = self._stream.get("sample_rate", 48000)
        in_channels = self._stream.get("channels", 2)
        in_bit_depth = self._stream.get("bit_depth", 16)

        # Convert bit_depth to PyAV format
        if in_bit_depth == 16:
            in_format = "s16"
            bytes_per_sample = 2
        elif in_bit_depth == 32:
            in_format = "s32"
            bytes_per_sample = 4
        else:
            _LOGGER.warning("Unsupported bit depth: %d", in_bit_depth)
            return b""

        layout = 'stereo' if in_channels == 2 else 'mono'
        frame_size = bytes_per_sample * in_channels

        # Buffer incoming incomplete frames
        if data:
            self._in_buffer.extend(data)

        samples = len(self._in_buffer) // frame_size

        if samples == 0:
            return b""

        bytes_to_consume = samples * frame_size
        chunk_data = bytes(self._in_buffer[:bytes_to_consume])
        del self._in_buffer[:bytes_to_consume]

        # Fast path: the server is already producing the exact target format
        # the UDP receiver expects (16 kHz / mono / s16le). PyAV adds no value
        # here and resampling would introduce tiny ramp-up delays, so skip it.
        if self._passthrough:
            return chunk_data

        if self._resampler is None:
            return b""

        try:
            # 1. Create PyAV frame using only completely aligned frames
            frame = av.AudioFrame(format=in_format, layout=layout, samples=samples)
            frame.sample_rate = in_rate
            frame.planes[0].update(chunk_data)

            # 2. Resample to target format
            out_frames = self._resampler.resample(frame)

            # 3. Extract s16le bytes
            out_bytes = bytearray()
            for out in out_frames:
                # s16 mono = 2 bytes per sample
                b = bytes(out.planes[0])[: out.samples * 2]
                out_bytes.extend(b)

            return bytes(out_bytes)
        except Exception as exc:
            _LOGGER.debug("PyAV resample error: %s", exc)
            return b""

    # ── Sendspin event callbacks (synchronous, as aiosendspin requires) ───────

    def _on_stream_start(self, payload: Any) -> None:
        """Store stream format metadata and create the PyAV resampler."""
        self._in_buffer.clear()
        # Reset throughput stats so the first log line reflects this stream.
        self._stats_start_us = None
        self._stats_chunks_in = 0
        self._stats_packets_out = 0
        self._stats_process_us = 0
        self._stats_last_log_us = 0
        self._stats_bytes_in = 0
        self._stats_audio_us_in = 0
        self._stats_last_ts_us = None

        # Drain stale chunks from a previous stream
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        if isinstance(payload, dict):
            player_info = payload
            if "payload" in player_info and isinstance(player_info["payload"], dict):
                player_info = player_info["payload"]
            if "player" in player_info and isinstance(player_info["player"], dict):
                player_info = player_info["player"]

            in_rate = player_info.get("sample_rate", 48000)
            in_codec = player_info.get("codec", AudioCodec.PCM)
            in_channels = player_info.get("channels", 2)
            in_bit_depth = player_info.get("bit_depth", 16)
        else:
            player_info = payload
            if hasattr(player_info, "payload") and player_info.payload is not None:
                player_info = player_info.payload
            if hasattr(player_info, "player") and player_info.player is not None:
                player_info = player_info.player

            in_rate = getattr(player_info, "sample_rate", 48000)
            in_codec = getattr(player_info, "codec", AudioCodec.PCM)
            in_channels = getattr(player_info, "channels", 2)
            in_bit_depth = getattr(player_info, "bit_depth", 16)

        self._stream = {
            "codec": in_codec,
            "sample_rate": in_rate,
            "channels": in_channels,
            "bit_depth": in_bit_depth,
        }

        # If the server is already emitting our target format we can skip
        # PyAV entirely — both cheaper and avoids the resampler's ramp-up.
        self._passthrough = (
            in_rate == UDP_AUDIO_SAMPLE_RATE
            and in_channels == UDP_AUDIO_CHANNELS
            and in_bit_depth == UDP_AUDIO_BIT_DEPTH
        )

        if self._passthrough:
            self._resampler = None
        else:
            self._resampler = av.AudioResampler(
                format="s16", layout="mono", rate=UDP_AUDIO_SAMPLE_RATE
            )

        # New stream → new RTP session (fresh sequence number, timestamp, SSRC)
        self._reset_rtp_state()

        _LOGGER.info(
            "Sendspin stream started: %s passthrough=%s",
            self._stream,
            self._passthrough,
        )
        self._attr_state = MediaPlayerState.PLAYING
        self.async_write_ha_state()

    def _on_audio_chunk(
        self, timestamp: float, data: bytes, audio_format: Any = None
    ) -> None:
        """Queue the incoming audio chunk for the worker to process."""
        if not (data and self._udp_sock is not None):
            return
        try:
            self._audio_queue.put_nowait((int(timestamp), data))
        except asyncio.QueueFull:
            # Queue overflow → the worker has fallen behind. Drop the oldest
            # chunk to release the bound, then enqueue the fresh one so the
            # backlog never grows without bound.
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._audio_queue.put_nowait((int(timestamp), data))
            except asyncio.QueueFull:
                pass

    def _on_stream_end(self) -> None:
        """Flush the soxr resampler tail and clean up."""
        _LOGGER.debug("Sendspin stream ended")

        # Flush the resampler's internal delay line
        if self._resampler is not None:
            try:
                self._resampler.resample(None)
            except Exception as exc:
                _LOGGER.debug("Resampler flush error: %s", exc)
            self._resampler = None

        # Any remaining audio has already been emitted from _audio_worker_loop.
        self._in_buffer.clear()
        self._stream = {}
        self._passthrough = False

    def _on_stream_clear(self, roles: list[str] | None = None) -> None:
        """Handle stream/clear — server asked the pipeline to drop everything.

        Unlike stream_end this can fire mid-track (group changes, track skip,
        server flush). We drop any queued / buffered audio so stale content
        cannot leak into the next stream.
        """
        _LOGGER.debug("Sendspin stream cleared (roles=%s)", roles)

        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        self._in_buffer.clear()
        if self._resampler is not None:
            try:
                # Flush and discard the resampler tail so it does not leak
                # into the next stream.
                self._resampler.resample(None)
            except Exception:
                pass

    def _on_group_update(self, state: Any) -> None:
        """Sync HA state with the Sendspin group playback state."""
        try:
            raw = (
                state.get("state") if isinstance(state, dict)
                else getattr(state, "state", None)
            )
            if raw is None:
                return
            raw_upper = str(raw).upper()
            if "PLAYING" in raw_upper:
                self._attr_state = MediaPlayerState.PLAYING
            elif "PAUSED" in raw_upper:
                self._attr_state = MediaPlayerState.PAUSED
            elif "STOPPED" in raw_upper or "IDLE" in raw_upper:
                self._attr_state = MediaPlayerState.IDLE
            self.async_write_ha_state()
        except Exception as exc:
            _LOGGER.debug("Group update error: %s", exc)

    def _on_metadata(self, metadata: Any) -> None:
        """Update track title / artist from Sendspin metadata."""
        try:
            if isinstance(metadata, dict):
                self._attr_media_title = (
                    metadata.get("title") or metadata.get("name")
                )
                artists = metadata.get("artists")
                self._attr_media_artist = (
                    artists[0] if isinstance(artists, list) and artists
                    else metadata.get("artist")
                )
            else:
                self._attr_media_title = getattr(metadata, "title", None)
                self._attr_media_artist = getattr(metadata, "artist", None)
            self.async_write_ha_state()
        except Exception as exc:
            _LOGGER.debug("Metadata update error: %s", exc)

    def _on_server_command(self, payload: Any) -> None:
        """Apply volume / mute commands sent by the Sendspin server."""
        try:
            volume = getattr(payload, "volume", None)
            if volume is not None:
                self._attr_volume_level = max(0.0, min(1.0, volume / 100.0))
            muted = getattr(payload, "muted", None)
            if muted is not None:
                self._attr_is_volume_muted = bool(muted)
            self.async_write_ha_state()
        except Exception as exc:
            _LOGGER.debug("Server command error: %s", exc)

    def _on_disconnect(self, reason: str) -> None:
        """Handle server-initiated disconnection."""
        _LOGGER.warning(
            "UDP Lyrics Player '%s' disconnected from Sendspin: %s",
            self._player_name,
            reason,
        )
        self._attr_state = MediaPlayerState.IDLE
        self._stream = {}
        self.async_write_ha_state()

    # ── HA media player controls ──────────────────────────────────────────────

    async def _send_group_cmd(self, command: Any) -> None:
        if self._sendspin and self._sendspin.connected:
            try:
                await self._sendspin.send_group_command(command)
            except Exception as exc:
                _LOGGER.debug("send_group_command(%s) error: %s", command, exc)

    async def async_media_play(self) -> None:
        await self._send_group_cmd(MediaCommand.PLAY)
        self._attr_state = MediaPlayerState.PLAYING
        self.async_write_ha_state()

    async def async_media_pause(self) -> None:
        await self._send_group_cmd(MediaCommand.PAUSE)
        self._attr_state = MediaPlayerState.PAUSED
        self.async_write_ha_state()

    async def async_media_stop(self) -> None:
        await self._send_group_cmd(MediaCommand.STOP)
        self._attr_state = MediaPlayerState.IDLE
        self.async_write_ha_state()

    async def async_media_next_track(self) -> None:
        await self._send_group_cmd(MediaCommand.NEXT)

    async def async_media_previous_track(self) -> None:
        await self._send_group_cmd(MediaCommand.PREVIOUS)

    async def async_set_volume_level(self, volume: float) -> None:
        self._attr_volume_level = volume
        if self._sendspin and self._sendspin.connected:
            try:
                await self._sendspin.send_player_state(
                    state=PlayerStateType.SYNCHRONIZED,
                    volume=int(self._attr_volume_level * 100),
                    muted=self._attr_is_volume_muted,
                )
            except Exception as exc:
                _LOGGER.debug("send_player_state error: %s", exc)
        self.async_write_ha_state()

    async def async_mute_volume(self, mute: bool) -> None:
        self._attr_is_volume_muted = mute
        if self._sendspin and self._sendspin.connected:
            try:
                await self._sendspin.send_player_state(
                    state=PlayerStateType.SYNCHRONIZED,
                    volume=int(self._attr_volume_level * 100),
                    muted=self._attr_is_volume_muted,
                )
            except Exception as exc:
                _LOGGER.debug("send_player_state error: %s", exc)
        self.async_write_ha_state()
