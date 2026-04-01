DOMAIN = "udp_lyrics_player"

# Configuration keys (also used as field labels in the UI)
CONF_PLAYER_NAME = "player_name"
CONF_SENDSPIN_SERVER_URL = "sendspin_server_url"
CONF_UDP_HOST = "udp_host"
CONF_UDP_PORT = "udp_port"

# Defaults
DEFAULT_UDP_PORT = 6056
DEFAULT_SENDSPIN_URL = "ws://192.168.1.137:8927/sendspin"

# Audio target format for UDP output (required by the lyrics recognizer)
UDP_AUDIO_SAMPLE_RATE = 16000   # Hz
UDP_AUDIO_CHANNELS = 1          # mono
UDP_AUDIO_BIT_DEPTH = 16        # bits per sample (signed PCM)

# Domain data keys
DATA_PLAYER = "player"
