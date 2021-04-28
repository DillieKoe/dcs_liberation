import logging
from typing import List, Optional, Tuple

from PySide2.QtCore import Property, QObject, Signal, Slot
from dcs import Point

from game import Game
from game.profiling import logged_duration
from game.theater import (
    ConflictTheater,
    ControlPoint,
    TheaterGroundObject,
)
from gen.ato import AirTaskingOrder
from gen.flights.flight import Flight, FlightWaypoint
from qt_ui.models import GameModel
from qt_ui.windows.GameUpdateSignal import GameUpdateSignal
from qt_ui.windows.basemenu.QBaseMenu2 import QBaseMenu2

LeafletLatLon = List[float]


class ControlPointJs(QObject):
    def __init__(
        self,
        control_point: ControlPoint,
        game_model: GameModel,
        theater: ConflictTheater,
    ) -> None:
        super().__init__()
        self.control_point = control_point
        self.game_model = game_model
        self.theater = theater

    @Property(str)
    def name(self) -> str:
        return self.control_point.name

    @Property(bool)
    def blue(self) -> bool:
        return self.control_point.captured

    @Property(list)
    def position(self) -> LeafletLatLon:
        ll = self.theater.point_to_ll(self.control_point.position)
        return [ll.latitude, ll.longitude]

    @Slot()
    def open_base_menu(self) -> None:
        self.base_details_dialog = QBaseMenu2(None, self.control_point, self.game_model)
        self.base_details_dialog.show()


class GroundObjectJs(QObject):
    def __init__(self, tgo: TheaterGroundObject, theater: ConflictTheater) -> None:
        super().__init__()
        self.tgo = tgo
        self.theater = theater

    @Property(bool)
    def blue(self) -> bool:
        return self.tgo.control_point.captured

    @Property(list)
    def position(self) -> LeafletLatLon:
        ll = self.theater.point_to_ll(self.tgo.position)
        return [ll.latitude, ll.longitude]

    @Property(list)
    def samThreatRanges(self) -> List[float]:
        if not self.tgo.might_have_aa:
            return []

        ranges = []
        for group in self.tgo.groups:
            threat_range = self.tgo.threat_range(group)
            if threat_range:
                ranges.append(threat_range.meters)
        return ranges

    @Property(list)
    def samDetectionRanges(self) -> List[float]:
        if not self.tgo.might_have_aa:
            return []

        ranges = []
        for group in self.tgo.groups:
            detection_range = self.tgo.detection_range(group)
            if detection_range:
                ranges.append(detection_range.meters)
        return ranges


class SupplyRouteJs(QObject):
    def __init__(self, points: List[LeafletLatLon]) -> None:
        super().__init__()
        self._points = points

    @Property(list)
    def points(self) -> List[LeafletLatLon]:
        return self._points


class WaypointJs(QObject):
    def __init__(self, waypoint: FlightWaypoint, theater: ConflictTheater) -> None:
        super().__init__()
        self.waypoint = waypoint
        self.theater = theater

    @Property(list)
    def position(self) -> LeafletLatLon:
        ll = self.theater.point_to_ll(self.waypoint.position)
        return [ll.latitude, ll.longitude]


class FlightJs(QObject):
    flightPlanChanged = Signal()

    def __init__(
        self, flight: Flight, selected: bool, theater: ConflictTheater
    ) -> None:
        super().__init__()
        self.flight = flight
        self._selected = selected
        self.theater = theater
        self._waypoints = []
        self.reset_waypoints()

    def reset_waypoints(self) -> None:
        self._waypoints = [WaypointJs(p, self.theater) for p in self.flight.points]
        self.flightPlanChanged.emit()

    @Property(list, notify=flightPlanChanged)
    def flightPlan(self) -> List[WaypointJs]:
        return self._waypoints

    @Property(bool)
    def blue(self) -> bool:
        return self.flight.departure.captured

    @Property(bool)
    def selected(self) -> bool:
        return self._selected


class MapModel(QObject):
    cleared = Signal()

    mapCenterChanged = Signal(list)
    controlPointsChanged = Signal()
    groundObjectsChanged = Signal()
    supplyRoutesChanged = Signal()
    flightsChanged = Signal()

    def __init__(self, game_model: GameModel) -> None:
        super().__init__()
        self.game_model = game_model
        self._map_center = [0, 0]
        self._control_points = []
        self._ground_objects = []
        self._supply_routes = []
        self._flights = []
        self._selected_flight_index: Optional[Tuple[int, int]] = None
        GameUpdateSignal.get_instance().game_loaded.connect(self.on_game_load)
        GameUpdateSignal.get_instance().flight_paths_changed.connect(self.reset_atos)
        GameUpdateSignal.get_instance().package_selection_changed.connect(
            self.set_package_selection
        )
        GameUpdateSignal.get_instance().flight_selection_changed.connect(
            self.set_flight_selection
        )
        self.reset()

    def set_package_selection(self, index: int) -> None:
        # Optional[int] isn't a valid type for a Qt signal. None will be converted to
        # zero automatically. We use -1 to indicate no selection.
        if index == -1:
            self._selected_flight_index = None
        else:
            self._selected_flight_index = index, 0
        self.reset_atos()

    def set_flight_selection(self, index: int) -> None:
        if self._selected_flight_index is None:
            if index != -1:
                # We don't know what order update_package_selection and
                # update_flight_selection will be called in when the last
                # package is removed. If no flight is selected, it's not a
                # problem to also have no package selected.
                logging.error("Flight was selected with no package selected")
            return

        # Optional[int] isn't a valid type for a Qt signal. None will be converted to
        # zero automatically. We use -1 to indicate no selection.
        if index == -1:
            self._selected_flight_index = self._selected_flight_index[0], None
        self._selected_flight_index = self._selected_flight_index[0], index
        self.reset_atos()

    @staticmethod
    def leaflet_coord_for(point: Point, theater: ConflictTheater) -> LeafletLatLon:
        ll = theater.point_to_ll(point)
        return [ll.latitude, ll.longitude]

    def reset(self) -> None:
        if self.game_model.game is None:
            self.clear()
            return
        with logged_duration("Map reset"):
            self.reset_control_points()
            self.reset_ground_objects()
            self.reset_routes()
            self.reset_atos()

    def on_game_load(self, game: Optional[Game]) -> None:
        if game is not None:
            self.reset_map_center(game.theater)

    def reset_map_center(self, theater: ConflictTheater) -> None:
        ll = theater.point_to_ll(theater.terrain.map_view_default.position)
        self._map_center = [ll.latitude, ll.longitude]
        self.mapCenterChanged.emit(self._map_center)

    @Property(list, notify=mapCenterChanged)
    def mapCenter(self) -> LeafletLatLon:
        return self._map_center

    def _flights_in_ato(self, ato: AirTaskingOrder, blue: bool) -> List[FlightJs]:
        flights = []
        for p_idx, package in enumerate(ato.packages):
            for f_idx, flight in enumerate(package.flights):
                flights.append(
                    FlightJs(
                        flight,
                        selected=blue and (p_idx, f_idx) == self._selected_flight_index,
                        theater=self.game.theater,
                    )
                )
        return flights

    def reset_atos(self) -> None:
        self._flights = self._flights_in_ato(
            self.game.blue_ato, blue=True
        ) + self._flights_in_ato(self.game.red_ato, blue=False)
        self.flightsChanged.emit()

    @Property(list, notify=flightsChanged)
    def flights(self) -> List[FlightJs]:
        return self._flights

    def reset_control_points(self) -> None:
        self._control_points = [
            ControlPointJs(c, self.game_model, self.game.theater)
            for c in self.game.theater.controlpoints
        ]
        self.controlPointsChanged.emit()

    @Property(list, notify=controlPointsChanged)
    def controlPoints(self) -> List[ControlPointJs]:
        return self._control_points

    def reset_ground_objects(self) -> None:
        seen = set()
        self._ground_objects = []
        for cp in self.game.theater.controlpoints:
            for tgo in cp.ground_objects:
                if tgo.name in seen:
                    continue
                seen.add(tgo.name)

                self._ground_objects.append(GroundObjectJs(tgo, self.game.theater))
        self.groundObjectsChanged.emit()

    @Property(list, notify=groundObjectsChanged)
    def groundObjects(self) -> List[GroundObjectJs]:
        return self._ground_objects

    def reset_routes(self) -> None:
        seen = set()
        self._supply_routes = []
        for control_point in self.game.theater.controlpoints:
            seen.add(control_point)
            for destination, convoy_route in control_point.convoy_routes.items():
                if destination in seen:
                    continue
                self._supply_routes.append(
                    SupplyRouteJs(
                        [
                            self.leaflet_coord_for(p, self.game.theater)
                            for p in convoy_route
                        ]
                    )
                )
            for destination, shipping_lane in control_point.shipping_lanes.items():
                if destination in seen:
                    continue
                if control_point.is_friendly(destination.captured):
                    self._supply_routes.append(
                        SupplyRouteJs(
                            [
                                self.leaflet_coord_for(p, self.game.theater)
                                for p in shipping_lane
                            ]
                        )
                    )
        self.supplyRoutesChanged.emit()

    @Property(list, notify=supplyRoutesChanged)
    def supplyRoutes(self) -> List[SupplyRouteJs]:
        return self._supply_routes

    def clear(self) -> None:
        self._control_points = []
        self._supply_routes = []
        self._ground_objects = []
        self._flights = []
        self.cleared.emit()

    @property
    def game(self) -> Game:
        if self.game_model.game is None:
            raise RuntimeError("No game loaded")
        return self.game_model.game
