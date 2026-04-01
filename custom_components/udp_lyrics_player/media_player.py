"""UDP Lyrics Player – media_player platform.

Connects to a Sendspin server as a PLAYER client so that it participates in
the synchronised playback group.  Every PCM audio chunk received from the
server is resampled to 16-bit mono 16 kHz and forwarded over UDP to the
configured destination, making the audio available to the Music Companion
lyrics-recognition (tagging) service.

Audio pipeline
--------------
Sendspin server  →  aiosendspin client  →  resample (numpy)  →  UDP socket
    (any PCM)            (WebSocket)       → 16 kHz / mono / s16le   → IP:port
"""

from __future__ import annotations

import asyncio
import logging
import socket
import uuid
from typing import Any

import numpy as np
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

# UDP send chunk size in bytes.  1024 frames * 2 bytes/frame = 2048 bytes,
# matching the chunk size used by standard audio streamers.
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
    """Sendspin-compatible dummy player that forwards audio over UDP.

    Controls (play / pause / stop / next / previous / volume / mute) relay
    group commands to the Sendspin server so that all real players in the
    group respond.  The entity's own state is kept in sync with the group
    state reported by the server.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = None  # use device name as entity name

    # ── Construction ──────────────────────────────────────────────────────────

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry

        self._player_name: str = config_entry.data[CONF_PLAYER_NAME]
        self._server_url: str = config_entry.data[CONF_SENDSPIN_SERVER_URL]
        self._udp_host: str = config_entry.data[CONF_UDP_HOST]
        self._udp_port: int = config_entry.data[CONF_UDP_PORT]

        # Unique, stable client ID derived from the entry ID
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
        self._stream: dict[str, Any] = {}          # active stream metadata
        self._connect_task: asyncio.Task | None = None
        self._listener_removers: list = []
        self._udp_buffer: bytearray = bytearray()  # accumulates resampled PCM

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
        """Open the UDP socket and start the Sendspin connection."""
        self._open_udp_socket()
        self._connect_task = self.hass.async_create_task(
            self._run_sendspin(), name=f"udp_lyrics_player_{self._client_id}"
        )

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the connection task and release resources."""
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
            try:
                await self._connect_task
            except (asyncio.CancelledError, Exception):
                pass
        await self._teardown_sendspin()
        self._close_udp_socket()

    # ── UDP socket helpers ────────────────────────────────────────────────────

    def _open_udp_socket(self) -> None:
        """Create a non-blocking UDP socket."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
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
        # Advertise PCM formats so the server sends raw samples (no decoder needed).
        # Preferred: exact target format; fallback: common 48 kHz stereo.
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
            buffer_capacity=512 * 1024,  # 512 KB jitter buffer
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

        # Register all event listeners and keep the removal callables.
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
                "UDP Lyrics Player '%s' connecting to Sendspin server %s",
                self._player_name,
                self._server_url,
            )
            await self._sendspin.connect(self._server_url)
            _LOGGER.info(
                "UDP Lyrics Player '%s' connected to Sendspin", self._player_name
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

    # ── Sendspin event callbacks ──────────────────────────────────────────────
    # aiosendspin expects synchronous (non-async) listener functions.

    def _on_stream_start(self, payload: Any) -> None:
        """Store stream format metadata; mark player as playing."""
        self._udp_buffer.clear()
        self._stream = {
            "codec": getattr(payload, "codec", AudioCodec.PCM),
            "sample_rate": getattr(payload, "sample_rate", 48000),
            "channels": getattr(payload, "channels", 2),
            "bit_depth": getattr(payload, "bit_depth", 16),
        }
        _LOGGER.debug("Sendspin stream started: %s", self._stream)
        self._attr_state = MediaPlayerState.PLAYING
        self.async_write_ha_state()

    def _on_audio_chunk(self, timestamp: float, data: bytes, audio_format: Any = None) -> None:
        """Receive a PCM audio chunk, resample it, and forward over UDP."""
        if not data or self._udp_sock is None:
            return
        self.hass.async_create_task(self._process_audio_chunk(data))

    async def _process_audio_chunk(self, data: bytes) -> None:
        """Resample, buffer, and forward audio over UDP in proper-sized chunks."""
        loop = asyncio.get_event_loop()
        try:
            resampled: bytes = await loop.run_in_executor(
                None,
                _resample_to_target,
                data,
                self._stream.get("sample_rate", 48000),
                self._stream.get("channels", 2),
                self._stream.get("bit_depth", 16),
            )
        except Exception as exc:
            _LOGGER.debug("Audio resampling error: %s", exc)
            return

        if not resampled:
            return

        # Accumulate resampled PCM and send in _UDP_SEND_CHUNK-sized packets
        self._udp_buffer.extend(resampled)

        try:
            dest = (self._udp_host, self._udp_port)
            while len(self._udp_buffer) >= _UDP_SEND_CHUNK:
                chunk = bytes(self._udp_buffer[:_UDP_SEND_CHUNK])
                del self._udp_buffer[:_UDP_SEND_CHUNK]
                await loop.run_in_executor(
                    None, self._udp_sock.sendto, chunk, dest
                )
        except Exception as exc:
            _LOGGER.debug("UDP send error: %s", exc)

    def _on_stream_end(self) -> None:
        """Flush remaining buffer and clear stream metadata."""
        _LOGGER.debug("Sendspin stream ended")
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
        """Send a group command to the Sendspin server (best-effort)."""
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


# ── Audio resampling (module-level, runs in executor thread) ──────────────────


def _resample_to_target(
    data: bytes,
    in_rate: int,
    in_channels: int,
    in_bit_depth: int,
) -> bytes:
    """Resample raw PCM audio to 16 kHz mono 16-bit signed little-endian PCM.

    This function is CPU-bound and is called via ``run_in_executor`` so it
    never blocks the asyncio event loop.

    Args:
        data:         Raw PCM bytes from the Sendspin server.
        in_rate:      Input sample rate in Hz (e.g. 48000).
        in_channels:  Number of input audio channels (1 = mono, 2 = stereo …).
        in_bit_depth: Bits per sample of the input (16, 24, or 32).

    Returns:
        Raw PCM bytes: signed 16-bit little-endian, mono, ``UDP_AUDIO_SAMPLE_RATE`` Hz.
        Returns ``b""`` if the input is empty or the bit depth is unsupported.
    """
    if not data:
        return b""

    # ── 1. Decode bytes → float32 normalised to [-1, 1] ──────────────────────
    if in_bit_depth == 16:
        samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0

    elif in_bit_depth == 24:
        # 24-bit PCM is stored as 3-byte little-endian signed integers.
        n = len(data) // 3
        if n == 0:
            return b""
        # Pad each triplet to 4 bytes (shift left by 8) then interpret as int32.
        padded = np.zeros(n * 4, dtype=np.uint8)
        raw = np.frombuffer(data[: n * 3], dtype=np.uint8).reshape(n, 3)
        padded_view = padded.reshape(n, 4)
        padded_view[:, 1:] = raw          # bytes 1-3 carry the 24-bit value
        samples = (
            padded.view("<i4").astype(np.float32) / 2147483648.0  # 2^31
        )

    elif in_bit_depth == 32:
        samples = np.frombuffer(data, dtype="<i4").astype(np.float32) / 2147483648.0

    else:
        _LOGGER.warning("Unsupported input bit depth: %d – chunk skipped", in_bit_depth)
        return b""

    # ── 2. Mix multi-channel audio down to mono ───────────────────────────────
    if in_channels > 1:
        # Ensure sample count is a multiple of in_channels before reshaping.
        trim = len(samples) - (len(samples) % in_channels)
        samples = samples[:trim].reshape(-1, in_channels).mean(axis=1)

    # ── 3. Resample to UDP_AUDIO_SAMPLE_RATE using linear interpolation ───────
    if in_rate != UDP_AUDIO_SAMPLE_RATE and len(samples) > 1:
        n_out = max(1, int(round(len(samples) * UDP_AUDIO_SAMPLE_RATE / in_rate)))
        x_in = np.arange(len(samples), dtype=np.float64)
        x_out = np.linspace(0.0, len(samples) - 1, n_out, dtype=np.float64)
        samples = np.interp(x_out, x_in, samples.astype(np.float64)).astype(
            np.float32
        )

    # ── 4. Encode as 16-bit signed little-endian PCM ─────────────────────────
    out = np.clip(samples * 32768.0, -32768.0, 32767.0).astype("<i2")
    return out.tobytes()
