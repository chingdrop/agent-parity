from agent_parity.connectors.base import AgentConnector, ConnectorError
from agent_parity.connectors.bitdefender import BitDefenderConnector
from agent_parity.connectors.carbonblack import CarbonBlackConnector
from agent_parity.connectors.sentinelone import SentinelOneConnector

# Vendor name (as used in config.yaml) -> connector class. Adding a fourth
# vendor is one new module plus one entry here.
CONNECTOR_CLASSES = {
    SentinelOneConnector.vendor: SentinelOneConnector,
    CarbonBlackConnector.vendor: CarbonBlackConnector,
    BitDefenderConnector.vendor: BitDefenderConnector,
}

__all__ = [
    "AgentConnector",
    "ConnectorError",
    "SentinelOneConnector",
    "CarbonBlackConnector",
    "BitDefenderConnector",
    "CONNECTOR_CLASSES",
]
