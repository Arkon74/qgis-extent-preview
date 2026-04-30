from qgis.PyQt.QtWidgets import QAction, QToolBar, QDockWidget, QTreeView
from qgis.PyQt.QtGui import QIcon, QColor
from qgis.core import (
    Qgis,
    QgsLayerItem,
    QgsDataCollectionItem,
    QgsDirectoryItem,
    QgsRasterLayer,
    QgsVectorLayer,
    QgsPointCloudLayer,
    QgsCoordinateTransform,
    QgsCoordinateReferenceSystem,
    QgsProject,
    QgsGeometry,
    QgsRectangle,
    QgsWkbTypes,
    QgsMessageLog,
    QgsBrowserModel,
)
from qgis.gui import QgsRubberBand
from qgis.PyQt import sip
import os
import struct


# Containers worth recursing into. Spreadsheets, CSVs, archives, etc. fall
# through to an early return — building a temp layer per sheet/file is slow
# and pointless since they have no spatial extent.
SPATIAL_CONTAINER_EXTS = {".gpkg", ".sqlite", ".db", ".gdb, .jpg, .jpeg, .png"}


class ExtentPreview:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.rb = None
        self.tree_view = None
        self.browser_dock = None

    def initGui(self):
        # Find the Browser dock + its tree view. Don't touch the model here —
        # at startup it may not be assigned yet, which crashes the cast below.
        # We do the cast lazily in inspect() instead.
        self.browser_dock = self.iface.mainWindow().findChild(QDockWidget, "Browser")
        if self.browser_dock:
            self.tree_view = self.browser_dock.findChild(QTreeView)

        # Toggle action
        icon_path = os.path.join(os.path.dirname(__file__), "icon.svg")
        self.action = QAction(QIcon(icon_path), "Preview layer extent", self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.toggled.connect(self.on_toggled)

        if self.browser_dock:
            toolbars = self.browser_dock.findChildren(QToolBar)
            if toolbars:
                toolbars[0].addAction(self.action)

        # One rubber band, reused for every selection
        self.rb = QgsRubberBand(self.iface.mapCanvas(), QgsWkbTypes.GeometryType.PolygonGeometry)
        self.rb.setColor(QColor(255, 0, 0, 200))
        self.rb.setFillColor(QColor(255, 0, 0, 40))
        self.rb.setWidth(2)

    def unload(self):
        # Toggle off so the disconnect path runs cleanly
        if self.action and self.action.isChecked():
            self.action.setChecked(False)

        if self.browser_dock and self.action:
            toolbars = self.browser_dock.findChildren(QToolBar)
            if toolbars:
                toolbars[0].removeAction(self.action)

        if self.rb:
            self.rb.reset(QgsWkbTypes.GeometryType.PolygonGeometry)
            self.iface.mapCanvas().scene().removeItem(self.rb)
            self.rb = None

        self.action = None
        self.tree_view = None
        self.browser_dock = None

    # -- Toggle on/off -------------------------------------------------------

    def on_toggled(self, checked):
        if not self.tree_view:
            return
        sel_model = self.tree_view.selectionModel()
        if checked:
            sel_model.currentChanged.connect(self.inspect)
        else:
            try:
                sel_model.currentChanged.disconnect(self.inspect)
            except (TypeError, RuntimeError):
                pass
            self.rb.reset(QgsWkbTypes.GeometryType.PolygonGeometry)

    # -- Selection handler ---------------------------------------------------

    def _browser_model(self):
        """Lazily get the casted QgsBrowserModel. Returns None if not ready."""
        proxy = self.tree_view.model() if self.tree_view else None
        if proxy is None:
            return None
        raw = proxy.sourceModel()
        if raw is None:
            return None
        return sip.cast(raw, QgsBrowserModel)

    def inspect(self, current, previous):
        self.rb.reset(QgsWkbTypes.GeometryType.PolygonGeometry)

        if not current.isValid():
            return

        browser_model = self._browser_model()
        if browser_model is None:
            return

        source_index = self.tree_view.model().mapToSource(current)
        item = browser_model.dataItem(source_index)
        if item is None:
            return

        if isinstance(item, QgsLayerItem):
            extents = self._extent_for_layer_item(item)
        elif isinstance(item, QgsDataCollectionItem) and not isinstance(item, QgsDirectoryItem):
            # Only recurse into known file-based spatial containers (.gpkg, .sqlite, etc.).
            # This skips:
            #   - service nodes (WMS/WFS/XYZ/PostGIS) where populate() means a network round-trip
            #   - non-spatial files (xlsx, csv, zip, ...)
            #   - top-level browser categories (Favorites, Spatial Bookmarks, ...)
            path = item.path() if hasattr(item, "path") else ""
            ext = os.path.splitext(path)[1].lower()
            if ext not in SPATIAL_CONTAINER_EXTS:
                return
            extents = self._extent_for_collection(item)
        else:
            return

        if not extents:
            return

        canvas_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
        combined = QgsRectangle()
        combined.setMinimal()
        for extent, crs in extents:
            if crs != canvas_crs:
                xform = QgsCoordinateTransform(crs, canvas_crs, QgsProject.instance())
                try:
                    extent = xform.transformBoundingBox(extent)
                except Exception as e:
                    QgsMessageLog.logMessage(
                        f"CRS transform failed: {e}", "extent_preview", Qgis.MessageLevel.Warning
                    )
                    continue
            combined.combineExtentWith(extent)

        if combined.isNull() or combined.isEmpty():
            return

        self.rb.setToGeometry(QgsGeometry.fromRect(combined), None)

    # -- Helpers -------------------------------------------------------------

    def _extent_for_layer_item(self, item):
        """Return [(extent, crs)] for a single layer item, or [] on failure."""
        uri = item.uri()
        ext = os.path.splitext(uri)[1].lower()

        # Fast path for LAS/LAZ — avoids QgsPointCloudLayer construction,
        # which triggers PDAL to build a COPC index next to the source file.
        if ext in (".las", ".laz"):
            result = self._read_las_header_extent(uri)
            return [result] if result else []

        # Standard path: temp layer construction, then read extent + CRS.
        layer = self._build_temp_layer(item)
        if layer is None or not layer.isValid():
            return []
        return [(layer.extent(), layer.crs())]

    def _extent_for_collection(self, item):
        """Recursively collect (extent, crs) for all layer items under a collection."""
        if not item.children():
            item.populate(True)

        results = []
        for child in item.children():
            if isinstance(child, QgsLayerItem):
                results.extend(self._extent_for_layer_item(child))
            elif isinstance(child, QgsDataCollectionItem) and not isinstance(child, QgsDirectoryItem):
                # Same allowlist check as in inspect() — avoid recursing into
                # service connections or non-spatial nested containers.
                child_path = child.path() if hasattr(child, "path") else ""
                child_ext = os.path.splitext(child_path)[1].lower()
                if child_ext in SPATIAL_CONTAINER_EXTS:
                    results.extend(self._extent_for_collection(child))
        return results

        
    def _read_las_header_extent(self, path):
        """Read bbox + CRS from a LAS/LAZ header. No point data read, no indexing.

        If the file has no embedded CRS, falls back to the project canvas CRS
        and emits a warning to the message bar so the user knows the rectangle
        may be drawn in the wrong place.
        """
        name = os.path.basename(path)
        try:
            with open(path, "rb") as f:
                if f.read(4) != b"LASF":
                    return None

                f.seek(6)
                global_encoding = struct.unpack("<H", f.read(2))[0]

                f.seek(24)
                version_major = f.read(1)[0]
                version_minor = f.read(1)[0]
                wkt_mode = bool(global_encoding & 0b10000) or version_minor >= 4

                f.seek(94)
                header_size = struct.unpack("<H", f.read(2))[0]
                offset_to_point_data = struct.unpack("<I", f.read(4))[0]
                num_vlrs = struct.unpack("<I", f.read(4))[0]

                # LAS 1.4 EVLRs (start at offset 235 uint64, count at 243 uint32)
                evlr_start = 0
                num_evlrs = 0
                if version_major == 1 and version_minor >= 4 and header_size >= 247:
                    f.seek(235)
                    evlr_start = struct.unpack("<Q", f.read(8))[0]
                    num_evlrs = struct.unpack("<I", f.read(4))[0]

                f.seek(179)
                data = f.read(48)
                if len(data) < 48:
                    return None
                max_x, min_x, max_y, min_y, _max_z, _min_z = struct.unpack("<6d", data)

                if min_x >= max_x or min_y >= max_y:
                    return None

                extent = QgsRectangle(min_x, min_y, max_x, max_y)

                # Walk regular VLRs first
                f.seek(header_size)
                crs = self._parse_las_vlrs(f, num_vlrs, offset_to_point_data, wkt_mode)

                # If nothing found and this is a 1.4 file with EVLRs, walk those too
                if crs is None and num_evlrs > 0 and evlr_start > 0:
                    f.seek(evlr_start)
                    crs = self._parse_las_evlrs(f, num_evlrs, wkt_mode)

            if crs is None or not crs.isValid():
                # No usable CRS in the file. Fall back to canvas CRS so the user
                # sees something, but warn them the placement may be wrong.
                canvas_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
                self.iface.messageBar().pushMessage(
                    "Extent preview",
                    f"{name} has no embedded CRS — drawing using project CRS "
                    f"({canvas_crs.authid() or 'custom'}). Position may be incorrect.",
                    level=Qgis.MessageLevel.Warning,
                    duration=4,
                )
                return extent, canvas_crs

            return extent, crs
        except (OSError, struct.error) as e:
            print(f"[{name}] LAS header read failed: {e}")
            return None

    def _parse_las_vlrs(self, f, count, stop_offset, wkt_mode):
        """Walk regular VLRs. Header 54 bytes, length is uint16."""
        return self._parse_vlr_block(f, count, stop_offset, wkt_mode,
                                     length_format="<H", header_size=54)

    def _parse_las_evlrs(self, f, count, wkt_mode):
        """Walk extended VLRs (LAS 1.4). Header 60 bytes, length is uint64."""
        return self._parse_vlr_block(f, count, None, wkt_mode,
                                     length_format="<Q", header_size=60)

    def _parse_vlr_block(self, f, count, stop_offset, wkt_mode,
                         length_format, header_size):
        """Generic VLR/EVLR walker. Returns QgsCoordinateReferenceSystem or None.

        VLR header (54 B):  2 reserved + 16 user_id + 2 record_id + 2 length + 32 description.
        EVLR header (60 B): same layout but length field is uint64.
        """
        geokey_directory = None
        wkt_string = None
        length_size = struct.calcsize(length_format)

        for _ in range(count):
            if stop_offset is not None and f.tell() >= stop_offset:
                break

            header = f.read(header_size)
            if len(header) < header_size:
                break

            user_id = header[2:18].rstrip(b"\x00").decode("ascii", errors="ignore")
            record_id = struct.unpack("<H", header[18:20])[0]
            record_length = struct.unpack(length_format, header[20:20 + length_size])[0]
            payload = f.read(record_length)

            if user_id == "LASF_Projection":
                if record_id == 34735:
                    geokey_directory = payload
                elif record_id in (2111, 2112):
                    wkt_string = payload.rstrip(b"\x00").decode("utf-8", errors="ignore")

        if wkt_mode and wkt_string:
            crs = QgsCoordinateReferenceSystem.fromWkt(wkt_string)
            if crs.isValid():
                return crs

        if geokey_directory:
            epsg = self._epsg_from_geokeys(geokey_directory)
            if epsg:
                crs = QgsCoordinateReferenceSystem.fromEpsgId(epsg)
                if crs.isValid():
                    return crs

        if wkt_string:
            crs = QgsCoordinateReferenceSystem.fromWkt(wkt_string)
            if crs.isValid():
                return crs

        return None

    @staticmethod
    def _epsg_from_geokeys(blob):
        """Pull the EPSG code from a GeoKeyDirectoryTag blob.

        Layout: header (4x uint16) + N entries (4x uint16 each).
        Entry:  KeyID, TIFFTagLocation, Count, Value_Offset.
        When TIFFTagLocation == 0, Value_Offset is the literal value.

        Relevant KeyIDs:
          3072 = ProjectedCSTypeGeoKey
          2048 = GeographicTypeGeoKey
        """
        if len(blob) < 8:
            return None
        keys = struct.unpack(f"<{len(blob) // 2}H", blob[:(len(blob) // 2) * 2])
        num_keys = keys[3]
        for i in range(num_keys):
            offset = 4 + i * 4
            if offset + 4 > len(keys):
                break
            key_id, tiff_tag_location, _count, value_offset = keys[offset:offset + 4]
            if tiff_tag_location == 0 and key_id in (3072, 2048):
                if 1024 <= value_offset <= 32767:
                    return value_offset
        return None

    @staticmethod
    def _build_temp_layer(item):
        """Construct a lightweight layer just to read extent + CRS."""
        uri = item.uri()
        provider = item.providerKey()
        try:
            layer_type = item.mapLayerType()
        except AttributeError:
            return None

        if layer_type == Qgis.LayerType.Raster:
            return QgsRasterLayer(uri, "tmp_extent_preview", provider)
        if layer_type == Qgis.LayerType.Vector:
            return QgsVectorLayer(uri, "tmp_extent_preview", provider)
        if layer_type == Qgis.LayerType.PointCloud:
            return QgsPointCloudLayer(uri, "tmp_extent_preview", provider)
        return None
