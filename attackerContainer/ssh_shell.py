"""
SSHShellSession — Guida interattiva della fakeshell dell'honeypot via un canale SSH persistente.

fakeshell.py (honeypotContainer/fakeshell.py) mantiene 'cwd' come stato Python in-process per
l'intera sessione di login: per questo serve un UNICO canale SSH persistente (paramiko
invoke_shell()) e non un exec per comando.

Per sapere quando l'output di un comando e' terminato, non si usa un sentinel-echo
(es. "; echo __DONE__") perche' si romperebbe silenziosamente su 'cd': fakeshell intercetta
cmd.startswith("cd") PRIMA di spawnare bash, quindi il marker non verrebbe mai eseguito/echoed.

Si rileva invece la comparsa del PROSSIMO PROMPT via regex sul formato ANSI esatto generato da
fakeshell:
    f"\\033[1;32m{user}@{hostname}\\033[0m:\\033[1;34m{cwd}\\033[0m{symbol} "
Questo funziona in modo uniforme per comandi normali, 'cd' ed errori, senza casi speciali.
"""

import re
import socket
import time

import paramiko

PROMPT_RE = re.compile(
    rb"\x1b\[1;32m(?P<userhost>[^\x1b]+)\x1b\[0m:"
    rb"\x1b\[1;34m(?P<cwd>[^\x1b]+)\x1b\[0m(?P<symbol>[#$]) $"
)

# Comandi che aprono pager/editor interattivi: la fakeshell li esegue realmente via bash,
# quindi possono bloccare il canale in attesa di input umano che non arrivera' mai.
INTERACTIVE_HINTS = ("top", "less", "more", "vim", "vi ", "nano", "man ", "watch")


class ShellTurnResult:
    def __init__(self, output: str, cwd: str | None, eof: bool, timed_out: bool):
        self.output = output
        self.cwd = cwd
        self.eof = eof
        self.timed_out = timed_out


class SSHShellSession:
    """Wrapper sincrono attorno a un canale interattivo paramiko. Le chiamate bloccanti vanno
    eseguite dal chiamante dentro asyncio.to_thread() per non bloccare l'event loop."""

    def __init__(self, host, port, username, password,
                 connect_retries=5, connect_retry_delay=2.0,
                 read_poll_timeout=1.0, hard_timeout=20.0, hang_recovery_timeout=5.0):
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = password
        self.connect_retries = connect_retries
        self.connect_retry_delay = connect_retry_delay
        self.read_poll_timeout = read_poll_timeout
        self.hard_timeout = hard_timeout
        self.hang_recovery_timeout = hang_recovery_timeout

        self._client: paramiko.SSHClient | None = None
        self._channel: paramiko.Channel | None = None

    # --- Ciclo di vita ---

    def connect(self):
        """Connette con retry (docker-compose depends_on garantisce solo l'avvio del
        container, non che sshd sia gia' in ascolto sulla 2222)."""
        last_err = None
        for attempt in range(1, self.connect_retries + 1):
            try:
                client = paramiko.SSHClient()
                # Host key rigenerata ad ogni build dell'immagine honeypot: nessun TOFU store
                # persistente ha senso qui, l'honeypot e' il nostro stesso target di test.
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                client.connect(
                    hostname=self.host,
                    port=self.port,
                    username=self.username,
                    password=self.password,
                    timeout=10,
                    allow_agent=False,
                    look_for_keys=False,
                )
                channel = client.invoke_shell()
                channel.settimeout(self.read_poll_timeout)
                self._client = client
                self._channel = channel
                return
            except Exception as e:
                last_err = e
                print(f"[SSH_SHELL] Tentativo {attempt}/{self.connect_retries} fallito: {e}")
                if attempt < self.connect_retries:
                    time.sleep(self.connect_retry_delay)
        raise ConnectionError(f"Impossibile connettersi a {self.host}:{self.port} dopo {self.connect_retries} tentativi: {last_err}")

    def close(self):
        if self._channel:
            try:
                self._channel.close()
            except Exception:
                pass
            self._channel = None
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    def drain_banner(self) -> ShellTurnResult:
        """Consuma MOTD + banner + primo prompt, prima che venga inviato alcun comando."""
        return self._read_until_prompt()

    # --- Esecuzione comandi ---

    def run_command(self, command: str) -> ShellTurnResult:
        if self._channel is None:
            raise RuntimeError("[SSH_SHELL] Canale non connesso. Chiamare connect() prima.")

        self._channel.send((command + "\n").encode("utf-8"))
        result = self._read_until_prompt()

        # Best-effort: la prima riga catturata puo' essere l'eco del comando appena inviato
        # (dipende dalle impostazioni termios della pty lato fakeshell durante l'input()).
        lines = result.output.split("\n", 1)
        if lines and lines[0].strip() == command.strip():
            result.output = lines[1] if len(lines) > 1 else ""

        return result

    # --- Lettura fino al prompt ---

    def _read_until_prompt(self) -> ShellTurnResult:
        buf = bytearray()
        deadline = time.monotonic() + self.hard_timeout
        sent_ctrl_c = False

        while True:
            try:
                chunk = self._channel.recv(4096)
            except socket.timeout:
                chunk = None

            if chunk is not None:
                if chunk == b"":
                    # EOF: il target ha chiuso la connessione (exit/quit/logout).
                    return ShellTurnResult(
                        output=self._decode(bytes(buf)), cwd=None, eof=True, timed_out=False
                    )
                buf.extend(chunk)
                match = PROMPT_RE.search(bytes(buf))
                if match and match.end() == len(buf):
                    output = self._decode(bytes(buf[: match.start()]))
                    cwd = match.group("cwd").decode("utf-8", errors="replace")
                    return ShellTurnResult(output=output, cwd=cwd, eof=False, timed_out=False)

            if time.monotonic() >= deadline:
                if not sent_ctrl_c:
                    # Probabile comando interattivo (pager/editor): tentiamo il recovery.
                    print("[SSH_SHELL] Timeout senza prompt: invio Ctrl-C per tentare il recovery.")
                    try:
                        self._channel.send(b"\x03")
                    except Exception:
                        pass
                    sent_ctrl_c = True
                    deadline = time.monotonic() + self.hang_recovery_timeout
                    continue
                return ShellTurnResult(
                    output=self._decode(bytes(buf))
                    + "\n<TIMEOUT: il comando non ha prodotto un nuovo prompt entro il timeout, inviato Ctrl-C>",
                    cwd=None,
                    eof=False,
                    timed_out=True,
                )

    @staticmethod
    def _decode(data: bytes) -> str:
        return data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
