"""

    Bus: A package encapsulating information about buses and bus routes

    Bus and BusStop classes

    Copyright (c) 2019 Miðeind ehf.
    Author: Vilhjálmur Þorsteinsson

       This program is free software: you can redistribute it and/or modify
       it under the terms of the GNU General Public License as published by
       the Free Software Foundation, either version 3 of the License, or
       (at your option) any later version.
       This program is distributed in the hope that it will be useful,
       but WITHOUT ANY WARRANTY; without even the implied warranty of
       MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
       GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with this program.  If not, see http://www.gnu.org/licenses/.


    This module implements the Bus and BusStop classes.

"""

import os
import math
from datetime import date, datetime
import threading
import functools
from collections import defaultdict
import xml.etree.ElementTree as ET

import requests


_THIS_PATH = os.path.dirname(__file__) or "."
_STATUS_FILE = os.path.join(_THIS_PATH, "resources", "status.xml")
_STATUS_URL_FILE = os.path.join(_THIS_PATH, "config", "status_url.txt")
_STATUS_URL = open(_STATUS_URL_FILE, "r").read().strip()
_EARTH_RADIUS = 6371.0088  # Earth's radius in km
_MIDEIND_LOCATION = (64.156896, -21.951200)


def distance(loc1, loc2):
    """
    Calculate the Haversine distance.

    Parameters
    ----------
    origin : tuple of float
        (lat, long)
    destination : tuple of float
        (lat, long)

    Returns
    -------
    distance_in_km : float

    Examples
    --------
    >>> origin = (48.1372, 11.5756)  # Munich
    >>> destination = (52.5186, 13.4083)  # Berlin
    >>> round(distance(origin, destination), 1)
    504.2

    Source:
    https://stackoverflow.com/questions/19412462
        /getting-distance-between-two-points-based-on-latitude-longitude

    """
    lat1, lon1 = loc1
    lat2, lon2 = loc2

    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    slat = math.sin(dlat / 2)
    slon = math.sin(dlon / 2)
    a = (
        slat * slat +
        math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
        slon * slon
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return _EARTH_RADIUS * c


# Entfernung
entf = functools.partial(distance, _MIDEIND_LOCATION)


def locfmt(loc):
    """ Return a (lat, lon) location tuple in a standard string format """
    return f"({loc[0]:.6f},{loc[1]:.6f})"


class BusTrip:

    _all_trips = dict()

    def __init__(self, **kwargs):
        self._id = kwargs.pop("trip_id")
        self._headsign = kwargs.pop("headsign")
        self._short_name = kwargs.pop("short_name")
        self._direction = kwargs.pop("direction")
        self._block = kwargs.pop("block")
        BusTrip._all_trips[self._id] = self

    @property
    def id(self):
        return self._id

    def __str__(self):
        return f"{self._id}: {self._headsign} {self._short_name} <{self._direction}>"

    @staticmethod
    def get(trip_id):
        return BusTrip._all_trips.get(trip_id)


class BusService:

    _all_services = dict()

    def __init__(self, service_id):
        self._id = service_id
        self._trips = dict()
        # Decode year, month, date
        self._valid_from = date(
            int(service_id[0:4]),
            int(service_id[4:6]),
            int(service_id[6:8]),
        )
        # Decode weekday validity of service,
        # M T W T F S S
        self._weekdays = [
            c != "-" for c in service_id[9:16]
        ]
        BusService._all_services[service_id] = self

    @staticmethod
    def get(service_id):
        return BusService._all_services.get(service_id) or BusService(service_id)

    @property
    def id(self):
        return self._id

    @property
    def trips(self):
        return self._trips.values()

    def is_active_on_date(self, on_date):
        return self._valid_from <= on_date and self._weekdays[on_date.weekday()]

    def is_active_on_weekday(self, weekday):
        return self._weekdays[weekday]

    def add_trip(self, trip):
        self._trips[trip.id] = trip


class BusRoute:

    _all_routes = dict()

    def __init__(self, route_id):
        self._id = route_id
        self._services = dict()
        BusRoute._all_routes[route_id] = self

    def add_service(self, service):
        self._services[service.id] = service

    def active_services(self, on_date=date.today()):
        return [s for s in self._services.values() if s.is_active_on_date(on_date)]

    def __str__(self):
        return f"Route {self._id} with {len(self._services)} services"

    @staticmethod
    def get(route_id):
        return BusRoute._all_routes.get(route_id) or BusRoute(route_id)

    @staticmethod
    def initialize():
        """ Read information about bus routes from the trips.txt file """
        BusRoute._all_routes = dict()
        with open(os.path.join(_THIS_PATH, "resources", "trips.txt"), "r") as f:
            index = 0
            for line in f:
                index += 1
                if index == 1:
                    # Ignore first line
                    continue
                line = line.strip()
                if not line:
                    continue
                # Format is:
                # route_id,service_id,trip_id,trip_headsign,trip_short_name,
                # direction_id,block_id,shape_id
                f = line.split(",")
                assert len(f) == 8
                # Convert 'ST.17' to '17'
                route_id = f[0].split(".")[1]
                route = BusRoute.get(route_id)
                service = BusService.get(f[1])
                route.add_service(service)
                trip = BusTrip(
                    trip_id=f[2],
                    headsign=f[3],
                    short_name=f[4],
                    direction=f[5],
                    block=f[6],
                )
                # We don't use shape_id, f[7], for now
                service.add_trip(trip)


class Bus:

    """ The Bus class represents the current state of a bus,
        including its route identifier, its location and
        heading, its last or current stop, its next stop,
        and its status code. """

    _all_buses = None
    _timestamp = None
    _lock = threading.Lock()

    def __init__(self, **kwargs):
        self._route_id = kwargs.pop("route_id")
        self._stop = kwargs.pop("stop")
        self._next_stop = kwargs.pop("next_stop")
        # Location is a tuple of (lat, lon)
        (lat, lon) = self._location = kwargs.pop("location")
        assert -90.0 <= lat <= 90.0
        assert -180.0 <= lon <= 180.0
        self._heading = kwargs.pop("heading")
        self._code = kwargs.pop("code")
        Bus._all_buses[self._route_id].append(self)

    @staticmethod
    def all_buses():
        """ Returns a dict of lists of all known buses, keyed by route id """
        Bus.refresh_state()
        return Bus._all_buses

    @staticmethod
    def buses_on_route(route_id):
        Bus.refresh_state()
        return Bus._all_buses[route_id]

    @staticmethod
    def _fetch_state():
        """ Fetch new state via HTTP """
        r = requests.get(_STATUS_URL)
        if r is not None and r.status_code == requests.codes.ok:
            print(f"Successfully fetched state from {_STATUS_URL}")
            html_doc = r.text
            return ET.fromstring(html_doc)
        # State not available
        return None

    @staticmethod
    def _read_state():
        print(f"Reading state from {_STATUS_FILE}")
        return ET.parse(_STATUS_FILE).getroot()

    @staticmethod
    def _load_state():
        """ Loads a fresh state of all buses from the web """
        # Clear previous state
        Bus._all_buses = defaultdict(list)
        # Attempt to fetch state via HTTP
        root = Bus._fetch_state()
        if root is None:
            # Fall back to reading state from file
            root = Bus._read_state()
        for bus in root.findall('bus'):
            lat = float(bus.get('lat'))
            lon = float(bus.get('lon'))
            heading = float(bus.get('head'))
            route_id = bus.get('route')
            stop = bus.get('stop')
            next_stop = bus.get('next')
            code = int(bus.get('code'))
            Bus(
                route_id=route_id,
                location=(lat, lon),
                stop=stop,
                next_stop=next_stop,
                heading=heading,
                code=code
            )
        Bus._timestamp = datetime.utcnow()

    @staticmethod
    def refresh_state():
        """ Load a new state, if required """
        with Bus._lock:
            if Bus._timestamp is not None:
                delta = datetime.utcnow() - Bus._timestamp
                if delta.total_seconds() < 60:
                    # The state that we already have is less than
                    # a minute old: no need to refresh
                    return
            Bus._load_state()

    @property
    def route_id(self):
        return self._route_id

    @property
    def route(self):
        return BusRoute.get(self._route_id)

    @property
    def location(self):
        return self._location

    @property
    def heading(self):
        return self._heading

    @property
    def stop(self):
        return BusStop.lookup(self._stop)

    @property
    def next_stop(self):
        return BusStop.lookup(self._next_stop)

    @property
    def code(self):
        """ Bus state """
        # 1 Ekki notað
        # 2 Vagninn hefur stöðvað
        # 3 Vagninn hefur ekið af stað
        # 4 Höfuðrofi af
        #   Vagninn er ekki ræstur. Skeyti berast nú á tveggja mínútna fresti frá vagni með þessum ástæðukóða.
        # 5 Höfuðrofi settur á
        #   Vagninn ræstur. Skeyti berast nú á 15 sekúndna fresti frá vagni.
        # 6 Vagn í gangi og liðnar amk 15 sek frá síðasta skeyti.
        # 7 Komið á stöð
        return self._code
    
    @property
    def state(self):
        """ Return the entire state in one call """
        return (
            self._route_id,
            self._location,
            self._heading,
            BusStop.lookup(self._stop),
            BusStop.lookup(self._next_stop),
            self._code
        )
    

class BusStop:

    _all_stops = dict()

    def __init__(self, stop_id, name, location):
        self._id = stop_id
        self._name = name
        # Location is a tuple of (lat, lon)
        (lat, lon) = self._location = location
        assert -90.0 <= lat <= 90.0
        assert -180.0 <= lon <= 180.0
        BusStop._all_stops[stop_id] = self

    @staticmethod
    def lookup(stop_id):
        """ Return a BusStop with the given id, or None if no such stop exists """
        return BusStop._all_stops.get(stop_id)

    def __str__(self):
        return f"{self._name}"

    @property
    def stop_id(self):
        return self._id
    
    @property
    def name(self):
        return self._name

    @property
    def location(self):
        return self._location
        
    @staticmethod
    def initialize():
        """ Read information about bus stops from the stops.txt file """
        with open(os.path.join(_THIS_PATH, "resources", "stops.txt"), "r") as f:
            index = 0
            for line in f:
                index += 1
                if index == 1:
                    # Ignore first line
                    continue
                line = line.strip()
                if not line:
                    continue
                # Format is:
                # stop_id,stop_name,stop_lat,stop_lon,location_type
                f = line.split(",")
                assert len(f) == 5
                BusStop(
                    stop_id=f[0].strip(),
                    name=f[1].strip(),
                    location=(float(f[2]), float(f[3]))
                )


if __name__ == "__main__":

    BusRoute.initialize()
    BusStop.initialize()

    all_buses = Bus.all_buses().items()
    for route_id, val in sorted(all_buses, key=lambda b: b[0].rjust(2)):
        route = BusRoute.get(route_id)
        print(f"{route}:")
        for service in route.active_services():
            print(f"   service {service.id}")
        for bus in sorted(val, key=lambda bus: entf(bus.location)):
            print(
                f"   location:{locfmt(bus.location)}, head:{bus.heading:>6.2f}, "
                f"stop:{bus.stop}, next:{bus.next_stop}, code:{bus.code}, "
                f"distance:{entf(bus.location):.2f}"
            )
