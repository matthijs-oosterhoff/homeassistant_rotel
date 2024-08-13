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
    """Set up the NAD platform."""

    rotel = RotelDevice(config)
    add_entities([rotel], True)
    _LOGGER.debug("ROTEL: RotelDevice initialized")
    await rotel.start(hass)


class RotelDevice(MediaPlayerEntity):
    _attr_icon = "mdi:speaker-multiple"
    _attr_supported_features = SUPPORT_ROTEL

    def __init__(self, config):
        self._attr_name = config[CONF_NAME]
        self._host = config[CONF_HOST]
        self._port = config[CONF_PORT]
        self._transport = None
        self._source_dict = AUDIO_SOURCES
        self._source_dict_reverse = {value: key for key, value in self._source_dict.items()}
        self._msg_buffer = ''

    async def start(self, hass):
        """
        Let Home Assistant create and manage a TCP connection,
        hookup the transport protocol to our device and send an initial query to our device.
        """
        transport, protocol = await hass.loop.create_connection(
            RotelProtocol,
            self._host,
            self._port
        )
        protocol.set_device(self)
        self._transport = transport
        self.send_request('model?power?volume?mute?source?freq?')
        _LOGGER.debug("ROTEL: started.")

    def send_request(self, message):
        """
        Send messages to the amp (which is a bit cheeky and may need a hard reset if command
        was not properly formatted
        """
        try:
            self._transport.write(message.encode())
            _LOGGER.debug('ROTEL: data sent: {!r}'.format(message))
        except:
            _LOGGER.warning('ROTEL: transport not ready !')

    @property
    def available(self) -> bool:
        """Return if device is available."""
        # return self.state is not None
        return self._attr_state is not None \
            and self._transport is not None \
            and not self._transport.is_closing()

    @property
    def source_list(self):
        """List of available input sources."""
        return sorted(self._source_dict_reverse)

    def turn_off(self) -> None:
        """Turn the media player off."""
        self.send_request('power_off!')

    def turn_on(self) -> None:
        """Turn the media player on."""
        self.send_request('power_on!')

    def select_source(self, source: str) -> None:
        """Select input source."""
        if source not in self._source_dict_reverse:
            _LOGGER.error(f'Selected unknown source: {source}')
        else:
            key = self._source_dict_reverse.get(source)
            self.send_request(f'{key}!')

    def volume_up(self) -> None:
        """Step volume up one increment."""
        self.send_request('vol_up!')

    def volume_down(self) -> None:
        """Step volume down one increment."""
        self.send_request('vol_dwn!')

    def set_volume_level(self, volume: float) -> None:
        """Set volume level, range 0..1."""
        self.send_request('vol_%s!' % str(round(volume * 100)).zfill(2))

    def mute_volume(self, mute: bool) -> None:
        """Mute (true) or unmute (false) media player."""
        self.send_request('mute_%s!' % (mute is True and 'on' or 'off'))

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
            # TODO
            _LOGGER.debug(f'got freq {value}')


class RotelProtocol(asyncio.Protocol):

    def __init__(self):
        self._device = None
        self._msg_buffer = ''

    def set_device(self, device):
        self._device = device

    def connection_made(self, transport):
        _LOGGER.debug('ROTEL: Transport initialized')

    def data_received(self, data):
        _LOGGER.debug('ROTEL: Data received {!r}'.format(data.decode()))
        try:
            self._msg_buffer += data.decode()
            _LOGGER.debug(f'ROTEL: msg buffer: {self._msg_buffer}')

            # According to the spec, all status updates are terminated with '$',
            # In practice, it seems some status updates are terminated with '!', for example 'network_status=connected!'
            commands = re.split('[$!]', self._msg_buffer)

            # check for incomplete commands
            if self._msg_buffer.endswith('$') or self._msg_buffer.endswith('!'):
                # command terminated properly, clear buffer
                self._msg_buffer = ''
            else:
                # last command not terminated, put it back on the buffer
                self._msg_buffer = commands[-1]

            # last item is either empty or an unterminated command, get rid of it.
            commands.pop(-1)

            #  update internal state depending on amp messages
            for cmd in commands:
                key, value = cmd.split('=')
                self._device.handle_incoming(key, value)

            # make sure internal state is propagated to the UI
            self._device.schedule_update_ha_state()
        except:
            _LOGGER.warning('ROTEL: Data received but not ready {!r}'.format(data.decode()))

    def connection_lost(self, exc):
        _LOGGER.warning('ROTEL: Connection Lost !')
