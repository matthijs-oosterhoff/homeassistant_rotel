from __future__ import annotations

import asyncio
import logging
import re

import voluptuous as vol

import homeassistant.helpers.config_validation as cv
from homeassistant.components.media_player import (
    PLATFORM_SCHEMA,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

DOMAIN = "roteltcp"

_LOGGER = logging.getLogger(__name__)

DEFAULT_PORT = 9590
DEFAULT_NAME = "Rotel"

HEARTBEAT_INTERVAL_SECONDS = 10
HEARTBEAT_TIMEOUT_SECONDS = 5

SUPPORT_ROTEL = (
        MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_MUTE
        | MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.SELECT_SOURCE
)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
    }
)

AUDIO_SOURCES = {'phono': 'Phono', 'cd': 'CD', 'tuner': 'Tuner', 'usb': 'USB',
                 'opt1': 'Optical 1', 'opt2': 'Optical 2', 'coax1': 'Coax 1', 'coax2': 'Coax 2',
                 'bluetooth': 'Bluetooth', 'pc_usb': 'PC USB', 'aux1': 'Aux 1', 'aux2': 'Aux 2'}


async def async_setup_platform(
        hass: HomeAssistant,
        config: ConfigType,
        add_entities: AddEntitiesCallback,
        discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the Rotel platform."""
    rotel = RotelDevice(config, hass)
    add_entities([rotel], True)
    _LOGGER.debug("ROTEL: RotelDevice initialized")
    asyncio.create_task(rotel.connect())


class RotelDevice(MediaPlayerEntity):
    _attr_icon = "mdi:speaker-multiple"
    _attr_supported_features = SUPPORT_ROTEL

    def __init__(self, config, hass):
        self._attr_name = config[CONF_NAME]
        self._host = config[CONF_HOST]
        self._port = config[CONF_PORT]
        self._hass = hass
        self._transport = None
        self._protocol = None
        self._source_dict = AUDIO_SOURCES
        self._source_dict_reverse = {v: k for k, v in self._source_dict.items()}
        self._msg_buffer = ''
        self._heartbeat_task = None
        self._pending_response = None
        self._send_lock = asyncio.Lock()

    async def connect(self):
        """
        Let Home Assistant create and manage a TCP connection,
        hookup the transport protocol to our device and send an initial query to our device.
        """
        _LOGGER.info("ROTEL: initializing connection")
        if self._transport:
            self._transport.close()
            self._transport = None
            await self.async_update_ha_state(True)

        while True:
            try:
                transport, protocol = await self._hass.loop.create_connection(
                    RotelProtocol,
                    self._host,
                    self._port
                )
                _LOGGER.debug("ROTEL: Connected to device")
                break
            except Exception as e:
                _LOGGER.warning(
                    "ROTEL: Connection failed (%s), retrying in 10s...", e
                )
                await asyncio.sleep(10)

        protocol.set_device(self)
        self._transport = transport
        _LOGGER.info("ROTEL: Connection successfull.")
        self._init_heartbeat_task()
        await self.async_update_ha_state(True)

    def connection_lost(self):
        if self._transport is not None and self._transport.is_closing():
            _LOGGER.debug("ROTEL: Ignoring connection_lost, connection is closing.")
            pass
        asyncio.create_task(self.connect())

    def _init_heartbeat_task(self):
        if self._heartbeat_task is None:
            self._heartbeat_task = self._hass.loop.create_task(self._heartbeat_loop())

    async def _heartbeat_loop(self):
        """Periodically send a request to detect network loss."""
        while True:
            try:
                # Create a future to wait for response
                self._pending_response = asyncio.Future()
                await self._send_request('model?power?volume?mute?source?freq?')

                # Wait for response with timeout
                await asyncio.wait_for(self._pending_response, timeout=HEARTBEAT_TIMEOUT_SECONDS)

            except asyncio.TimeoutError:
                _LOGGER.warning("ROTEL: No response from heartbeat, reconnecting...")
                await self.connect()
            except Exception as e:
                _LOGGER.warning("ROTEL: Heartbeat failed: %s", e)
                await self.connect()
            finally:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

    async def _send_request(self, message: str):
        """Async-safe send with lock to avoid interleaving messages."""
        async with self._send_lock:
            if self._transport and not self._transport.is_closing():
                try:
                    self._transport.write(message.encode())
                    _LOGGER.debug('ROTEL: data sent: %r', message)
                except Exception as e:
                    _LOGGER.warning("ROTEL: Send failed (%s)", e)

    def send_request(self, message: str):
        """Public method for synchronous-style calls."""
        if self._hass:
            self._hass.loop.create_task(self._send_request(message))
        else:
            _LOGGER.warning("ROTEL: Hass loop not ready, cannot send message")

    @property
    def available(self) -> bool:
        """Return if device is available."""
        return self._attr_state is not None \
            and self._transport is not None \
            and not self._transport.is_closing()

    @property
    def source_list(self):
        return sorted(self._source_dict_reverse)

    def turn_off(self) -> None:
        self.send_request('power_off!')

    def turn_on(self) -> None:
        self.send_request('power_on!')

    def select_source(self, source: str) -> None:
        if source not in self._source_dict_reverse:
            _LOGGER.error(f'Selected unknown source: {source}')
        else:
            key = self._source_dict_reverse[source]
            self.send_request(f'{key}!')

    def volume_up(self) -> None:
        self.send_request('vol_up!')

    def volume_down(self) -> None:
        """Step volume down one increment."""
        self.send_request('vol_dwn!')

    def set_volume_level(self, volume: float) -> None:
        self.send_request('vol_%s!' % str(round(volume * 100)).zfill(2))

    def mute_volume(self, mute: bool) -> None:
        self.send_request('mute_%s!' % ('on' if mute else 'off'))

    def handle_incoming(self, key, value):
        _LOGGER.debug(f'ROTEL: handle incoming: {key} => {value}')

        if key == 'volume':
            self._attr_volume_level = int(value) / 100
        elif key == 'power':
            if value == 'on':
                self._attr_state = MediaPlayerState.ON
            elif value == 'standby':
                self._attr_state = MediaPlayerState.STANDBY
            else:
                self._attr_state = None
                self.send_request('power?')
        elif key == 'mute':
            if value == 'on':
                self._attr_is_volume_muted = True
            elif value == 'off':
                self._attr_is_volume_muted = False
            else:
                self._attr_is_volume_muted = None
                self.send_request('mute?')
        elif key == 'source':
            if value not in self._source_dict:
                _LOGGER.warning(f'Unknown source from receiver: {value}')
            else:
                self._attr_source = self._source_dict.get(value)
        elif key == 'freq':
            _LOGGER.debug(f'ROTEL: got freq {value}')

        # Resolve heartbeat future if waiting
        if self._pending_response and not self._pending_response.done():
            self._pending_response.set_result(True)

        # Update HA state
        self.schedule_update_ha_state()


class RotelProtocol(asyncio.Protocol):
    def __init__(self):
        self._device = None
        self._msg_buffer = ''

    def set_device(self, device):
        self._device = device

    def connection_made(self, transport):
        _LOGGER.debug('ROTEL: Transport initialized')

    def data_received(self, data):
        try:
            self._msg_buffer += data.decode()
            _LOGGER.debug('ROTEL: Data received %r', data.decode())

            commands = re.split('[$!]', self._msg_buffer)

            # check for incomplete commands
            if self._msg_buffer.endswith('$') or self._msg_buffer.endswith('!'):
                self._msg_buffer = ''
            else:
                self._msg_buffer = commands[-1]

            commands.pop(-1)

            for cmd in commands:
                if '=' in cmd:
                    key, value = cmd.split('=')
                    self._device.handle_incoming(key, value)
        except Exception:
            _LOGGER.warning('ROTEL: Data received but not ready %r', data.decode())

    def connection_lost(self, exc):
        _LOGGER.warning('ROTEL: Connection lost !')
        self._device.connection_lost()
