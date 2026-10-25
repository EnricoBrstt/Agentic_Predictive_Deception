"""
SSHShellSession: drives the honeypot fakeshell over one persistent SSH channel.

fakeshell.py keeps 'cwd' as in-process state for the whole login, so a single invoke_shell() channel
is used instead of one exec per command. The end of a command's output is detected by matching the
next fakeshell prompt (exact ANSI format below) rather than a sentinel echo, which would never run
after 'cd' because fakeshell handles cd itself without spawning bash:
    f"\\033[1;32m{user}@{hostname}\\033[0m:\\033[1;34m{cwd}\\033[0m{symbol} "
"""

import re
import socket
import time

import paramiko

PROMPT_RE = re.compile(
    rb"\x1b\[1;32m(?P<userhost>[^\x1b]+)\x1b\[0m:"
    rb"\x1b\[1;34m(?P<cwd>[^\x1b]+)\x1b\[0m(?P<symbol>[#$]) $"
)

# Interactive pagers/editors: fakeshell really runs them, so they can block the channel waiting for input
INTERACTIVE_HINTS = ("top", "less", "more", "vim", "vi ", "nano", "man ", "watch")


class ShellTurnResult:
    def __init__(self, output: str, cwd: str | None, eof: bool, timed_out: bool):
        self.output = output
        self.cwd = cwd
        self.eof = eof
        self.timed_out = timed_out


class SSHShellSession:
    """Synchronous wrapper around an interactive paramiko channel; callers run it in asyncio.to_thread()."""

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

    # --- Lifecycle ---

    def connect(self):
        """Connect with retries: depends_on only waits for the container to start, not for sshd."""
        last_err = None
        for attempt in range(1, self.connect_retries + 1):
            try:
                client = paramiko.SSHClient()
                # The honeypot host key changes on every build and it is our own test target: accept it
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
                print(f"[SSH_SHELL] Attempt {attempt}/{self.connect_retries} failed: {e}")
                if attempt < self.connect_retries:
                    time.sleep(self.connect_retry_delay)
        raise ConnectionError(f"Cannot connect to {self.host}:{self.port} after {self.connect_retries} attempts: {last_err}")

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
        """Consume MOTD, banner and first prompt before any command is sent."""
        return self._read_until_prompt()

    # --- Commands ---

    def run_command(self, command: str) -> ShellTurnResult:
        if self._channel is None:
            raise RuntimeError("[SSH_SHELL] Channel not connected. Call connect() first.")

        self._channel.send((command + "\n").encode("utf-8"))
        result = self._read_until_prompt()

        # Best effort: drop the echo of the command if the fakeshell pty sent it back
        lines = result.output.split("\n", 1)
        if lines and lines[0].strip() == command.strip():
            result.output = lines[1] if len(lines) > 1 else ""

        return result

    # --- Read until the next prompt ---

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
                    # EOF: the target closed the connection (exit/quit/logout)
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
                    # Probably an interactive program: try Ctrl-C once
                    print("[SSH_SHELL] No prompt before timeout, sending Ctrl-C.")
                    try:
                        self._channel.send(b"\x03")
                    except Exception:
                        pass
                    sent_ctrl_c = True
                    deadline = time.monotonic() + self.hang_recovery_timeout
                    continue
                return ShellTurnResult(
                    output=self._decode(bytes(buf))
                    + "\n<TIMEOUT: the command produced no new prompt within the timeout, sent Ctrl-C>",
                    cwd=None,
                    eof=False,
                    timed_out=True,
                )

    @staticmethod
    def _decode(data: bytes) -> str:
        return data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
