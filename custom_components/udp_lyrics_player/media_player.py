"""UDP Lyrics Player – media_player platform.

Connects to a Sendspin server as a PLAYER client so that it participates in
the synchronised playback group.  Every PCM audio chunk received from the
server is resampled to 16-bit mono 16 kHz and forwarded over UDP to the
configured destination, making the audio available to LyricsMachine's
lyrics-recognition (tagging) service, which receives it on UDP 6056.

Audio pipeline
--------------
Sendspin server  →  aiosendspin client  →  av.AudioFrame   →  PyAV resample
    (any PCM)            (WebSocket)       → interleaved    → 16 kHz s16le mono
                                                             → UDP socket
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import random
import socket
import struct
import time
from pathlib import Path
from typing import Any

import av
from aiosendspin.client import SendspinClient
from aiosendspin.models import (
    AudioCodec,
    DeviceInfo,
    MediaCommand,
    PlayerCommand,
    Roles,
)
from aiosendspin.models.types import GoodbyeReason
from aiosendspin.models.player import (
    ClientHelloPlayerSupport,
    SupportedAudioFormat,
)
from aiosendspin.noise.keys import Identity, b64url_decode
from aiosendspin.noise.trust_store import FileClientPairingStore

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_PLAYER_NAME,
    CONF_SENDSPIN_SERVER_URL,
    CONF_UDP_HOST,
    CONF_UDP_PORT,
    DOMAIN,
    UDP_AUDIO_SAMPLE_RATE,
)

_LOGGER = logging.getLogger(__name__)

# UDP send chunk: 1024 frames * 2 bytes/frame = 2048 bytes.
_UDP_SEND_CHUNK = 2048

# RTP (RFC 3550) constants.
# Payload type 96 is the first dynamic slot — used here for L16 mono 16 kHz.
# The clock rate equals the audio sample rate (16 000 Hz), so the timestamp
# increments by the number of PCM samples contained in each packet.
_RTP_PAYLOAD_TYPE = 96
_RTP_SAMPLES_PER_PACKET = _UDP_SEND_CHUNK // 2  # 1024 samples @ 16-bit mono
_RTP_EXT_PROFILE_ONE_BYTE = 0xBEDE
_RTP_EXT_PROFILE_TWO_BYTE = 0x1000
_RTP_EXT_ID_MA_PLAYER_NAME = 1
_RTP_EXT_ID_MA_PLAYER_ID = 2
_RTP_EXT_BURST_PACKET_COUNT = 5
_RTP_EXT_HEARTBEAT_SECONDS = 2.0

# How often to re-report client-level availability to the Sendspin server
# while connected. The RTP extension heartbeat above only reaches the
# SyncLyrics UDP receiver; this separate Sendspin-connection heartbeat keeps
# Music Assistant's view of the player fresh so it is never marked stale.
_PLAYER_STATE_HEARTBEAT_SECONDS = 2.0

# Reconnect backoff bounds (seconds). The Sendspin server is often unreachable
# for a short window after a Home Assistant restart, so we keep retrying with
# exponential backoff instead of giving up after a single failed connect.
_RECONNECT_BACKOFF_INITIAL = 2.0
_RECONNECT_BACKOFF_MAX = 60.0


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

        # Sendspin >= 5 identifies a client by its long-term X25519 public key
        # rather than a caller-chosen string, so client_id is only known once the
        # identity has been loaded from (or written to) disk. Music Assistant uses
        # this same value as its player_id, and it is what goes out in the RTP
        # player-id extension so the UDP receiver can correlate the two.
        self._identity: Identity | None = None
        self._pairing_store: FileClientPairingStore | None = None
        self._client_id: str = ""

        # HA entity attributes
        self._attr_unique_id = config_entry.entry_id
        self._attr_state = MediaPlayerState.IDLE
        # Unavailable until the Sendspin handshake completes. A wrong server URL
        # or an unreachable server otherwise leaves the entity looking healthy
        # and idle in HA while the player never registers with Music Assistant.
        self._attr_available = False
        # Logical player volume exposed to HA/Sendspin/MA. This is deliberately
        # not applied as gain in the UDP PCM/RTP path so the lyrics/listening
        # service always receives full-scale audio.
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
        self._state_heartbeat_task: asyncio.Task | None = None
        self._listener_removers: list = []
        # Set whenever a reconnect is requested (initial start, or after a
        # server-initiated disconnect). The connect loop waits on this between
        # attempts so callbacks can trigger an immediate retry.
        self._reconnect_event: asyncio.Event = asyncio.Event()

        # Audio pipeline state — only touched by the single worker task.
        self._audio_queue: asyncio.Queue[tuple[int, bytes, Any]] = asyncio.Queue()
        self._udp_buffer: bytearray = bytearray()
        self._in_buffer: bytearray = bytearray()
        self._resampler: av.AudioResampler | None = None

        # RTP session state — reset on every stream_start.
        # Initialised to zero here; _reset_rtp_state() assigns random values
        # before the first packet is ever sent.
        self._rtp_seq: int = 0
        self._rtp_ts: int = 0
        self._rtp_ssrc: int = 0
        self._rtp_first_packet: bool = True
        self._rtp_packets_sent: int = 0
        self._rtp_next_ext_heartbeat_monotonic: float = 0.0
        self._rtp_player_name_bytes: bytes = b""
        self._rtp_player_id_bytes: bytes = b""

    # ── Device info ───────────────────────────────────────────────────────────

    @property
    def device_info(self) -> dict:
        return {
            "identifiers": {(DOMAIN, self._config_entry.entry_id)},
            "name": self._player_name,
            "manufacturer": "LyricsMachine",
            "model": "UDP Lyrics Player",
            "sw_version": "1.0.0",
        }

    # ── HA lifecycle hooks ────────────────────────────────────────────────────

    async def async_added_to_hass(self) -> None:
        """Open the UDP socket, start the audio worker, and connect."""
        self._open_udp_socket()
        # Background tasks: infinite loops that must not block HA bootstrap.
        # async_create_task is tracked by setup and would trigger a 60s
        # "setup timed out" warning when these loops never complete.
        entry_id = self._config_entry.entry_id
        self._worker_task = self.hass.async_create_background_task(
            self._audio_worker_loop(),
            name=f"udp_lyrics_worker_{entry_id}",
        )
        self._connect_task = self.hass.async_create_background_task(
            self._run_sendspin(),
            name=f"udp_lyrics_conn_{entry_id}",
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

    # ── Availability ──────────────────────────────────────────────────────────

    def _set_available(self, available: bool) -> None:
        """Publish a change in Sendspin connectivity as HA entity availability.

        Safe to call from the connect loop and from aiosendspin callbacks: it is
        a no-op when nothing changed, and while the entity is not (yet) attached
        to hass it updates the attribute without writing state.
        """
        if self._attr_available == available:
            return
        self._attr_available = available
        if self.hass is not None and self.entity_id:
            self.async_write_ha_state()

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
        self._rtp_packets_sent = 0
        self._rtp_next_ext_heartbeat_monotonic = (
            time.monotonic() + _RTP_EXT_HEARTBEAT_SECONDS
        )
        self._rtp_player_name_bytes = self._encode_utf8_at_char_boundary(
            self._player_name, 255
        )
        self._rtp_player_id_bytes = self._encode_utf8_at_char_boundary(
            self._client_id, 255
        )

    def _encode_utf8_at_char_boundary(self, text: str, max_bytes: int) -> bytes:
        """UTF-8 encode *text* and truncate to *max_bytes* without splitting characters."""
        if max_bytes <= 0:
            return b""
        out = bytearray()
        for ch in text:
            encoded = ch.encode("utf-8")
            if len(out) + len(encoded) > max_bytes:
                break
            out.extend(encoded)
        return bytes(out)

    def _build_rtp_extension(self, elements: list[tuple[int, bytes]]) -> bytes:
        """Build RFC 8285 one-byte or two-byte RTP header extension payload."""
        valid = [
            (ext_id, data)
            for ext_id, data in elements
            if 1 <= ext_id <= 14 and 1 <= len(data) <= 255
        ]
        if not valid:
            return b""

        use_two_byte_form = any(len(data) > 16 for _, data in valid)
        body = bytearray()
        if use_two_byte_form:
            for ext_id, data in valid:
                body.append(ext_id)
                body.append(len(data))
                body.extend(data)
            profile = _RTP_EXT_PROFILE_TWO_BYTE
        else:
            for ext_id, data in valid:
                body.append((ext_id << 4) | (len(data) - 1))
                body.extend(data)
            profile = _RTP_EXT_PROFILE_ONE_BYTE

        while len(body) % 4:
            body.append(0x00)
        return struct.pack("!HH", profile, len(body) // 4) + bytes(body)

    def _should_send_rtp_extension(self) -> bool:
        """Send extension in initial burst and then heartbeat cadence."""
        if self._rtp_packets_sent < _RTP_EXT_BURST_PACKET_COUNT:
            return True
        now = time.monotonic()
        if now >= self._rtp_next_ext_heartbeat_monotonic:
            self._rtp_next_ext_heartbeat_monotonic = (
                now + _RTP_EXT_HEARTBEAT_SECONDS
            )
            return True
        return False

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

        * V=2, P=0, CC=0 with optional X when metadata extension is present.
        * Marker bit is set on the very first packet of a stream (talk-spurt
          start), then cleared for the remainder per RFC 3551 §4.1.
        * Payload type 96 (dynamic) for L16 mono 16 kHz.
        * Timestamp clock runs at the audio sample rate (16 000 Hz); it is
          advanced by the number of PCM samples contained in *payload*.
        * Sequence number wraps at 65 535 → 0 as required by the RFC.
        """
        marker = 1 if self._rtp_first_packet else 0
        self._rtp_first_packet = False
        include_extension = self._should_send_rtp_extension()
        ext = b""
        if include_extension:
            ext = self._build_rtp_extension(
                [
                    (_RTP_EXT_ID_MA_PLAYER_NAME, self._rtp_player_name_bytes),
                    (_RTP_EXT_ID_MA_PLAYER_ID, self._rtp_player_id_bytes),
                ]
            )
            if not ext:
                include_extension = False

        header = struct.pack(
            "!BBHII",
            0x80 | (0x10 if include_extension else 0x00),
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
        self._rtp_packets_sent += 1

        return header + ext + payload

    # ── Sendspin connection ───────────────────────────────────────────────────

    async def _run_sendspin(self) -> None:
        """Connect to the Sendspin server and keep retrying on failure.

        On Home Assistant startup the Sendspin server is frequently not yet
        reachable when entities are added. Instead of giving up after a single
        attempt (which previously required the user to press *Reload* on the
        integration), we wait for HA to finish starting and then loop with
        exponential backoff. The same loop also drives reconnection after a
        server-initiated disconnect.
        """
        # Wait until Home Assistant has finished starting so we don't race the
        # Sendspin server, the network stack, or DNS coming up.
        if self.hass.state is not CoreState.running:
            started = asyncio.Event()

            def _on_started(_event):
                started.set()

            unsub = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, _on_started
            )
            try:
                await started.wait()
            finally:
                # async_listen_once self-removes after firing, so only unsub
                # if we're bailing out before the event arrived (e.g. cancel).
                if not started.is_set():
                    unsub()

        backoff = _RECONNECT_BACKOFF_INITIAL
        while True:
            try:
                await self._connect_once()
                # Connected successfully — reset backoff and wait for a
                # disconnect-triggered reconnect request.
                backoff = _RECONNECT_BACKOFF_INITIAL
                self._reconnect_event.clear()
                await self._reconnect_event.wait()
                self._reconnect_event.clear()
                # Drop the dead client before reconnecting.
                await self._teardown_sendspin()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _LOGGER.warning(
                    "UDP Lyrics Player '%s' connect to %s failed: %s "
                    "(retrying in %.0fs)",
                    self._player_name,
                    self._server_url,
                    exc,
                    backoff,
                )
                self._set_available(False)
                await self._teardown_sendspin()
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
                backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)

    def _storage_dir(self) -> Path:
        """Return this entry's private directory for identity and pairing state."""
        return Path(
            self.hass.config.path(".storage", DOMAIN, self._config_entry.entry_id)
        )

    @staticmethod
    def _load_or_create_identity(storage_dir: Path) -> Identity:
        """Load this player's long-term identity, generating one only if absent.

        Blocking (file I/O) — call from an executor. A corrupt key file raises
        rather than silently minting a new identity: a new key is a new
        client_id, which Music Assistant would see as a brand new player.
        """
        storage_dir.mkdir(parents=True, exist_ok=True)
        key_path = storage_dir / "identity.key"
        try:
            return Identity.from_private_bytes(
                b64url_decode(key_path.read_text().strip())
            )
        except FileNotFoundError:
            pass
        identity = Identity.generate()
        try:
            fd = os.open(key_path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
        except FileExistsError:
            # Lost a race with another writer; adopt whatever landed on disk.
            return Identity.from_private_bytes(
                b64url_decode(key_path.read_text().strip())
            )
        with os.fdopen(fd, "w") as handle:
            handle.write(identity.private_b64u)
        return identity

    async def _ensure_identity(self) -> None:
        """Load identity and pairing store once, and advertise guest access.

        Music Assistant approves a client without any pairing step only when the
        hello advertises unpaired access (`_auto_trust_guest_access`). This player
        carries playback and nothing else, so there is nothing for the user to
        decide — enable it so the player appears without a PIN exchange.
        """
        if self._identity is not None and self._pairing_store is not None:
            return

        storage_dir = self._storage_dir()
        identity = await self.hass.async_add_executor_job(
            self._load_or_create_identity, storage_dir
        )
        pairing_store = await FileClientPairingStore.open(
            storage_dir / "pairing_store.json"
        )

        config = await pairing_store.get_pairing_config()
        if not config.unpaired_access_enabled:
            await pairing_store.store_pairing_config(
                dataclasses.replace(config, unpaired_access_enabled=True)
            )

        self._identity = identity
        self._pairing_store = pairing_store
        self._client_id = identity.peer_id

    async def _connect_once(self) -> None:
        """Build a fresh SendspinClient and open the connection."""
        await self._ensure_identity()
        assert self._identity is not None and self._pairing_store is not None

        # Advertise ONLY group-compatible formats. A Sendspin sync group plays
        # one shared encoded stream to every member, so MA must pick a single
        # format that every member supports. Real speakers (Waveshare,
        # reSpeaker XVF3800) stream CD/48k stereo PCM; none can play 16 kHz
        # mono. The 16 kHz mono profile is purely our internal UDP-output
        # concern — _process_chunk() downconverts whatever input format we
        # receive to 16 kHz mono before sending over UDP — so it must NOT be
        # advertised here. Offering a format no real speaker supports prevents
        # MA from finding a common sync format and blocks grouping
        # ("can not be grouped with respeaker_lyrics").
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
        ]

        player_support = ClientHelloPlayerSupport(
            supported_formats=supported_formats,
            buffer_capacity=512 * 1024,
            supported_commands=[PlayerCommand.VOLUME, PlayerCommand.MUTE],
        )

        self._sendspin = SendspinClient(
            self._identity,
            self._player_name,
            [Roles.PLAYER],
            pairing_store=self._pairing_store,
            player_support=player_support,
            device_info=DeviceInfo(
                product_name="UDP Lyrics Player",
                manufacturer="LyricsMachine",
                software_version="1.0.0",
            ),
            initial_volume=self._logical_volume_percent,
            initial_muted=self._attr_is_volume_muted,
            # state_supported_commands is deliberately unset: it advertises
            # 'set_static_delay' only (volume/mute belong to player_support
            # above), and this player exposes no server-settable static delay.
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
        self._set_available(True)

        # The SDK sends full client state itself once the server activates our
        # player role (and again on every reactivation), so all this needs to do
        # is keep Music Assistant's view fresh while we sit in a sync group.
        await self._report_availability()
        self._state_heartbeat_task = self.hass.async_create_background_task(
            self._player_state_heartbeat_loop(),
            name=f"udp_lyrics_state_hb_{self._config_entry.entry_id}",
        )

    async def _teardown_sendspin(self) -> None:
        """Remove listeners and disconnect gracefully."""
        if self._state_heartbeat_task and not self._state_heartbeat_task.done():
            self._state_heartbeat_task.cancel()
            try:
                await self._state_heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass
        self._state_heartbeat_task = None

        for remove_fn in self._listener_removers:
            if callable(remove_fn):
                try:
                    remove_fn()
                except Exception:
                    pass
        self._listener_removers.clear()

        if self._sendspin is not None:
            try:
                # disconnect() announces the goodbye itself, so the server drops
                # us immediately instead of holding the client for its delayed
                # reconnect grace period.
                await self._sendspin.disconnect(GoodbyeReason.SHUTDOWN)
            except Exception as exc:
                _LOGGER.debug("Error during Sendspin disconnect: %s", exc)
            self._sendspin = None

    # ── Player-state reporting ────────────────────────────────────────────────

    @property
    def _logical_volume_percent(self) -> int:
        """Return the logical HA/Sendspin volume as an integer percent."""
        return round(self._attr_volume_level * 100)

    @staticmethod
    def _coerce_logical_volume_level(volume: Any) -> float:
        """Normalize a HA fraction or Sendspin percent volume to a 0..1 level."""
        raw_volume = float(volume)
        if raw_volume > 1.0:
            raw_volume /= 100.0
        return max(0.0, min(1.0, raw_volume))

    async def _report_player_state(self) -> None:
        """Report this player's client state to the Sendspin server.

        ``send_player_state`` carries client availability (always True while we
        are operational) together with logical volume/mute. It does not carry
        play/pause — transport state is server-owned — but sending it on every
        transition and on a heartbeat resets Music Assistant's freshness clock
        for this player so it is never reported as stale/idle. The logical volume
        reported here is not applied to UDP audio samples.
        """
        if self._sendspin and self._sendspin.connected:
            try:
                volume = self._logical_volume_percent
                _LOGGER.debug(
                    "Reporting Sendspin player state for '%s': volume=%s muted=%s",
                    self._player_name,
                    volume,
                    self._attr_is_volume_muted,
                )
                await self._sendspin.send_player_state(
                    available=True,
                    volume=volume,
                    muted=self._attr_is_volume_muted,
                )
            except Exception as exc:
                _LOGGER.debug("send_player_state error: %s", exc)

    def _schedule_player_state_report(self) -> None:
        """Fire-and-forget a player-state report from a sync callback context."""
        if self._sendspin and self._sendspin.connected:
            self.hass.async_create_task(self._report_player_state())

    async def _report_availability(self) -> None:
        """Report client-level availability to the Sendspin server.

        Deliberately not ``send_player_state``: that carries a player object,
        which the server rejects as non-compliant whenever it has not activated
        our player role — true for most of the time a player sits idle. Client
        availability is role-agnostic and is the spec's way to say "still here".
        """
        if self._sendspin and self._sendspin.connected:
            try:
                await self._sendspin.send_available(available=True)
            except Exception as exc:
                _LOGGER.debug("send_available error: %s", exc)

    async def _player_state_heartbeat_loop(self) -> None:
        """Periodically re-report availability so MA never marks us stale."""
        while True:
            try:
                await asyncio.sleep(_PLAYER_STATE_HEARTBEAT_SECONDS)
                await self._report_availability()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                _LOGGER.debug("Player state heartbeat error: %s", exc)

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
                timestamp, data, pcm_format = await self._audio_queue.get()

                if self._udp_sock is None:
                    continue

                # Mix to mono and resample — all in an executor thread
                pcm_out: bytes = await loop.run_in_executor(
                    None, self._process_chunk, data, pcm_format
                )

                if not pcm_out:
                    continue

                # Synchronize playback to Server target play time
                if self._sendspin is not None:
                    try:
                        target_client_time_us = self._sendspin.compute_play_time(int(timestamp))
                        # Ask the client for "now" on its own clock rather than
                        # assuming it is time.monotonic() — the SDK owns its clock
                        # source and the two need not share a base.
                        now_us = self._sendspin.now_us()
                        delay_sec = (target_client_time_us - now_us) / 1_000_000.0
                        if delay_sec > 0:
                            await asyncio.sleep(delay_sec)
                    except Exception as exc:
                        _LOGGER.debug("Audio play timing error: %s", exc)

                self._udp_buffer.extend(pcm_out)

                # Drain buffer in _UDP_SEND_CHUNK-sized RTP packets
                while len(self._udp_buffer) >= _UDP_SEND_CHUNK:
                    chunk = bytes(self._udp_buffer[:_UDP_SEND_CHUNK])
                    del self._udp_buffer[:_UDP_SEND_CHUNK]
                    self._udp_sock.sendto(self._make_rtp_packet(chunk), dest)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                _LOGGER.debug("Audio worker error: %s", exc)

    def _process_chunk(self, data: bytes, pcm_format: Any) -> bytes:
        """Mix decoded PCM to mono, resample via PyAV, and encode to s16le.

        Called from an executor thread by the single worker task. ``pcm_format``
        is the ``PCMFormat`` aiosendspin attached to this chunk: from Sendspin 5
        the SDK decodes the stream itself (PCM and FLAC), so the chunk is always
        raw PCM and its shape is described per chunk rather than inferred from
        the stream/start payload.
        """
        if self._resampler is None or pcm_format is None:
            return b""

        in_rate = pcm_format.sample_rate
        in_channels = pcm_format.channels
        in_bit_depth = pcm_format.bit_depth

        # Convert bit_depth to PyAV format
        if in_bit_depth == 16:
            in_format = "s16"
            bytes_per_sample = 2
        elif in_bit_depth == 32:
            in_format = "s32"
            bytes_per_sample = 4
        else:
            # Only 16-bit formats are advertised in client/hello, so anything
            # else means the server ignored our supported_formats list.
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

    def _on_stream_start(self, message: Any) -> None:
        """Reset the audio pipeline and create a fresh PyAV resampler.

        The stream/start payload is no longer parsed for the input format: from
        Sendspin 5 every audio chunk carries its own decoded ``PCMFormat``, which
        is authoritative and removes the guesswork this used to do over dict and
        attribute shapes.
        """
        self._udp_buffer.clear()
        self._in_buffer.clear()
        self._stream = {}

        # Drain stale chunks from a previous stream
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        # Create a fresh PyAV streaming resampler
        self._resampler = av.AudioResampler(
            format="s16", layout="mono", rate=UDP_AUDIO_SAMPLE_RATE
        )

        # New stream → new RTP session (fresh sequence number, timestamp, SSRC)
        self._reset_rtp_state()

        _LOGGER.debug("Sendspin stream started")
        self._attr_state = MediaPlayerState.PLAYING
        self.async_write_ha_state()
        self._schedule_player_state_report()

    def _on_audio_chunk(
        self, timestamp: int, data: bytes, audio_format: Any = None
    ) -> None:
        """Queue the incoming audio chunk, with its format, for the worker."""
        if not data or self._udp_sock is None:
            return
        pcm_format = getattr(audio_format, "pcm_format", None)
        if pcm_format is None:
            _LOGGER.debug("Dropping audio chunk without a PCM format")
            return
        if not self._stream:
            self._stream = {
                "codec": getattr(audio_format, "codec", AudioCodec.PCM),
                "sample_rate": pcm_format.sample_rate,
                "channels": pcm_format.channels,
                "bit_depth": pcm_format.bit_depth,
            }
            _LOGGER.debug("Sendspin stream format: %s", self._stream)
        self._audio_queue.put_nowait((int(timestamp), data, pcm_format))

    def _on_stream_end(self, roles: Any = None) -> None:
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

        # Send whatever remains in the UDP buffer as a final RTP packet
        if self._udp_buffer and self._udp_sock is not None:
            try:
                dest = (self._udp_host, self._udp_port)
                self._udp_sock.sendto(
                    self._make_rtp_packet(bytes(self._udp_buffer)), dest
                )
            except Exception as exc:
                _LOGGER.debug("UDP flush error: %s", exc)
        self._udp_buffer.clear()
        self._in_buffer.clear()
        self._stream = {}

        # The audio stream stops on both pause and stop, and the stream-end
        # event itself can't distinguish them. Default to PAUSED so MA's view
        # of this player leaves PLAYING immediately; an authoritative
        # _on_group_update (if one arrives) will correct this to IDLE when the
        # group actually stopped. Only downgrade from an active PLAYING state
        # so we don't clobber an already-IDLE entity.
        if self._attr_state == MediaPlayerState.PLAYING:
            self._attr_state = MediaPlayerState.PAUSED
            self.async_write_ha_state()
        self._schedule_player_state_report()

    def _on_group_update(self, state: Any) -> None:
        """Sync HA state with the Sendspin group playback state."""
        try:
            raw = (
                state.get("state") if isinstance(state, dict)
                else getattr(state, "state", None)
            )
            if raw is None:
                return
            # A group update is authoritative for transport state when present.
            raw_upper = str(raw).upper()
            new_state = self._attr_state
            if "PLAYING" in raw_upper:
                new_state = MediaPlayerState.PLAYING
            elif "PAUSED" in raw_upper:
                new_state = MediaPlayerState.PAUSED
            elif "STOPPED" in raw_upper or "IDLE" in raw_upper:
                new_state = MediaPlayerState.IDLE

            changed = new_state != self._attr_state
            self._attr_state = new_state
            self.async_write_ha_state()
            if changed:
                self._schedule_player_state_report()
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
        """Apply logical volume / mute commands sent by the Sendspin server."""
        try:
            # aiosendspin server/command payloads wrap player commands in a
            # nested ``player`` object: {player: {command, volume|mute}}. Keep
            # direct-key fallbacks for older/untyped payloads seen in the wild.
            player = (
                payload.get("player")
                if isinstance(payload, dict)
                else getattr(payload, "player", None)
            )
            command_payload = player if player is not None else payload
            volume = (
                command_payload.get("volume")
                if isinstance(command_payload, dict)
                else getattr(command_payload, "volume", None)
            )
            muted = (
                command_payload.get("mute", command_payload.get("muted"))
                if isinstance(command_payload, dict)
                else getattr(
                    command_payload,
                    "mute",
                    getattr(command_payload, "muted", None),
                )
            )
            if volume is not None:
                self._attr_volume_level = self._coerce_logical_volume_level(volume)
                _LOGGER.debug(
                    "Received Sendspin volume command for '%s': raw=%s logical=%s%%",
                    self._player_name,
                    volume,
                    self._logical_volume_percent,
                )
            if muted is not None:
                self._attr_is_volume_muted = bool(muted)
            if volume is not None or muted is not None:
                self.async_write_ha_state()
                self._schedule_player_state_report()
        except Exception as exc:
            _LOGGER.debug("Server command error: %s", exc)

    def _on_disconnect(self, reason: Any = None, *args: Any, **kwargs: Any) -> None:
        """Handle server-initiated disconnection by triggering a reconnect.

        aiosendspin invokes this callback with no arguments in some versions
        and with a reason string in others, so accept either shape — raising
        a TypeError here would prevent the reconnect event from being set.
        """
        try:
            _LOGGER.warning(
                "UDP Lyrics Player '%s' disconnected from Sendspin: %s",
                self._player_name,
                reason,
            )
            self._attr_state = MediaPlayerState.IDLE
            self._attr_available = False
            self._stream = {}
            self.async_write_ha_state()
        finally:
            # Always wake the connect loop so it tears down this client and
            # reconnects, even if state bookkeeping above raised.
            self._reconnect_event.set()

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
        await self._report_player_state()

    async def async_media_pause(self) -> None:
        await self._send_group_cmd(MediaCommand.PAUSE)
        self._attr_state = MediaPlayerState.PAUSED
        self.async_write_ha_state()
        await self._report_player_state()

    async def async_media_stop(self) -> None:
        await self._send_group_cmd(MediaCommand.STOP)
        self._attr_state = MediaPlayerState.IDLE
        self.async_write_ha_state()
        await self._report_player_state()

    async def async_media_next_track(self) -> None:
        await self._send_group_cmd(MediaCommand.NEXT)

    async def async_media_previous_track(self) -> None:
        await self._send_group_cmd(MediaCommand.PREVIOUS)

    async def async_set_volume_level(self, volume: float) -> None:
        self._attr_volume_level = self._coerce_logical_volume_level(volume)
        _LOGGER.debug(
            "Received HA volume command for '%s': raw=%s logical=%s%%",
            self._player_name,
            volume,
            self._logical_volume_percent,
        )
        await self._report_player_state()
        self.async_write_ha_state()

    async def async_mute_volume(self, mute: bool) -> None:
        self._attr_is_volume_muted = mute
        await self._report_player_state()
        self.async_write_ha_state()
