import json

class UnifiedFunctionCall:
    """Provider-independent tool call."""
    def __init__(self, name, args, call_id=None):
        self.name = name
        self.args = args
        # OpenAI links each tool result to its call through call_id; always None for Google
        self.id = call_id

class UnifiedResponse:
    """Provider-independent model reply: text plus tool calls."""
    def __init__(self, text="", function_calls=None):
        self.text = text
        self.function_calls = function_calls or []


# --- Chat session wrappers ---

class GoogleChatWrapper:
    def __init__(self, google_chat):
        self.chat = google_chat

    async def send_message(self, message):
        response = await self.chat.send_message(message)

        text = response.text or ""
        function_calls = []
        if response.function_calls:
            for fc in response.function_calls:
                # Google has no call_id
                function_calls.append(UnifiedFunctionCall(name=fc.name, args=fc.args, call_id=None))

        return UnifiedResponse(text, function_calls)

class OpenAIChatWrapper:
    def __init__(self, client, model, system_instruction, tools):
        self.client = client
        self.model = model
        self.tools = tools
        self.history = [{"role": "system", "content": system_instruction}]

    async def send_message(self, message):
        # 1. Append the user message or the tool results to the history
        if isinstance(message, str):
            self.history.append({"role": "user", "content": message})
        elif isinstance(message, list):
            if not message:
                raise ValueError("Empty tool_responses list passed to send_message.")
            # Already formatted by AgentConnector.format_tool_response
            for tool_result in message:
                self.history.append(tool_result)

        # 2. max_tokens caps the credit OpenRouter reserves for each in-flight request (avoids 402 errors)
        kwargs = {"model": self.model, "messages": self.history, "temperature": 0.0, "max_tokens": 2048}
        if self.tools:
            kwargs["tools"] = self.tools

        response = await self.client.chat.completions.create(**kwargs)
        msg = response.choices[0].message

        # 3. Keep the assistant reply in the history: OpenAI requires the tool_calls message
        #    right before the matching tool results
        self.history.append(msg)

        # 4. Convert to the unified format (tool arguments arrive as a JSON string)
        text = msg.content or ""
        function_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments)
                function_calls.append(UnifiedFunctionCall(name=tc.function.name, args=args, call_id=tc.id))

        return UnifiedResponse(text, function_calls)
