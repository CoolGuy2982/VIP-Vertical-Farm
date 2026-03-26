import base64
import logging
import os
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from google import genai

load_dotenv()

logger = logging.getLogger(__name__)

GROWER_TOOLS = [
    {
        "name": "read_sensors",
        "description": (
            "Read all current sensor values: temperature (°C), humidity (%), "
            "soil moisture (%), and ambient light level (lux). "
            "Call this at the start of every check-in and before/after any action "
            "to establish a baseline."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "capture_image",
        "description": (
            "Take a photo of the plant right now and analyze it. "
            "Look for: leaf color, turgor pressure (wilting/rigidity), stem posture, "
            "signs of disease (spots, mold, discoloration), new growth, root visibility. "
            "Call this every check-in and after any action you want visual confirmation of."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "run_pump",
        "description": (
            "Run the water pump relay for a precise number of seconds. "
            "The pump delivers water at a fixed rate, you need to calibrate your mental "
            "model of how much water N seconds produces for this specific setup. "
            "Start conservative (3-10s) and increase based on observed soil moisture response. "
            "NEVER run if soil moisture is above 65%. Always log the reason."
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


def _build_tool_declarations() -> list[dict]:
    return [
        {
            "function_declarations": [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                }
                for t in GROWER_TOOLS
            ]
        }
    ]


class GeminiClient:
    def __init__(self, config: dict):
        self.config = config
        gemini_config = config.get("gemini", {})

        api_key = os.environ.get("GEMINI_API_KEY", "")
        self.client = genai.Client(api_key=api_key) if api_key else genai.Client()

        self.model = gemini_config.get("model", "gemini-3-flash-preview")
        self.max_tokens = gemini_config.get("max_output_tokens", 4096)
        self.thinking = gemini_config.get("thinking", True)

        self._last_interaction_id: Optional[str] = None
        self._chain_length = 0
        self._max_chain_length = 20

    def create_interaction(
        self,
        system_instruction: str,
        user_message: str,
        image_path: Optional[str] = None,
        use_tools: bool = True,
        continue_chain: bool = True,
    ):
        input_parts = []

        if image_path and Path(image_path).exists():
            with open(image_path, "rb") as f:
                image_data = base64.b64encode(f.read()).decode("utf-8")
            ext = Path(image_path).suffix.lower()
            mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                        ".png": "image/png", ".webp": "image/webp"}
            mime_type = mime_map.get(ext, "image/jpeg")
            input_parts.append({"inline_data": {"mime_type": mime_type, "data": image_data}})

        input_parts.append(user_message)

        previous_id = None
        if continue_chain and self._last_interaction_id:
            if self._chain_length < self._max_chain_length:
                previous_id = self._last_interaction_id

        kwargs = {
            "model": self.model,
            "input": input_parts if len(input_parts) > 1 else user_message,
            "system_instruction": system_instruction,
            "generation_config": {"max_output_tokens": self.max_tokens},
        }

        if use_tools:
            kwargs["tools"] = _build_tool_declarations()

        if previous_id:
            kwargs["previous_interaction_id"] = previous_id

        if self.thinking:
            kwargs["generation_config"]["thinking"] = {
                "enabled": True,
                "include_thoughts": True,
            }

        logger.info("Gemini interaction (chain=%d, model=%s)", self._chain_length, self.model)
        interaction = self.client.interactions.create(**kwargs)

        self._last_interaction_id = interaction.id
        self._chain_length += 1
        return interaction

    def submit_tool_results(
        self,
        interaction_id: str,
        tool_results: list[dict],
        system_instruction: str,
    ):
        input_parts = []
        for result in tool_results:
            input_parts.append({
                "function_response": {
                    "id": result["call_id"],
                    "name": result["name"],
                    "response": (
                        result["result"]
                        if isinstance(result["result"], dict)
                        else {"result": result["result"]}
                    ),
                }
            })

        interaction = self.client.interactions.create(
            model=self.model,
            input=input_parts,
            previous_interaction_id=interaction_id,
            system_instruction=system_instruction,
            tools=_build_tool_declarations(),
            generation_config={"max_output_tokens": self.max_tokens},
        )

        self._last_interaction_id = interaction.id
        self._chain_length += 1
        return interaction

    def reset_chain(self):
        self._last_interaction_id = None
        self._chain_length = 0
        logger.info("Interaction chain reset")

    def extract_response(self, interaction) -> dict:
        result = {
            "text": None,
            "function_calls": [],
            "thoughts": [],
            "status": interaction.status,
            "interaction_id": interaction.id,
        }

        for output in interaction.outputs:
            if output.type == "text":
                result["text"] = output.text
            elif output.type == "thought":
                if hasattr(output, "summary") and output.summary:
                    result["thoughts"].append(output.summary)
            elif output.type == "function_call":
                result["function_calls"].append({
                    "id": output.id,
                    "name": output.name,
                    "arguments": output.arguments,
                })

        return result
