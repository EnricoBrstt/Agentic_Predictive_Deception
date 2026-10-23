import json

class UnifiedFunctionCall:
    """Oggetto standardizzato per le chiamate ai tool, indipendente dal provider."""
    def __init__(self, name, args, call_id=None):
        self.name = name
        self.args = args
        # call_id è usato solo da OpenAI per collegare tool_result al tool_call.
        # Per Google è sempre None: Part.from_function_response non richiede un id.
        self.id = call_id

class UnifiedResponse:
    """Risposta standardizzata restituita al PredictiveAgent, indipendente dal provider."""
    def __init__(self, text="", function_calls=None):
        self.text = text
        self.function_calls = function_calls or []


# --- WRAPPERS PER LE SESSIONI DI CHAT ---

class GoogleChatWrapper:
    def __init__(self, google_chat):
        self.chat = google_chat

    async def send_message(self, message):
        response = await self.chat.send_message(message)

        text = response.text or ""
        function_calls = []
        if response.function_calls:
            for fc in response.function_calls:
                # Google non espone un call_id: passiamo None esplicitamente (vedi UnifiedFunctionCall)
                function_calls.append(UnifiedFunctionCall(name=fc.name, args=fc.args, call_id=None))

        return UnifiedResponse(text, function_calls)

class OpenAIChatWrapper:
    def __init__(self, client, model, system_instruction, tools):
        self.client = client
        self.model = model
        self.tools = tools
        self.history = [{"role": "system", "content": system_instruction}]

    async def send_message(self, message):
        # 1. Aggiunge il messaggio utente o i risultati dei tool alla cronologia
        if isinstance(message, str):
            self.history.append({"role": "user", "content": message})
        elif isinstance(message, list):
            if not message:
                raise ValueError("Lista tool_responses vuota passata a send_message: nessun tool result da inviare.")
            # I tool results sono già formattati da AgentConnector.format_tool_response
            for tool_result in message:
                self.history.append(tool_result)

        # 2. Prepara la chiamata
        # max_tokens limita il credito che OpenRouter riserva per ogni richiesta in corso (evita errori 402)
        kwargs = {"model": self.model, "messages": self.history, "temperature": 0.0, "max_tokens": 2048}
        if self.tools:
            kwargs["tools"] = self.tools

        # 3. Esegue la chiamata API
        response = await self.client.chat.completions.create(**kwargs)
        msg = response.choices[0].message

        # 4. Aggiunge la risposta dell'assistente alla history (obbligatorio per OpenAI:
        #    il messaggio con tool_calls deve precedere i tool results nel turno successivo)
        self.history.append(msg)

        # 5. Estrae testo e tool call in formato unificato
        text = msg.content or ""
        function_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                # OpenAI restituisce gli argomenti come stringa JSON: deserializziamo
                args = json.loads(tc.function.arguments)
                function_calls.append(UnifiedFunctionCall(name=tc.function.name, args=args, call_id=tc.id))

        return UnifiedResponse(text, function_calls)