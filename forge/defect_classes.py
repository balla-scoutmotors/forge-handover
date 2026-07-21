"""
This program demonstrates a basic architecture that:
1. Receives defect predictions (simulated YOLO output)
2. Maps them to defect objects
3. Retrieves possible causes and their values for each defect (populated from MES JSON)

Focus: Clean OOP structure + easy to expand

Supported Defects:
- burr: Teaching orientation, surface flatness, material, and heat input
- edge_weld: Tip position, edge distance, tooling/calibration, geometry, teaching
- spatter: Heat input and electrode condition
- dark_weld_spot: Excess heat and electrode tip condition
"""

import json
import os
import inspect
from typing import Any, Dict, List

# Path to the MES data file (relative to this script)
MES_DATA_PATH = os.path.join(os.path.dirname(__file__), 'simple_mes.json')


# -----------------------------
# BASE CLASS (PARENT)
# -----------------------------
class Defect:
    """
    Parent class for all defect types.
    Holds basic information fields shared across all defects.
    All fields default to None and are populated from MES data.
    """

    def __init__(self, name,
                 time=None,
                 class_name=None,
                 cell_number=None,
                 assembly_number=None,
                 operator=None):
        self.name = name
        self.time = time
        self.class_name = class_name
        self.cell_number = cell_number
        self.assembly_number = assembly_number
        self.operator = operator

    def get_causes(self):
        """
        Must be implemented by child classes.
        Returns a dict of {cause_parameter: value} populated from MES data.
        """
        raise NotImplementedError("Subclasses must implement get_causes()")

    def to_dict(self):
        """
        Returns the full defect record: basic info + causes dict.
        """
        return {
            "defect_name": self.name,
            "time": self.time,
            "class_name": self.class_name,
            "cell_number": self.cell_number,
            "assembly_number": self.assembly_number,
            "operator": self.operator,
            "causes": self.get_causes()
        }


# -----------------------------
# CHILD CLASSES (SPECIFIC DEFECTS)
# -----------------------------

class Burr(Defect):
    """Burr defects driven by teaching orientation, surface flatness, material, and heat input."""

    def __init__(self,
                 time=None, class_name=None, cell_number=None,
                 assembly_number=None, operator=None,
                 teaching_retaught_days=None,
                 surface_flatness_angle_degrees=None,
                 material_type=None,
                 peak_temperature_celsius=None,
                 total_weld_time_seconds=None):
        super().__init__("Burr", time=time, class_name=class_name,
                         cell_number=cell_number, assembly_number=assembly_number,
                         operator=operator)
        self.teaching_retaught_days = teaching_retaught_days
        self.surface_flatness_angle_degrees = surface_flatness_angle_degrees
        self.material_type = material_type
        self.peak_temperature_celsius = peak_temperature_celsius
        self.total_weld_time_seconds = total_weld_time_seconds

    def get_causes(self):
        return {
            "teaching_retaught_days": self.teaching_retaught_days,
            "surface_flatness_angle_degrees": self.surface_flatness_angle_degrees,
            "material_type": self.material_type,
            "peak_temperature_celsius": self.peak_temperature_celsius,
            "total_weld_time_seconds": self.total_weld_time_seconds
        }


class EdgeWeld(Defect):
    """Edge weld defects driven by tip position, edge distance, tooling/calibration, and teaching."""

    def __init__(self,
                 time=None, class_name=None, cell_number=None,
                 assembly_number=None, operator=None,
                 electrode_tip_position_mm=None,
                 electrode_tip_from_sheet_edge_mm=None,
                 tool_changed_days=None,
                 electrode_tip_calibration_days=None,
                 part_body_change_days=None,
                 teaching_retaught_days=None):
        super().__init__("Edge Weld", time=time, class_name=class_name,
                         cell_number=cell_number, assembly_number=assembly_number,
                         operator=operator)
        self.electrode_tip_position_mm = electrode_tip_position_mm
        self.electrode_tip_from_sheet_edge_mm = electrode_tip_from_sheet_edge_mm
        self.tool_changed_days = tool_changed_days
        self.electrode_tip_calibration_days = electrode_tip_calibration_days
        self.part_body_change_days = part_body_change_days
        self.teaching_retaught_days = teaching_retaught_days

    def get_causes(self):
        return {
            "electrode_tip_position_mm": self.electrode_tip_position_mm,
            "electrode_tip_from_sheet_edge_mm": self.electrode_tip_from_sheet_edge_mm,
            "tool_changed_days": self.tool_changed_days,
            "electrode_tip_calibration_days": self.electrode_tip_calibration_days,
            "part_body_change_days": self.part_body_change_days,
            "teaching_retaught_days": self.teaching_retaught_days
        }


class Spatter(Defect):
    """Spatter defects - always present, driven by heat input and electrode condition."""

    def __init__(self,
                 time=None, class_name=None, cell_number=None,
                 assembly_number=None, operator=None,
                 peak_temperature_celsius=None,
                 total_weld_time_seconds=None,
                 electrode_condition=None):
        super().__init__("Spatter", time=time, class_name=class_name,
                         cell_number=cell_number, assembly_number=assembly_number,
                         operator=operator)
        self.peak_temperature_celsius = peak_temperature_celsius
        self.total_weld_time_seconds = total_weld_time_seconds
        self.electrode_condition = electrode_condition

    def get_causes(self):
        return {
            "peak_temperature_celsius": self.peak_temperature_celsius,
            "total_weld_time_seconds": self.total_weld_time_seconds,
            "electrode_condition": self.electrode_condition
        }


class DarkWeldSpot(Defect):
    """Dark weld spot defects driven by excess heat and electrode tip condition."""

    def __init__(self,
                 time=None, class_name=None, cell_number=None,
                 assembly_number=None, operator=None,
                 peak_temperature_celsius=None,
                 electrode_condition=None,
                 total_weld_time_seconds=None):
        super().__init__("Dark Weld Spot", time=time, class_name=class_name,
                         cell_number=cell_number, assembly_number=assembly_number,
                         operator=operator)
        self.peak_temperature_celsius = peak_temperature_celsius
        self.electrode_condition = electrode_condition
        self.total_weld_time_seconds = total_weld_time_seconds

    def get_causes(self):
        return {
            "peak_temperature_celsius": self.peak_temperature_celsius,
            "electrode_condition": self.electrode_condition,
            "total_weld_time_seconds": self.total_weld_time_seconds
        }


class UnknownDefect(Defect):
    """Fallback defect used when a class name has no explicit mapping."""

    def __init__(self,
                 time=None, class_name=None, cell_number=None,
                 assembly_number=None, operator=None):
        super().__init__("Unknown", time=time, class_name=class_name,
                         cell_number=cell_number, assembly_number=assembly_number,
                         operator=operator)

    def get_causes(self):
        return {}


# -----------------------------
# FACTORY CLASS
# -----------------------------
class DefectFactory:
    """
    Converts a detected class name string into a defect object,
    populating it with values from MES data where available.
    """

    @staticmethod
    def create_defect(defect_name, mes_data=None):
        """Instantiate defect class from a class name and MES row.

        Args:
            defect_name (str): Incoming YOLO class name.
            mes_data (dict, optional): MES row used to populate constructor fields.

        Returns:
            Defect: Instantiated subclass mapped from defect_name.
        """
        if mes_data is None:
            mes_data = {}

        defect_map = {
            "burr": Burr,
            "edge_weld": EdgeWeld,
            "spatter": Spatter,
            "dark_weld_spot": DarkWeldSpot,
        }

        defect_class = defect_map.get(defect_name.lower().strip(), UnknownDefect)

        # Only pass keys from MES data that the constructor actually accepts
        valid_params = inspect.signature(defect_class.__init__).parameters
        filtered_data = {
            k: v for k, v in mes_data.items()
            if isinstance(mes_data, dict) and k in valid_params
        }

        return defect_class(**filtered_data)

    @staticmethod
    def supported_class_names() -> List[str]:
        """Return supported incoming YOLO class names."""
        return ["burr", "edge_weld", "spatter", "dark_weld_spot"]


def _normalize_name(value: Any) -> str:
    """Normalize a class/value string for case-insensitive matching.

    Args:
        value (Any): Value to normalize.

    Returns:
        str: Lower-cased, stripped string.
    """
    return str(value or "").strip().lower()


def _select_mes_record_for_class(class_name: str, mes_data: Any) -> Dict[str, Any]:
    """Select the first MES row that matches the detected class name.

    Args:
        class_name (str): Detected class label.
        mes_data (Any): Parsed JSON content (dict or list of dicts).

    Returns:
        Dict[str, Any]: Matching MES row if found, otherwise an empty dict.
    """
    target = _normalize_name(class_name)

    if isinstance(mes_data, dict):
        return mes_data

    if isinstance(mes_data, list):
        for row in mes_data:
            if not isinstance(row, dict):
                continue
            if _normalize_name(row.get("class_name")) == target:
                return row

    return {}


def load_mes_data(mes_data_path: str = MES_DATA_PATH) -> Dict[str, Any]:
    """Load MES JSON as a dictionary; returns empty dict on failure."""
    try:
        with open(mes_data_path, 'r', encoding='utf-8') as f:
            loaded = json.load(f)
            if isinstance(loaded, dict):
                return loaded
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}


def list_station_camera_paths(mes_data: Dict[str, Any]) -> Dict[str, List[str]]:
    """Return available station -> camera keys from body_shop branch."""
    body_shop = mes_data.get("body_shop") if isinstance(mes_data, dict) else None
    if not isinstance(body_shop, dict):
        return {}

    available: Dict[str, List[str]] = {}
    for station_key, station_value in body_shop.items():
        if not isinstance(station_value, dict):
            continue
        camera_keys = [
            camera_key
            for camera_key, camera_value in station_value.items()
            if isinstance(camera_value, dict)
        ]
        if camera_keys:
            available[station_key] = sorted(camera_keys)
    return available


def get_camera_context(mes_data: Dict[str, Any], station_key: str, camera_key: str) -> Dict[str, Any]:
    """Return camera branch for the exact station/camera path or raise KeyError."""
    body_shop = mes_data.get("body_shop") if isinstance(mes_data, dict) else None
    if not isinstance(body_shop, dict):
        raise KeyError("Missing 'body_shop' root in MES data.")

    station = body_shop.get(station_key)
    if not isinstance(station, dict):
        raise KeyError(f"Station '{station_key}' does not exist in MES data.")

    camera = station.get(camera_key)
    if not isinstance(camera, dict):
        raise KeyError(f"Camera path '{station_key}/{camera_key}' does not exist in MES data.")

    return camera


def classify_defect_for_path(
    class_name: str,
    mes_data: Dict[str, Any],
    station_key: str,
    camera_key: str,
    confidence: float = None,
) -> Dict[str, Any]:
    """Classify using the exact station/camera JSON path for enrichment."""
    camera_context = get_camera_context(mes_data, station_key, camera_key)
    robot_data = camera_context.get("robot_data", {})
    if not isinstance(robot_data, dict):
        raise KeyError(f"Camera path '{station_key}/{camera_key}' is missing 'robot_data'.")

    mes_record = dict(robot_data)
    mes_record.setdefault("time", camera_context.get("timestamp"))
    mes_record.setdefault("class_name", class_name)
    mes_record.setdefault("operator", camera_context.get("robot_id"))
    mes_record.setdefault("cell_number", camera_context.get("camera_id"))
    mes_record.setdefault("assembly_number", camera_context.get("station_name"))

    defect = DefectFactory.create_defect(class_name, mes_record)
    result = defect.to_dict()
    result["confidence"] = confidence
    result["station"] = station_key
    result["camera"] = camera_key
    result["source_path"] = f"body_shop.{station_key}.{camera_key}.robot_data"
    return result


# =============================
# INTEGRATION WRAPPER FUNCTION
# =============================
def classify_defect(class_name: str, confidence: float = None, mes_data_path: str = MES_DATA_PATH) -> Dict[str, Any]:
    """
    Reads MES data from JSON, creates a defect instance populated with
    those values, and returns the full defect record as a dict.

    Args:
        class_name (str): The class name detected by YOLO
        confidence (float, optional): Confidence score (0.0-1.0)
        mes_data_path (str): Path to the MES JSON file

    Returns:
        dict: Basic info + causes dict + confidence
    """
    # Load MES data
    mes_data = load_mes_data(mes_data_path)

    mes_record = _select_mes_record_for_class(class_name, mes_data)

    # Create defect object populated with MES values
    defect = DefectFactory.create_defect(class_name, mes_record)

    # Build result from the defect's full dict + confidence
    result = defect.to_dict()
    result['confidence'] = confidence
    return result


# =============================
# SIMULATED YOLO OUTPUT (for testing)
# =============================
def get_yolo_detections():
    """
    Simulates YOLO output. In a real system this comes from the model.

    Args:
        None

    Returns:
        List[str]: Example class names for local testing.
    """
    return ["burr", "edge_weld", "spatter", "dark_weld_spot"]


# =============================
# MAIN PROGRAM
# =============================
def main():
    print("Starting defect detection system...\n")

    detections = get_yolo_detections()

    for detection in detections:
        print(f"Detected: {detection}")
        result = classify_defect(detection, confidence=0.95)
        print(json.dumps(result, indent=2))
        print("\n" + "-" * 40 + "\n")


# -----------------------------
# RUN PROGRAM
# -----------------------------
if __name__ == "__main__":
    main()