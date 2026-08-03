import datetime

import srf_client

from jolt_toolkit.report_generator.data_class import ServerData


def fetch_events(
    vehicle_registration: str,
    date_start: datetime.datetime,
    date_end: datetime.datetime,
    srf_data: srf_client.SRFData,
) -> ServerData:

    vehicle_obj = srf_data.vehicles.get(obj_id=vehicle_registration)
    params = {
        "start_time": srf_client.filter.between(
            datetime.datetime.combine(
                date_start, datetime.time.min, datetime.timezone.utc
            ),
            datetime.datetime.combine(
                date_end, datetime.time.max, datetime.timezone.utc
            ),
        ),
        "sort": srf_client.sort.asc("startTime"),
    }

    legs = srf_data.legs.find_all(
        **params, **{"trip.vehicle.registration": vehicle_registration}
    )
    charging_events = srf_data.transactions.find_all(
        **params, **{"vehicle.registration": vehicle_registration}
    )
    return ServerData(vehicle=vehicle_obj, legs=legs, charging_events=charging_events)
