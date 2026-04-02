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
import socket
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

# UDP send chunk: 1024 frames * 2 bytes/frame = 2048 bytes.
_UDP_SEND_CHUNK = 2048


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

        # Audio pipeline state — only touched by the single worker task.
        self._audio_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._udp_buffer: bytearray = bytearray()
        self._resampler: av.AudioResampler | None = None

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

    # ── Sendspin connection ───────────────────────────────────────────────────

    async def _run_sendspin(self) -> None:
        """Connect to the Sendspin server and keep the connection alive."""
        supported_formats = [
            SupportedAudioFormat(
                codec=AudioCodec.PCM,
                sample_rate=UDP_AUDIO_SAMPLE_RATE,
                bit_depth=UDP_AUDIO_BIT_DEPTH,
                channels=UDP_AUDIO_CHANNELS,
            ),
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
        ]

        player_support = ClientHelloPlayerSupport(
            supported_formats=supported_formats,
            buffer_capacity=512 * 1024,
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

        A single worker task drains the queue so that _udp_buffer and
        _resampler are never accessed concurrently.
        """
        loop = asyncio.get_event_loop()
        dest = (self._udp_host, self._udp_port)

        while True:
            try:
                data = await self._audio_queue.get()

                if self._udp_sock is None:
                    continue

                # Decode, mix to mono, resample — all in an executor thread
                pcm_out: bytes = await loop.run_in_executor(
                    None, self._process_chunk, data
                )

                if not pcm_out:
                    continue

                self._udp_buffer.extend(pcm_out)

                # Drain buffer in _UDP_SEND_CHUNK-sized packets
                while len(self._udp_buffer) >= _UDP_SEND_CHUNK:
                    chunk = bytes(self._udp_buffer[:_UDP_SEND_CHUNK])
                    del self._udp_buffer[:_UDP_SEND_CHUNK]
                    self._udp_sock.sendto(chunk, dest)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                _LOGGER.debug("Audio worker error: %s", exc)

    def _process_chunk(self, data: bytes) -> bytes:
        """Decode raw PCM, resample via PyAV, and encode to s16le mono.

        Called from an executor thread by the single worker task.
        """
        if not data or self._resampler is None:
            return b""

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
        samples = len(data) // frame_size

        if samples == 0:
            return b""

        try:
            # 1. Create PyAV frame from input data
            frame = av.AudioFrame(format=in_format, layout=layout, samples=samples)
            frame.sample_rate = in_rate
            frame.planes[0].update(data)

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
        self._udp_buffer.clear()

        # Drain stale chunks from a previous stream
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        in_rate = getattr(payload, "sample_rate", 48000)
        self._stream = {
            "codec": getattr(payload, "codec", AudioCodec.PCM),
            "sample_rate": in_rate,
            "channels": getattr(payload, "channels", 2),
            "bit_depth": getattr(payload, "bit_depth", 16),
        }

        # Create a fresh PyAV streaming resampler
        self._resampler = av.AudioResampler(
            format="s16", layout="mono", rate=UDP_AUDIO_SAMPLE_RATE
        )

        _LOGGER.debug("Sendspin stream started: %s", self._stream)
        self._attr_state = MediaPlayerState.PLAYING
        self.async_write_ha_state()

    def _on_audio_chunk(
        self, timestamp: float, data: bytes, audio_format: Any = None
    ) -> None:
        """Queue the incoming audio chunk for the worker to process."""
        if data and self._udp_sock is not None:
            self._audio_queue.put_nowait(data)

    def _on_stream_end(self) -> None:
        """Flush the soxr resampler tail, send remaining buffer, clean up."""
        _LOGGER.debug("Sendspin stream ended")

        # Flush the resampler's internal delay line
        if self._resampler is not None:
            try:
                tail_frames = self._resampler.resample(None)
                for out in tail_frames:
                    b = bytes(out.planes[0])[: out.samples * 2]
                    self._udp_buffer.extend(b)
            except Exception as exc:
                _LOGGER.debug("Resampler flush error: %s", exc)
            self._resampler = None

        # Send whatever remains in the UDP buffer
        if self._udp_buffer and self._udp_sock is not None:
            try:
                dest = (self._udp_host, self._udp_port)
                self._udp_sock.sendto(bytes(self._udp_buffer), dest)
            except Exception as exc:
                _LOGGER.debug("UDP flush error: %s", exc)
        self._udp_buffer.clear()
        self._stream = {}

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


