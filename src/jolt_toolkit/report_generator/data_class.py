# import necessary libraries
import dataclasses

import srf_client


# define the ServerData class
@dataclasses.dataclass
class ServerData:
    """Container for data retrieved from SRF API."""

    vehicle: srf_client.model.Vehicle
    legs: srf_client.model.Leg
    charging_events: srf_client.model.ChargerTransaction
