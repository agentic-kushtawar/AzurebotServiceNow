from enum import Enum, auto

class Intent(Enum):
    PASSWORD_RESET = auto()
    RESET_INSTRUCTIONS = auto()
    TICKET_CREATE = auto()
    TICKET_STATUS = auto()
    VPN_HELP = auto()
    SOFTWARE_INSTALL = auto()
    HELP = auto()
    FALLBACK = auto()
