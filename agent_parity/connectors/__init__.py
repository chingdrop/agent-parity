from agent_parity.connectors.base import CONNECTOR_REGISTRY, AgentConnector, ConnectorError
from agent_parity.connectors.bitdefender import BitDefenderConnector
from agent_parity.connectors.carbonblack import CarbonBlackConnector
from agent_parity.connectors.sentinelone import SentinelOneConnector

# Vendor name (as used in config.yaml) -> connector class, populated by each
# connector's @register_connector decorator as its module is imported above.
# Adding a fourth vendor is one new module (decorated) plus one import line
# here — nothing else to edit.
CONNECTOR_CLASSES = CONNECTOR_REGISTRY

__all__ = [
    "AgentConnector",
    "ConnectorError",
    "SentinelOneConnector",
    "CarbonBlackConnector",
    "BitDefenderConnector",
    "CONNECTOR_CLASSES",
]
