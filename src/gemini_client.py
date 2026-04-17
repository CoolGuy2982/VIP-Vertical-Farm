import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()

logger = logging.getLogger(__name__)

GROWER_TOOLS = [
    {
        "name": "capture_plant",
        "description": (
            "Take a photo of the plant with the plant camera. "
            "Look for: leaf color, turgor pressure (wilting/rigidity), stem posture, "
            "signs of disease (spots, mold, discoloration), new growth, root visibility. "
            "Call this every check-in and after any action you want visual confirmation of."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "capture_dashboard",
        "description": (
            "Take a photo of the grow tent's sensor dashboard with the dashboard camera. "
            "Read the temperature, humidity, soil moisture, and any other values shown. "
            "After reading the dashboard, ALWAYS call report_sensors with the values you see. "
            "Call this at the start of every check-in to get current conditions."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "report_sensors",
        "description": (
            "Report the sensor values you read from the dashboard camera image. "
            "Call this immediately after capture_dashboard with the values you extracted. "
            "This stores them for trend tracking and history."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "temperature_c": {
                    "type": "number",
                    "description": "Temperature in Celsius from the dashboard",
                },
                "humidity_pct": {
                    "type": "number",
                    "description": "Relative humidity percentage from the dashboard",
                },
                "soil_moisture_pct": {
                    "type": "number",
                    "description": "Soil moisture percentage if shown on dashboard",
                },
                "light_lux": {
                    "type": "number",
                    "description": "Light level if shown on dashboard",
                },
                "other_readings": {
                    "type": "string",
                    "description": "Any other values visible on the dashboard",
                },
            },
        },
    },
    {
        "name": "run_pump",
        "description": (
            "Run the water pump for a precise number of seconds via the Kasa cloud. "
            "IMPORTANT: The cloud API adds ~2-3s of latency on both ON and OFF commands — "
            "always add 5s of buffer to your target duration. "
            "A full 18-row tray watering requires ~40 seconds; anything under 20s will not "
            "reach all rows. Start at 40s for a full water and adjust based on observed "
            "soil moisture response. NEVER run if soil moisture is above 65%. Always log the reason."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "number",
                    "description": "Pump run duration in seconds (1-60). Be precise.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this duration was chosen (soil moisture level, stage, etc.)",
                },
            },
            "required": ["seconds", "reason"],
        },
    },
    {
        "name": "turn_on_lights",
        "description": (
            "Turn on the grow lights relay for a specified number of minutes. "
            "Lights auto-off after the duration. Consider: current time of day, "
            "daily light integral (DLI) target for this stage, and the plant's "
            "circadian rhythm. Seedlings: 14-16h/day. Vegetative: 16-18h/day. "
            "Always give the plant at least 6h of darkness. "
            "If lights are already on, this resets the timer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "minutes": {
                    "type": "number",
                    "description": "How long to keep lights on (minutes). Max 1440.",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this duration (photoperiod strategy, DLI target, etc.)",
                },
            },
            "required": ["minutes", "reason"],
        },
    },
    {
        "name": "turn_off_lights",
        "description": (
            "Turn the grow lights off immediately. "
            "Use this to enforce darkness period or respond to high temperature."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Why turning off now"},
            },
            "required": ["reason"],
        },
    },
    {
        "name": "observe_in",
        "description": (
            "Schedule a future observation: the system will capture a photo and "
            "read all sensors at the specified delay, then call you back with the "
            "results. This is how you close feedback loops, e.g. water the plant "
            "then observe in 20 minutes to see if soil moisture rose as expected, "
            "or turn on lights and observe in 2 hours to check temperature impact. "
            "Use this aggressively to learn cause-and-effect for THIS specific setup."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "delay_minutes": {
                    "type": "number",
                    "description": "Minutes until the observation fires. Can be fractional (0.5 = 30 seconds).",
                },
                "context": {
                    "type": "string",
                    "description": (
                        "Why you're observing and what you expect to see. "
                        "Be specific: 'After 8s pump run, expecting soil moisture to rise from 32% to ~45%.' "
                        "This context is shown back to you with the observation result."
                    ),
                },
            },
            "required": ["delay_minutes", "context"],
        },
    },
    {
        "name": "schedule_checkin",
        "description": (
            "Schedule your next full check-in cycle. "
            "Choose interval based on urgency and plant state: "
            "5-15 min when actively monitoring stress or after a major intervention, "
            "30-60 min for seedlings or after watering, "
            "2-4 hours for stable vegetative plants, "
            "up to 8 hours overnight if conditions are stable. "
            "You should call this at the end of EVERY check-in."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "minutes": {
                    "type": "integer",
                    "description": "Minutes until next check-in (5-480).",
                },
                "reason": {
                    "type": "string",
                    "description": "Why this interval.",
                },
            },
            "required": ["minutes", "reason"],
        },
    },
    {
        "name": "get_pending_actions",
        "description": (
            "See all currently scheduled future actions (observations, check-ins, etc.). "
            "Use this to avoid double-scheduling or to check what's coming up."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "cancel_action",
        "description": "Cancel a previously scheduled action by its ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "action_id": {"type": "string", "description": "The action ID to cancel"},
            },
            "required": ["action_id"],
        },
    },
    {
        "name": "log_milestone",
        "description": (
            "Record a significant growth milestone. Log whenever you observe: "
            "germination, cotyledon emergence, first true leaves, each new node, "
            "flowering, fruiting, size measurements, or any significant change. "
            "Include quantitative measurements when possible."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Detailed description of what you observed",
                },
                "stage": {
                    "type": "string",
                    "description": "Current growth stage",
                },
                "measurements": {
                    "type": "object",
                    "description": "Quantitative measurements",
                    "properties": {
                        "height_cm": {"type": "number"},
                        "leaf_count": {"type": "integer"},
                        "node_count": {"type": "integer"},
                        "stem_diameter_mm": {"type": "number"},
                        "canopy_width_cm": {"type": "number"},
                        "health_score": {
                            "type": "integer",
                            "description": "Your assessment 1-10 (1=critical, 10=perfect)",
                        },
                    },
                },
            },
            "required": ["description", "stage"],
        },
    },
    {
        "name": "get_growth_history",
        "description": "Retrieve all recorded growth milestones and measurement history.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "get_sensor_history",
        "description": "Get sensor trend statistics (min/max/avg) over the last N hours.",
        "parameters": {
            "type": "object",
            "properties": {
                "hours": {
                    "type": "integer",
                    "description": "Hours to look back (1-168)",
                },
            },
            "required": ["hours"],
        },
    },
    {
        "name": "get_decision_log",
        "description": "Retrieve your recent decisions and the reasoning behind them.",
        "parameters": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "description": "Number of decisions to retrieve"},
            },
            "required": ["count"],
        },
    },
    {
        "name": "emergency_alert",
        "description": (
            "Log a critical alert. Use for: hardware failure, extreme temperatures "
            "(below 5°C or above 40°C), sensor disconnection, pump malfunction, "
            "signs of serious disease, or anything requiring immediate human attention."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Alert description"},
                "severity": {
                    "type": "string",
                    "enum": ["warning", "critical"],
                },
            },
            "required": ["message", "severity"],
        },
    },
]


def _build_tools() -> list[types.Tool]:
    return [types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name=t["name"],
            description=t["description"],
            parameters=t["parameters"],
        )
        for t in GROWER_TOOLS
    ])]


def _clean_result(obj):
    """Strip None, empty lists, and empty dicts so Gemini doesn't choke."""
    if isinstance(obj, dict):
        return {k: _clean_result(v) for k, v in obj.items()
                if v is not None and v != [] and v != {}}
    if isinstance(obj, list):
        return [_clean_result(item) for item in obj]
    return obj


class GeminiClient:
    def __init__(self, config: dict):
        self.config = config
        gemini_cfg = config.get("gemini", {})

        api_key = os.environ.get("GEMINI_API_KEY", "")
        self.client = genai.Client(api_key=api_key) if api_key else genai.Client()

        self.model      = gemini_cfg.get("model", "gemini-3-flash-preview")
        self.max_tokens = gemini_cfg.get("max_output_tokens", 4096)
        self.thinking   = gemini_cfg.get("thinking", True)

        # Accumulated conversation for the current tool loop.
        # Reset at the start of each create_interaction call.
        self._contents: list[types.Content] = []
        self._system_instruction: str = ""
        self._use_tools: bool = True

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _make_config(self, system_instruction: str, use_tools: bool) -> types.GenerateContentConfig:
        kwargs: dict = {
            "system_instruction": system_instruction,
            "max_output_tokens": self.max_tokens,
        }
        if use_tools:
            kwargs["tools"] = _build_tools()
        if self.thinking:
            # -1 = dynamic budget (model decides how much to think)
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=-1)
        return types.GenerateContentConfig(**kwargs)

    def _call(self) -> types.GenerateContentResponse:
        return self.client.models.generate_content(
            model=self.model,
            contents=self._contents,
            config=self._make_config(self._system_instruction, self._use_tools),
        )

    # ── Public interface (matches what ai_grower._run_tool_loop expects) ──────

    def create_interaction(
        self,
        system_instruction: str,
        user_message: str,
        image_paths: Optional[list[str]] = None,
        use_tools: bool = True,
        continue_chain: bool = True,   # kept for API compat, no longer used
    ) -> types.GenerateContentResponse:
        """Start a fresh generate_content conversation and return the first response."""
        parts: list[types.Part] = []

        for image_path in (image_paths or []):
            if image_path and Path(image_path).exists():
                with open(image_path, "rb") as f:
                    image_bytes = f.read()
                ext = Path(image_path).suffix.lower()
                mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                            ".png": "image/png", ".webp": "image/webp"}
                mime_type = mime_map.get(ext, "image/jpeg")
                parts.append(types.Part.from_bytes(data=image_bytes, mime_type=mime_type))

        parts.append(types.Part.from_text(text=user_message))

        self._system_instruction = system_instruction
        self._use_tools = use_tools
        self._contents = [types.Content(role="user", parts=parts)]

        logger.info("Gemini generate_content (model=%s, tools=%s)", self.model, use_tools)
        response = self._call()

        # Append model turn to history so the next submit_tool_results continues correctly
        self._contents.append(response.candidates[0].content)
        return response

    def submit_tool_results(
        self,
        interaction_id: Optional[str],   # kept for API compat, not used
        tool_results: list[dict],
        system_instruction: str,
    ) -> types.GenerateContentResponse:
        """Append function responses to the conversation and get the next model turn."""
        parts: list[types.Part] = []
        for result in tool_results:
            raw = result["result"]
            cleaned = _clean_result(raw) if isinstance(raw, dict) else {"result": str(raw)}
            parts.append(types.Part.from_function_response(
                name=result["name"],
                response=cleaned,
            ))

        self._contents.append(types.Content(role="user", parts=parts))

        response = self._call()
        self._contents.append(response.candidates[0].content)
        return response

    def reset_chain(self):
        """Clear conversation history (called after context compression)."""
        self._contents = []
        self._system_instruction = ""
        logger.info("Gemini conversation history cleared")

    def extract_response(self, response: types.GenerateContentResponse) -> dict:
        """Parse a GenerateContentResponse into a flat dict for ai_grower."""
        result: dict = {
            "text": None,
            "function_calls": [],
            "thoughts": [],
            "status": "completed",
            "interaction_id": None,   # no server-side ID with standard API
        }

        if not response.candidates:
            logger.warning("Gemini returned no candidates")
            return result

        for part in response.candidates[0].content.parts:
            # Thinking / reasoning traces
            if getattr(part, "thought", False):
                if part.text:
                    result["thoughts"].append(part.text)
            # Tool calls
            elif part.function_call:
                fn = part.function_call
                args = dict(fn.args) if fn.args else {}
                result["function_calls"].append({
                    "id":        getattr(fn, "id", "") or "",
                    "name":      fn.name,
                    "arguments": args,
                })
            # Text response
            elif part.text:
                result["text"] = (result["text"] or "") + part.text

        return result
