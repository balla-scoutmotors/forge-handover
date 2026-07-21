# --- Standard library imports ---
import os                              # Filesystem paths and environment variables.
import json                           # Serialise detection events to JSON artifacts.
from datetime import datetime         # Timestamps for events and output filenames.
from pathlib import Path              # Cross-platform path handling (e.g. Downloads folder).
from typing import Any, Dict, List    # Type hints for readability.
from PIL import Image, ImageDraw, ImageOps  # Load images, draw overlays, honor EXIF orientation.

# --- Third-party imports ---
import streamlit as st                # Web UI framework for the app.
from ultralytics import YOLO          # YOLO model for defect detection inference.

# --- Local application imports ---
# Defect classification helpers and MES (Manufacturing Execution System) data access.
from defect_classes import (
    DefectFactory,                    # Provides the set of supported defect class names.
    classify_defect_for_path,         # Builds a structured event for a defect + camera path.
    get_camera_context,               # Resolves a station/camera path in the MES data.
    list_station_camera_paths,        # Lists valid station -> camera paths from MES data.
    load_mes_data,                    # Loads the simple_mes.json configuration.
)
from detection_event_publisher import DetectionEventPublisher  # Persists/publishes events (local + S3).
from streamlit_markdown_converter import StreamlitJSONToMarkdown  # Converts event JSON to Markdown.
from defect_weld_resolver import (
    get_step_weld_points,             # Returns weld coordinates for a part/step from the weld map.
    list_local_weld_map_steps,        # Lists available (part_type, step) pairs in weld_match.json.
    parse_bbox,                       # Extracts (x, y, w, h) from a YOLO box.
    resolve_nearest_weld,             # Correlates each defect to its nearest weld point.
)


# --- Module-level paths ---
BASE_DIR = os.path.dirname(__file__)                              # Directory containing this script.
DEFAULT_MODEL_PATH = os.path.join(BASE_DIR, "star-circle-line.pt")  # Default YOLO weights.
MES_PATH = os.path.join(BASE_DIR, "simple_mes.json")             # MES configuration file path.


def persist_run_outputs(events: List[Dict[str, Any]], output_dir: str) -> Dict[str, str]:
    """Persist both JSON and Markdown artifacts for a detection run."""
    # Ensure the output directory exists before writing any files.
    os.makedirs(output_dir, exist_ok=True)

    # Build timestamped filenames so each run produces unique artifacts.
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = os.path.join(output_dir, f"streamlit_detected_events_{timestamp}.json")
    md_path = os.path.join(output_dir, f"streamlit_detected_events_{timestamp}.md")

    # Write the raw events as pretty-printed JSON.
    with open(json_path, "w", encoding="utf-8") as json_file:
        json.dump(events, json_file, indent=2)

    # Convert the same events to a human-readable Markdown report and save it.
    markdown_content = StreamlitJSONToMarkdown.json_events_to_markdown(events)
    with open(md_path, "w", encoding="utf-8") as md_file:
        md_file.write(markdown_content)

    # Keep a stable latest JSON filename for downstream automations.
    latest_json_path = os.path.join(output_dir, "streamlit_detected_events.json")
    with open(latest_json_path, "w", encoding="utf-8") as latest_json_file:
        json.dump(events, latest_json_file, indent=2)

    # Return the paths of all written artifacts for reporting in the UI.
    return {
        "json_path": json_path,
        "md_path": md_path,
        "latest_json_path": latest_json_path,
    }


@st.cache_resource  # Cache the loaded model so it is not reloaded on every Streamlit rerun.
def load_model(model_path: str) -> YOLO:
    # Instantiate and return the YOLO model from the given weights path.
    return YOLO(model_path)


# Colours (RGB) for the reasoning overlay.
_COLOR_WELD = (30, 144, 255)       # weld position from the map (blue)
_COLOR_DEFECT = (0, 200, 0)        # detected defect (green)
_COLOR_UNRESOLVED = (220, 50, 50)  # defect with no weld map (red)
_COLOR_LINE = (0, 200, 0)          # defect → nearest weld link (green)


def build_reasoning_overlay(
    base_image: Image.Image,
    detections: List[Dict[str, Any]],
    weld_points: List[Dict[str, Any]],
) -> Image.Image:
    """
    Draw the defect → nearest-weld correlation onto a copy of the image.

    Visual legend:
      * Blue dot + id    : weld position from the JSON map.
      * Green box/dot     : detected defect.
      * Green line + dist : link from the defect to its NEAREST weld.
      * Red box           : defect with no weld map available.
    """
    # Work on an RGB copy so the original image is never mutated.
    overlay = base_image.convert("RGB").copy()
    draw = ImageDraw.Draw(overlay)
    r = max(4, int(min(overlay.size) * 0.006))  # marker radius scales with image

    # 1. Weld positions from the map (blue dots + weld id labels).
    for wp in weld_points:
        wx, wy = wp["x"], wp["y"]
        draw.ellipse([wx - r, wy - r, wx + r, wy + r], fill=_COLOR_WELD)
        draw.text((wx + r + 2, wy - r - 2), wp["weld_id"], fill=_COLOR_WELD)

    # 2. Defects: boxes, centres, and line to nearest weld.
    for det in detections:
        # Extract the defect bounding box, falling back to zeros if missing.
        bb = det.get("bounding_box", {})
        x, y, w, h = bb.get("x", 0), bb.get("y", 0), bb.get("w", 0), bb.get("h", 0)
        # Use the precomputed centre if present, otherwise derive it from the box.
        cx, cy = det.get("center_x", x + w / 2), det.get("center_y", y + h / 2)
        # Green if a weld was matched, red if the defect could not be resolved.
        matched = det.get("weld_matched", False)
        color = _COLOR_DEFECT if matched else _COLOR_UNRESOLVED

        # Draw the defect bounding box, its centre marker, and its class label.
        draw.rectangle([x, y, x + w, y + h], outline=color, width=2)
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
        draw.text((x, max(0, y - 12)), det.get("class_name", "defect"), fill=color)

        # If matched, draw a line to the nearest weld and label it with the distance.
        if matched and det.get("weld_cap_x") is not None:
            wx, wy = det["weld_cap_x"], det["weld_cap_y"]
            draw.line([cx, cy, wx, wy], fill=_COLOR_LINE, width=2)
            dist = det.get("weld_match_distance_px")
            mid_x, mid_y = (cx + wx) / 2, (cy + wy) / 2  # Label at the midpoint of the link.
            draw.text(
                (mid_x, mid_y),
                f"{det.get('weld_id')}  {dist}px",
                fill=_COLOR_LINE,
            )

    # Return the annotated image for display in the UI.
    return overlay


def run_detection_on_uploaded_images(
    model: YOLO,
    mes_data: Dict[str, Any],
    station_key: str,
    camera_keys: List[str],
    conf_threshold: float,
    uploaded_files: List[Any],
    event_publisher: DetectionEventPublisher,
    part_type: str,
    step: int,
) -> Dict[str, Any]:
    supported_classes = DefectFactory.supported_class_names()

    # Accumulators for the events produced and the publishing outcome counters.
    events: List[Dict[str, Any]] = []
    local_published_events = 0
    s3_uploaded_events = 0
    publish_failures = 0
    # Streamlit placeholders that are updated live as images are processed.
    progress = st.progress(0)
    run_status = st.empty()
    events_table = st.empty()
    image_results = st.empty()

    # Nothing to do if no files were uploaded; return zeroed counters.
    if not uploaded_files:
        return {
            "events": events,
            "local_published_events": local_published_events,
            "s3_uploaded_events": s3_uploaded_events,
            "publish_failures": publish_failures,
        }

    # Process each uploaded image independently.
    for image_index, uploaded_file in enumerate(uploaded_files):
        try:
            # exif_transpose honors the camera's EXIF orientation tag so phone
            # photos are not displayed flipped/rotated relative to the file.
            image = ImageOps.exif_transpose(Image.open(uploaded_file)).convert("RGB")
        except Exception:
            # Skip unreadable/corrupt images and count them as failures.
            publish_failures += 1
            continue

        # Run YOLO inference for a single uploaded image.
        results = model.predict(source=image, conf=conf_threshold, verbose=False)
        if not results:
            # No result object returned; update progress and move on.
            progress.progress(min((image_index + 1) / len(uploaded_files), 1.0))
            run_status.info(
                f"Images processed: {image_index + 1}/{len(uploaded_files)} | Events emitted: {len(events)}"
            )
            continue

        image_result = results[0]  # Single image -> single result.

        if image_result.boxes is not None and len(image_result.boxes) > 0:
            # Build raw defect detections, then correlate each to its
            # nearest weld coordinate from the map.
            detections: List[Dict[str, Any]] = []
            for box in image_result.boxes:
                # Decode the class id, human-readable name, and confidence score.
                class_id = int(box.cls[0])
                class_name = str(model.names[class_id]).strip().lower()
                confidence = float(box.conf[0])

                # Ignore unsupported classes or detections below the threshold.
                if class_name not in supported_classes or confidence < conf_threshold:
                    continue

                # Convert the box to (x, y, w, h) and record the detection.
                bx, by, bw, bh = parse_bbox(box)
                detections.append(
                    {
                        "class_name": class_name,
                        "confidence": confidence,
                        "bounding_box": {"x": bx, "y": by, "w": bw, "h": bh},
                    }
                )

            # Correlate each defect to its nearest weld (no cutoff, many-to-one).
            resolve_nearest_weld(detections, part_type=part_type, step=step)

            # Visualise the correlation: weld map points, defect boxes, and
            # the line from each defect to its nearest weld + distance.
            weld_points = get_step_weld_points(part_type, step)
            overlay = build_reasoning_overlay(image, detections, weld_points)
            image_results.image(
                overlay,
                caption=(
                    f"Defect → nearest weld: {uploaded_file.name} "
                    f"(green = defect, blue = weld, line = nearest weld)"
                ),
                use_container_width=True,
            )

            # Per-image reasoning summary table shown to the user.
            reasoning_rows = [
                {
                    "defect": d["class_name"],
                    "confidence": round(d["confidence"], 3),
                    "center": (round(d.get("center_x") or 0, 1), round(d.get("center_y") or 0, 1)),
                    "nearest_weld": d["weld_id"],
                    "weld_location": d["weld_stud_location"],
                    "distance_px": d["weld_match_distance_px"],
                }
                for d in detections
            ]
            if reasoning_rows:
                st.dataframe(reasoning_rows, use_container_width=True)

            # Turn each defect detection into one event per selected camera.
            for detection in detections:
                class_name = detection["class_name"]
                confidence = detection["confidence"]

                for camera_key in camera_keys:
                    # Build the base classified event for this defect + camera path.
                    event = classify_defect_for_path(
                        class_name=class_name,
                        mes_data=mes_data,
                        station_key=station_key,
                        camera_key=camera_key,
                        confidence=confidence,
                    )
                    # Enrich the event with runtime metadata and weld correlation.
                    event["event_time"] = datetime.now().isoformat(timespec="seconds")
                    event["frame_index"] = image_index
                    event["track_id"] = None
                    event["image_name"] = uploaded_file.name
                    event["part_type"] = part_type
                    event["step_number"] = step
                    event["bounding_box"] = detection["bounding_box"]
                    event["weld_id"] = detection["weld_id"]
                    event["weld_stud_location"] = detection["weld_stud_location"]
                    event["weld_match_distance_px"] = detection["weld_match_distance_px"]
                    event["weld_matched"] = detection["weld_matched"]

                    try:
                        # Publish locally (and optionally to S3); track outcomes.
                        publish_result = event_publisher.publish_event(event)
                        local_published_events += 1
                        if publish_result.get("uploaded_to_s3"):
                            s3_uploaded_events += 1
                    except Exception:
                        publish_failures += 1

                    events.append(event)

        # Update the progress bar and status line after each image.
        progress.progress(min((image_index + 1) / len(uploaded_files), 1.0))
        run_status.info(
            f"Images processed: {image_index + 1}/{len(uploaded_files)} | Events emitted: {len(events)}"
        )

        # Show a rolling preview of the most recent events.
        if events:
            events_table.dataframe(events[-30:], use_container_width=True)

    # Return the collected events and publishing statistics.
    return {
        "events": events,
        "local_published_events": local_published_events,
        "s3_uploaded_events": s3_uploaded_events,
        "publish_failures": publish_failures,
    }


def main() -> None:
    # Configure the page and render the header/description.
    st.set_page_config(page_title="Defect → Weld Correlator", layout="wide")
    st.title("Defect → Weld Correlator")
    st.caption(
        "Upload image(s); detect defects with the star-circle-line model, then "
        "label each defect with its nearest weld from the coordinate map."
    )

    # Load the MES configuration and derive the valid station -> camera paths.
    mes_data = load_mes_data(MES_PATH)
    available_paths = list_station_camera_paths(mes_data)

    # Without any valid paths there is nothing to configure; stop early.
    if not available_paths:
        st.error(
            "No valid station/camera paths found under body_shop in simple_mes.json. "
            "Detection controls are hidden until this is fixed."
        )
        return

    # --- Sidebar: all run configuration lives here. ---
    with st.sidebar:
        st.header("Configuration")
        # Choose the station and the cameras available for it.
        station_key = st.selectbox("Station", options=sorted(available_paths.keys()))
        camera_options = available_paths.get(station_key, [])

        # A station with no cameras cannot run inference.
        if not camera_options:
            st.error(
                f"No camera paths for {station_key}. Run controls are hidden by design."
            )
            return

        selected_cameras = st.multiselect(
            "Cameras",
            options=camera_options,
            default=[camera_options[0]],
        )

        # Require at least one camera before continuing.
        if not selected_cameras:
            st.warning("Select at least one valid camera to enable inference.")
            return

        # Strict guard: verify each selected path exists before exposing run controls.
        invalid_paths = []
        for camera_key in selected_cameras:
            try:
                get_camera_context(mes_data, station_key, camera_key)
            except KeyError as ex:
                invalid_paths.append(str(ex))

        # If any selected path is invalid, hide the run controls.
        if invalid_paths:
            st.error("Invalid station/camera path detected. Run controls hidden.")
            for message in invalid_paths:
                st.caption(message)
            return

        # Model weights path and the confidence threshold for detections.
        model_path_input = st.text_input("YOLO model path", value=DEFAULT_MODEL_PATH)
        conf_threshold = st.slider("Confidence threshold", 0.10, 0.99, 0.55, 0.01)

        st.markdown("---")
        st.subheader("Weld map")
        # Load available (part_type, step) combinations from the weld map.
        weld_map_steps = list_local_weld_map_steps()
        part_type = None
        step = None
        if not weld_map_steps:
            st.warning(
                "No weld map entries found in weld_match.json. "
                "Defects cannot be correlated to welds."
            )
        else:
            # Let the user pick a part type, then a capture step within it.
            part_types = sorted({pt for pt, _ in weld_map_steps})
            part_type = st.selectbox("Part type", options=part_types)
            available_steps = sorted({s for pt, s in weld_map_steps if pt == part_type})
            step = st.selectbox("Capture step", options=available_steps)

        # Output folder for artifacts plus S3 upload configuration.
        default_output_dir = str(Path.home() / "Downloads")
        output_dir = st.text_input("Auto-save output folder", value=default_output_dir)
        s3_bucket = st.text_input("S3 bucket", value=os.getenv("S3_BUCKET_NAME", "forge-project-data"))
        s3_prefix = st.text_input("S3 key prefix", value=os.getenv("FORGE_S3_PREFIX", "detections"))
        default_enable_s3 = os.getenv("FORGE_S3_UPLOAD_ENABLED", "true").lower() in {"1", "true", "yes"}
        s3_upload_enabled = st.checkbox("Enable S3 upload", value=default_enable_s3)

    # --- Main panel: image upload and active selection summary. ---
    uploaded_images = st.file_uploader(
        "Upload image(s)",
        type=["jpg", "jpeg", "png", "bmp", "webp"],
        accept_multiple_files=True,
    )

    # Show the currently selected station/cameras and derived MES data paths.
    st.subheader("Active Selection")
    st.write(
        {
            "station": station_key,
            "cameras": selected_cameras,
            "source_paths": [
                f"body_shop.{station_key}.{camera}.robot_data" for camera in selected_cameras
            ],
        }
    )

    # --- Run button: validate inputs, load model, run detection, save output. ---
    if st.button("Run YOLO on Uploaded Images", type="primary"):
        # Require at least one uploaded image.
        if not uploaded_images:
            st.error("Upload at least one image to run inference.")
            return

        # A part type and step are needed to correlate defects to welds.
        if part_type is None or step is None:
            st.error("Select a part type and capture step before running inference.")
            return

        # Validate that the model path exists (allowing the auto-download alias).
        if not os.path.exists(model_path_input) and model_path_input not in {"yolo11n.pt"}:
            st.error(f"Model path not found: {model_path_input}")
            return

        # Load the (cached) YOLO model, surfacing any load errors to the user.
        try:
            model = load_model(model_path_input)
        except Exception as ex:
            st.error(f"Failed to load model: {ex}")
            return

        # If S3 upload is enabled, a bucket name is required.
        if not s3_bucket and s3_upload_enabled:
            st.error("S3 upload is enabled but bucket name is empty.")
            return

        # Initialise the publisher responsible for local + S3 persistence.
        try:
            event_publisher = DetectionEventPublisher(
                output_dir=output_dir,
                s3_bucket=s3_bucket,
                s3_prefix=s3_prefix,
                s3_upload_enabled=s3_upload_enabled,
            )
        except Exception as ex:
            st.error(f"Failed to initialize event publisher: {ex}")
            return

        st.info("Running defect detection and weld correlation on uploaded images.")

        # Execute the full detection + correlation pipeline over all images.
        try:
            detection_result = run_detection_on_uploaded_images(
                model=model,
                mes_data=mes_data,
                station_key=station_key,
                camera_keys=selected_cameras,
                conf_threshold=conf_threshold,
                uploaded_files=uploaded_images,
                event_publisher=event_publisher,
                part_type=part_type,
                step=step,
            )
        except KeyError as ex:
            # A guard inside the pipeline rejected an invalid path.
            st.error(f"Guard stopped run: {ex}")
            return
        except Exception as ex:
            st.error(f"Runtime error: {ex}")
            return

        events = detection_result["events"]

        # Persist the aggregated JSON/Markdown artifacts for the whole run.
        try:
            output_paths = persist_run_outputs(events, output_dir=output_dir)
        except Exception as ex:
            st.error(f"Could not auto-save artifacts: {ex}")
            return

        # Report a full summary of the run to the user.
        st.success(
            "Run complete. "
            f"Events emitted: {len(events)} | "
            f"JSON saved: {output_paths['json_path']} | "
            f"MD saved: {output_paths['md_path']} | "
            f"Per-event local artifacts: {detection_result['local_published_events']} | "
            f"Per-event S3 uploads: {detection_result['s3_uploaded_events']} | "
            f"Publish failures: {detection_result['publish_failures']}"
        )


if __name__ == "__main__":
    # Entry point when the script is run directly with Streamlit.
    main()
