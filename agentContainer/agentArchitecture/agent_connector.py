"""
AgentConnector: provider-independent access to the LLM (Google Gemini, OpenAI, OpenRouter, local LM Studio).

create_agentic_chat() returns a multi-turn chat session configured with a system prompt and tools. The
session is wrapped (adapter_connector.py) behind a common send_message() -> UnifiedResponse interface, so
the agents' tool-calling loops never deal with SDK differences such as OpenAI's tool call_id.

Configuration:
- provider 'local': LM Studio through its OpenAI-compatible API.
- provider 'cloud': key and SDK are read from the Docker secret (.env format):
      LLM_API_KEY=<key>
      LLM_SDK=<google|openai|openrouter>
"""

import os
import json
from google.genai.types import HarmCategory, HarmBlockThreshold, GenerateContentConfig, Part
from google.genai import Client as GoogleClient
from openai import AsyncOpenAI
from adapter_connector import GoogleChatWrapper, OpenAIChatWrapper

# Docker secret holding LLM_API_KEY and LLM_SDK
SECRET_FILE = "/run/secrets/llm_config_secret"

VALID_SDKS = {"google", "openai", "openrouter"}

# Gemini safety filters disabled
SAFETY_CONFIG = [
    {"category": HarmCategory.HARM_CATEGORY_HATE_SPEECH, "threshold": HarmBlockThreshold.BLOCK_NONE},
    {"category": HarmCategory.HARM_CATEGORY_HARASSMENT, "threshold": HarmBlockThreshold.BLOCK_NONE},
    {"category": HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT, "threshold": HarmBlockThreshold.BLOCK_NONE},
    {"category": HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT, "threshold": HarmBlockThreshold.BLOCK_NONE},
]

class AgentConnector:

    def __init__(self, agent_name, model_name, provider):
        self.provider = provider
        self.agent_name = agent_name

        if self.provider == "local":
            # LM Studio (OpenAI-compatible API); host.docker.internal reaches the host from the container
            self.sdk = "openai"
            self.client = AsyncOpenAI(base_url="http://host.docker.internal:1234/v1", api_key="lm-studio")
            self.model = model_name
            print(f"[{self.agent_name}][CONNECTOR] Local LM Studio, model {self.model}")

        else: # cloud: key and SDK from the Docker secret
            api_key, self.sdk = self._load_key_logic()

            if self.sdk == "google":
                self.client = GoogleClient(api_key=api_key)
                self.model = model_name
                print(f"[{self.agent_name}][CONNECTOR] Google SDK, model {self.model}")

            elif self.sdk == "openai":
                self.client = AsyncOpenAI(api_key=api_key, base_url="https://api.openai.com/v1")
                self.model = model_name
                print(f"[{self.agent_name}][CONNECTOR] OpenAI SDK, model {self.model}")

            elif self.sdk == "openrouter":
                self.client = AsyncOpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")
                self.model = model_name
                print(f"[{self.agent_name}][CONNECTOR] OpenRouter, model {self.model}")

    def _load_key_logic(self) -> tuple[str, str]:
        """Read LLM_API_KEY and LLM_SDK from the Docker secret (.env format)."""

        if os.path.exists(SECRET_FILE):
            config = {}
            with open(SECRET_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    # Skip blank lines and comments
                    if not line or line.startswith("#"):
                        continue

                    if "=" in line:
                        key, value = line.split("=", 1)
                        config[key.strip()] = value.strip()

            api_key = config.get("LLM_API_KEY")
            sdk = config.get("LLM_SDK", "").lower()

            if not api_key or not sdk:
                raise ValueError(
                    f"[{self.agent_name}][CONNECTOR] Invalid '{SECRET_FILE}': it must define "
                    "LLM_API_KEY=<key> and LLM_SDK=<google|openai|openrouter>."
                )

            if sdk not in VALID_SDKS:
                raise ValueError(
                    f"[{self.agent_name}][CONNECTOR] Unknown SDK '{sdk}', accepted values: {VALID_SDKS}"
                )

            print(f"[{self.agent_name}][CONNECTOR] Config loaded from {SECRET_FILE} (sdk={sdk})")
            return api_key, sdk
        else:
            raise ValueError(
                    f"[{self.agent_name}][CONNECTOR] '{SECRET_FILE}' not found: check the llm_config_secret Docker secret."
            )

    def create_agentic_chat(self, system_instruction: str, google_tools: list, openai_tools: list):
        """Return a chat session wrapped in the unified interface for the current SDK."""
        if self.sdk == "google":
            config = GenerateContentConfig(
                system_instruction=system_instruction,
                tools=google_tools,
                temperature=0.0,
                safety_settings=SAFETY_CONFIG
            )
            chat = self.client.aio.chats.create(model=self.model, config=config)
            return GoogleChatWrapper(chat)
        else: # openai / openrouter / local
            return OpenAIChatWrapper(self.client, self.model, system_instruction, openai_tools)

    def format_tool_response(self, name: str, result: str, call_id: str = None):
        """
        Format a tool result for the current SDK.

        Google: Part.from_function_response (no call_id).
        OpenAI/OpenRouter/local: role='tool' message with the mandatory tool_call_id.
        """
        if self.sdk == "google":
            return Part.from_function_response(name=name, response={"result": result})
        else:
            return {
                "role": "tool",
                "tool_call_id": call_id,
                "name": name,
                "content": json.dumps({"result": result})
            }
