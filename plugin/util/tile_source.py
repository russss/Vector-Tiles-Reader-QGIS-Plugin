import os
import sqlite3
import sys
import traceback
import urllib.parse
from typing import List, Tuple

from PyQt5.QtCore import QObject, pyqtSignal

from .file_helper import is_sqlite_db
from .log_helper import critical, debug, info, warn
from .network_helper import load_tiles_async, url_exists
from .tile_helper import WORLD_BOUNDS, Bounds, VectorTile, get_tile_bounds, get_tiles_from_center
from .tile_json import TileJSON

try:
    import simplejson as json
except ImportError:
    import json


_DEFAULT_CRS = "EPSG:3857"


class AbstractSource(QObject):
    progress_changed = pyqtSignal(int, name="tileSourceProgressChanged")
    max_progress_changed = pyqtSignal(int, name="tileSourceMaxProgressChanged")
    message_changed = pyqtSignal("QString", name="tileSourceMessageChanged")
    tile_limit_reached = pyqtSignal(name="tile_limit_reached")

    def __init__(self):
        QObject.__init__(self)
        self._cancelling = False

    def cancel(self) -> None:
        self._cancelling = True

    def source(self) -> str:
        raise NotImplementedError

    def vector_layers(self) -> List:
        raise NotImplementedError

    def close_connection(self):
        pass

    def name(self) -> str:
        raise NotImplementedError

    def attribution(self) -> str:
        raise NotImplementedError

    def min_zoom(self) -> int:
        """
         * Returns the minimum zoom that is found in either the metadata or the tile table
        :return:
        """
        raise NotImplementedError

    def max_zoom(self) -> int:
        """
         * Returns the maximum zoom that is found in either the metadata or the tile table
        :return:
        """
        raise NotImplementedError

    def scheme(self) -> str:
        raise NotImplementedError

    def bounds(self):
        raise NotImplementedError

    def bounds_tile(self, zoom: int) -> Bounds:
        """
         * Returns the tile boundaries
        :param zoom:
        :return:
        """
        raise NotImplementedError

    def crs(self):
        raise NotImplementedError

    def load_tiles(self, zoom_level, tiles_to_load, max_tiles=None):
        """
         * Loads the tiles for the specified zoom_level and bounds from the web service,
          this source has been created with
        :param tiles_to_load: All tile coordinates which shall be loaded
        :param zoom_level: The zoom level which will be loaded
        :param max_tiles: The maximum number of tiles to be loaded
        :return:
        """
        raise NotImplementedError


class ServerSource(AbstractSource):
    def __init__(self, url: str):
        AbstractSource.__init__(self)
        if not url:
            raise RuntimeError("URL is required")

        if os.path.isfile(url):
            valid = True
        else:
            valid, error, url = url_exists(url)
        if not valid:
            critical("The URL seems to be invalid: {}", url)
            raise RuntimeError(error)

        self.url = url
        self.json = TileJSON(url)
        self.json.load()

    def source(self):
        return self.url

    def vector_layers(self):
        return self.json.vector_layers()

    def bounds(self):
        return self.json.bounds_longlat()

    def close_connection(self):
        pass

    def name(self):
        name = self.json.name()
        if not name:
            name = self.json.id()
        if not name:
            name = urllib.parse.urlsplit(self.url)[1]
        return name

    def min_zoom(self):
        return self.json.min_zoom()

    def max_zoom(self):
        return self.json.max_zoom()

    def attribution(self):
        return self.json.attribution()

    def scheme(self):
        return self.json.scheme()

    def bounds_tile(self, zoom):
        lng_lat = self.json.bounds_longlat()
        return get_tile_bounds(zoom=zoom, scheme=self.scheme(), extent=lng_lat, source_crs="4326")

    def crs(self):
        return self.json.crs()

    def load_tiles(self, zoom_level, tiles_to_load, max_tiles=None):
        self._cancelling = False
        base_url = self.json.tiles()[0]
        if "{s}" in base_url:
            info("Special treatment for Nextzen url...")

        urls = []
        if max_tiles and len(tiles_to_load) > max_tiles:
            tiles_to_load = get_tiles_from_center(max_tiles, tiles_to_load, should_cancel_func=lambda: self._cancelling)
            self.tile_limit_reached.emit()

        parameters = urllib.parse.parse_qs(urllib.parse.urlparse(self.url).query)
        api_key = ""
        if "api_key" in list(parameters.keys()):
            api_key = parameters["api_key"][0]
        nextzen_servers = ["a", "b", "c", "d"]
        for i, t in enumerate(tiles_to_load):
            col = t[0]
            row = t[1]
            load_url = (
                base_url.replace("{z}", str(int(zoom_level)))
                .replace("{x}", str(int(col)))
                .replace("{y}", str(int(row)))
                .replace("{api_key}", str(api_key))
                .replace("{s}", nextzen_servers[divmod(i, len(nextzen_servers))[1]])
            )  # nextzen server placeholder
            if api_key:
                load_url += "?api_key={}".format(api_key)
            urls.append((load_url, col, row))

        self.max_progress_changed.emit(len(urls))
        self.message_changed.emit("Getting {} tiles from source...".format(len(urls)))
        tile_coords_with_content = load_tiles_async(
            urls_with_col_and_row=urls,
            on_progress_changed=lambda p: self.progress_changed.emit(p),
            cancelling_func=lambda: self._cancelling,
        )
        tiles_with_data = []
        for coord, data in tile_coords_with_content:
            tile = VectorTile(self.scheme(), zoom_level=zoom_level, x=coord[0], y=coord[1])
            tiles_with_data.append((tile, data))

        return tiles_with_data


class MBTilesSource(AbstractSource):
    def attribution(self):
        return self._get_metadata_value("attribution", "")

    def __init__(self, path):
        AbstractSource.__init__(self)
        if not os.path.isfile(path):
            raise RuntimeError("The file does not exist: {}".format(path))

        if not is_sqlite_db(path):
            raise RuntimeError(
                "The file '{}' is not a valid Mapbox vector tile file and cannot be loaded.".format(path)
            )

        self.path = path
        self.conn = None
        self._metadata_cache = {}

    def source(self):
        return self.path

    def crs(self):
        return self._get_metadata_value("crs", _DEFAULT_CRS)

    def vector_layers(self):
        data = self._get_metadata_value("json")
        layers = []
        if data:
            json_data = json.loads(data)
            if "vector_layers" in json_data:
                layers = json_data["vector_layers"]
        else:
            warn("No json found in metadata table")
        return layers

    def bounds(self) -> Tuple:
        bounds = self._get_metadata_value("bounds")
        if bounds and isinstance(bounds, str):
            bounds = bounds.replace(" ", "").replace("[", "").replace("]", "").split(",")
            bounds = tuple(float(s) for s in bounds)
        return bounds

    def bounds_tile(self, zoom):
        bounds = self.bounds()
        tile_bounds = None
        scheme = self.scheme()
        if bounds:
            tile_bounds = get_tile_bounds(zoom=zoom, extent=bounds, scheme=scheme, source_crs="4326")
        if not tile_bounds:
            tile_bounds = self._get_bounds_from_data(zoom_level=zoom)
        if not tile_bounds:
            bounds = WORLD_BOUNDS
            tile_bounds = get_tile_bounds(zoom=zoom, extent=bounds, scheme=scheme, source_crs="4326")
        return tile_bounds

    def name(self):
        base_name = os.path.splitext(os.path.basename(self.path))[0]
        return base_name

    def scheme(self):
        return self._get_metadata_value("scheme", default="tms")

    def min_zoom(self):
        return self._get_zoom(max_zoom=False)

    def max_zoom(self):
        return self._get_zoom(max_zoom=True)

    def mask_level(self):
        return self._get_metadata_value("maskLevel")

    def load_tiles(self, zoom_level, tiles_to_load, max_tiles=None):
        """
         * Loads the tiles listed in tiles_to_load for the specified zoom_level.
        :param zoom_level:
        :param tiles_to_load:
        :param max_tiles:
        :return:
        """
        self._cancelling = False
        debug("Reading tiles of zoom level {}", zoom_level)

        if zoom_level is None:
            raise RuntimeError("zoom_level is required")

        if tiles_to_load is None:
            raise RuntimeError("tiles_to_load is required")

        if max_tiles is not None:
            center_tiles = get_tiles_from_center(
                nr_of_tiles=max_tiles, available_tiles=tiles_to_load, should_cancel_func=lambda: self._cancelling
            )
        else:
            center_tiles = tiles_to_load
        where_clause = self._get_where_clause(tiles_to_load=center_tiles, zoom_level=zoom_level)

        sql_command = "SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles {};"
        sql = sql_command.format(where_clause)

        tile_data_tuples = []
        rows = self._get_from_db(sql=sql)
        count_sql = "select count(*) 'nr_of_tiles' from tiles WHERE zoom_level = {}".format(zoom_level)
        total_nr_of_tiles = self._get_single_value(count_sql, "nr_of_tiles")
        if max_tiles is not None and max_tiles < total_nr_of_tiles:
            self.tile_limit_reached.emit()

        if rows:
            self.max_progress_changed.emit(len(rows))
            for index, row in enumerate(rows):
                if self._cancelling or (max_tiles and len(tile_data_tuples) >= max_tiles):
                    break
                tile, data = self._create_tile(row)
                tile_data_tuples.append((tile, data))
                self.progress_changed.emit(index + 1)
        return tile_data_tuples

    def _get_bounds_from_data(self, zoom_level):
        sql = """select 
                min(tile_column) 'x_min', 
                max(tile_column) 'x_max', 
                min(tile_row) 'y_min', 
                max(tile_row) 'y_max'
                from tiles
                WHERE zoom_level = {}""".format(
            zoom_level
        )
        rows = self._get_from_db(sql)
        bounds = None
        if rows:
            row = rows[0]
            bounds = Bounds.create(
                zoom=zoom_level,
                x_min=row["x_min"],
                x_max=row["x_max"],
                y_min=row["y_min"],
                y_max=row["y_max"],
                scheme=self.scheme(),
            )
        return bounds

    @staticmethod
    def _get_where_clause(tiles_to_load, zoom_level):
        where_clause = ""
        if zoom_level is not None or tiles_to_load is not None:
            where_clause = "WHERE"
            if zoom_level is not None:
                where_clause += " zoom_level = {}".format(zoom_level)
                if tiles_to_load is not None:
                    where_clause += " AND"
            if tiles_to_load is not None:
                tile_coords = str(["{};{}".format(x[0], x[1]) for x in tiles_to_load]).replace("[", "").replace("]", "")
                where_clause += ' tile_column || ";" || tile_row IN ({})'.format(tile_coords)
        return where_clause

    def _create_tile(self, row):
        zoom_level = row["zoom_level"]
        tile_col = row["tile_column"]
        tile_row = row["tile_row"]
        binary_data = row["tile_data"]
        tile = VectorTile(self.scheme(), zoom_level, tile_col, tile_row)
        return tile, binary_data

    def close_connection(self):
        """
         * Closes the current db connection
        :return: 
        """
        if self.conn:
            try:
                self.conn.close()
                debug("Connection closed")
            except:
                warn("Closing connection failed: {}".format(sys.exc_info()))
        self.conn = None

    def _get_zoom(self, max_zoom=True):
        if max_zoom:
            field_name = "maxzoom"
        else:
            field_name = "minzoom"

        if field_name not in self._metadata_cache:
            zoom = self._get_metadata_value(field_name)
            if zoom is None:
                zoom = self._get_zoom_from_tiles_table(max_zoom=max_zoom)
            if zoom is not None:
                zoom = int(zoom)
            self._metadata_cache[field_name] = zoom
        return self._metadata_cache[field_name]

    def _get_zoom_from_tiles_table(self, max_zoom=True):
        if max_zoom:
            order = "desc"
        else:
            order = "asc"

        query = """
            select zoom_level as 'zoom_level'
            from tiles
            order by zoom_level {}
            limit 1
        """.format(
            order
        )
        return self._get_single_value(sql_query=query, field_name="zoom_level")

    def _get_metadata_value(self, field_name, default=None):
        if field_name not in self._metadata_cache:
            debug("Loading metadata value '{}'", field_name)
            sql = "select value as '{0}' from metadata where name = '{0}'".format(field_name)
            value = self._get_single_value(sql_query=sql, field_name=field_name)
            if default and not value:
                value = default
            self._metadata_cache[field_name] = value
        return self._metadata_cache[field_name]

    def _get_single_value(self, sql_query, field_name):
        """
         * Helper function that can be used to safely load a single value from the db
         * Returns the value or None if result is empty or execution of query failed
        :param sql_query: 
        :param field_name: 
        :return: 
        """
        value = None
        try:
            rows = self._get_from_db(sql=sql_query)
            if rows:
                value = rows[0][field_name]
                debug("Value is: {}".format(value))
        except:
            critical("Loading metadata value '{}' failed: {}", field_name, sys.exc_info())
        return value

    def _get_from_db(self, sql):
        if not self.conn:
            debug("Not connected yet.")
            self._connect_to_db()
        try:
            debug("Execute SQL: {}", sql)
            cur = self.conn.cursor()
            cur.execute(sql)
            return cur.fetchall()
        except sqlite3.OperationalError:
            critical("Getting data from db failed: {}", sql)
        except:
            tb = ""
            if traceback:
                tb = traceback.format_exc()
            critical("Getting data from db failed: {}, {}", sys.exc_info(), tb)

    def _connect_to_db(self):
        """
         * Since an mbtile file is a sqlite database, we can connect to it
        """
        debug("Connecting to: {}", self.path)
        try:
            self.conn = sqlite3.connect(self.path)
            self.conn.row_factory = sqlite3.Row
            debug("Successfully connected")
        except:
            critical("Db connection failed:", sys.exc_info())


class DirectorySource(AbstractSource):
    def __init__(self, path):
        AbstractSource.__init__(self)
        if not os.path.isdir(path):
            raise RuntimeError("The folder does not exist: {}".format(path))
        self.path = path
        metadata_path = os.path.join(path, "metadata.json")
        if not os.path.isfile(metadata_path):
            raise RuntimeError("There is no metadata.json in the directory.")
        self.json = TileJSON(metadata_path)
        self.json.load()

    def source(self):
        return self.path

    def attribution(self):
        return self.json.attribution()

    def vector_layers(self):
        layers = self.json.get_value("vector_layers", is_array=True, is_required=False)
        if not layers:
            layers = json.loads(self.json.get_value("json"))["vector_layers"]
        if not layers:
            raise RuntimeError("'vector_layers' is required")
        return layers

    def name(self):
        name = self.json.name()
        if not name:
            name = self.json.id()
        if not name:
            name = os.path.basename(self.path)
        return name

    def min_zoom(self):
        return self.json.min_zoom()

    def max_zoom(self):
        return self.json.max_zoom()

    def mask_level(self):
        return self.json.mask_level()

    def scheme(self):
        return self.json.scheme()

    def bounds(self):
        return self.json.bounds_longlat()

    def bounds_tile(self, zoom):
        return self.json.bounds_tile(zoom)

    def crs(self):
        return self.json.crs()

    def load_tiles(self, zoom_level, tiles_to_load, max_tiles=None):
        self._cancelling = False
        tile_data_tuples = []

        if len(tiles_to_load) > max_tiles:
            tiles_to_load = get_tiles_from_center(max_tiles, tiles_to_load, should_cancel_func=lambda: self._cancelling)
            self.tile_limit_reached.emit()

        tile_path = self.json.get_value(key="tiles", is_array=True)
        if tile_path:
            tile_path = tile_path[0]
        else:
            tile_path = os.path.join(self.path, "{z}/{x}/{y}.pbf")

        self.max_progress_changed.emit(tiles_to_load)
        for index, t in enumerate(tiles_to_load):
            self.progress_changed.emit(index)
            full_path = tile_path.format(z=int(zoom_level), x=t[0], y=t[1])
            col = t[0]
            row = t[1]
            tile = VectorTile(self.scheme(), zoom_level, col, row)
            if os.path.isfile(full_path):
                with open(full_path, "rb") as f:
                    encoded_data = f.read()
                    tile_data_tuples.append((tile, encoded_data))
            else:
                info("File not found: {}", full_path)
        return tile_data_tuples
