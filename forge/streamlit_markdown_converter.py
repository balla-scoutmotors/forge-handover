"""
Streamlit integration for automatic JSON to Markdown conversion.
Added this to streamlit_app.py to enable automatic markdown generation.
"""

import json
import os
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any
import streamlit as st
import pandas as pd


class StreamlitJSONToMarkdown:
    """Streamlit-compatible JSON to Markdown converter for Qdrant."""

    @staticmethod
    def format_event_as_markdown(event: Dict[str, Any], index: int = 1) -> str:
        """
        Format a single event as markdown.
        
        Args:
            event: Event dictionary
            index: Event number
            
        Returns:
            Markdown string
        """
        md = f"## Event #{index}: {event.get('defect_name', 'Unknown')}\n\n"
        md += f"- **Event ID:** {event.get('event_id', 'N/A')}\n\n"

        # Basic Information
        md += "### Basic Information\n"
        md += f"- **Defect Name:** {event.get('defect_name', 'N/A')}\n"
        md += f"- **Class:** {event.get('class_name', 'N/A')}\n"
        md += f"- **Confidence:** {event.get('confidence', 'N/A'):.2%}\n\n"

        # Location & Identification
        md += "### Location & Identification\n"
        md += f"- **Cell Number:** {event.get('cell_number', 'N/A')}\n"
        md += f"- **Assembly Number:** {event.get('assembly_number', 'N/A')}\n"
        md += f"- **Station:** {event.get('station', 'N/A')}\n"
        md += f"- **Camera:** {event.get('camera', 'N/A')}\n\n"

        # Operator & Source Information
        md += "### Operator & Source\n"
        md += f"- **Operator:** {event.get('operator', 'N/A')}\n"
        md += f"- **Source Path:** `{event.get('source_path', 'N/A')}`\n\n"

        # Timing Information
        md += "### Timing Information\n"
        md += f"- **Detection Time:** {event.get('time', 'N/A')}\n"
        md += f"- **Event Time:** {event.get('event_time', 'N/A')}\n"
        md += f"- **Frame Index:** {event.get('frame_index', 'N/A')}\n"
        md += f"- **Track ID:** {event.get('track_id', 'N/A')}\n\n"

        # Root Causes Analysis
        causes = event.get('causes', {})
        if causes:
            md += "### Root Causes\n"
            for cause, value in causes.items():
                cause_display = cause.replace('_', ' ').title()
                md += f"- **{cause_display}:** {value}\n"
            md += "\n"

        md += "---\n\n"
        return md

    @staticmethod
    def json_events_to_markdown(events_data: List[Dict] | Dict) -> str:
        """
        Convert JSON events to markdown.
        
        Args:
            events_data: List of event dictionaries or single event
            
        Returns:
            Markdown string
        """
        # Handle single event
        if isinstance(events_data, dict):
            events_data = [events_data]

        markdown = f"# Detected Events Report\n"
        markdown += f"*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*\n\n"
        markdown += f"**Total Events:** {len(events_data)}\n\n"

        for idx, event in enumerate(events_data, 1):
            markdown += StreamlitJSONToMarkdown.format_event_as_markdown(event, idx)

        return markdown

    @staticmethod
    def json_to_markdown_file(json_data: str | bytes, output_path: str = None) -> str:
        """
        Convert JSON string/bytes to markdown file.
        
        Args:
            json_data: JSON string or bytes
            output_path: Output file path (auto-generated if None)
            
        Returns:
            Path to created markdown file
        """
        try:
            if isinstance(json_data, bytes):
                events = json.loads(json_data.decode('utf-8'))
            else:
                events = json.loads(json_data)
        except json.JSONDecodeError as e:
            st.error(f"Invalid JSON: {e}")
            return None

        markdown_content = StreamlitJSONToMarkdown.json_events_to_markdown(events)

        if output_path is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = f"detected_events_{timestamp}.md"

        try:
            with open(output_path, "w") as f:
                f.write(markdown_content)
            return output_path
        except Exception as e:
            st.error(f"Error saving markdown: {e}")
            return None


def add_json_to_markdown_controls(streamlit_app):
    """
    Add JSON to Markdown conversion controls to your Streamlit app.
    
    Example usage in your streamlit_app.py:
    ```python
    from streamlit_markdown_converter import add_json_to_markdown_controls
    
    st.set_page_config(page_title="Defect Detection", layout="wide")
    
    # Your existing code...
    
    # Add conversion controls
    with st.sidebar:
        add_json_to_markdown_controls(st)
    ```
    """
    with streamlit_app.sidebar:
        streamlit_app.markdown("---")
        streamlit_app.subheader("📄 Export to Markdown")

        # JSON file upload
        uploaded_json = streamlit_app.file_uploader(
            "Upload streamlit_detected_events.json",
            type="json",
            key="json_upload"
        )

        if uploaded_json is not None:
            # Read and convert
            json_content = uploaded_json.read()
            markdown_content = StreamlitJSONToMarkdown.json_events_to_markdown(
                json.loads(json_content)
            )

            # Display preview
            with streamlit_app.expander("📋 Preview Markdown"):
                streamlit_app.markdown(markdown_content)

            # Download button
            streamlit_app.download_button(
                label="⬇️ Download as Markdown",
                data=markdown_content,
                file_name=f"detected_events_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md",
                mime="text/markdown",
                key="md_download"
            )

            streamlit_app.success("✓ Ready to download!")
        else:
            streamlit_app.info("Upload JSON file to convert to Markdown")


# Standalone Streamlit app
if __name__ == "__main__":
    st.set_page_config(
        page_title="JSON to Markdown Converter",
        page_icon="📄",
        layout="wide"
    )

    st.title("📄 JSON to Markdown Converter")
    st.markdown("Convert streamlit_detected_events.json to Markdown format for Qdrant")

    col1, col2 = st.columns([2, 1])

    with col1:
        uploaded_file = st.file_uploader(
            "Upload JSON file",
            type="json",
            help="Upload streamlit_detected_events.json"
        )

        if uploaded_file is not None:
            try:
                json_data = json.load(uploaded_file)
                markdown_output = StreamlitJSONToMarkdown.json_events_to_markdown(json_data)

                st.markdown("### Preview")
                st.markdown(markdown_output)

                st.markdown("### Download")
                st.download_button(
                    label="📥 Download Markdown File",
                    data=markdown_output,
                    file_name=f"detected_events_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md",
                    mime="text/markdown"
                )
            except Exception as e:
                st.error(f"Error processing file: {e}")

    with col2:
        st.markdown("### Info")
        st.info(
            "This tool converts detected events JSON to Markdown format "
            "suitable for uploading to Qdrant vector database."
        )
        st.markdown("### Features")
        st.markdown(
            """
            - ✓ Structured markdown format
            - ✓ Event indexing
            - ✓ Metadata organization
            - ✓ Root cause analysis
            - ✓ Direct download
            """
        )
