#!/usr/bin/env python3
import os
import json
import sys
import tty
import termios
import pty
import shlex
import time
import select
import socket
import getpass
import readline
import glob
import requests
from requests.exceptions import ReadTimeout

# L'URL del futuro Orchestratore AI (Agent_Team)
# Quando testeremo tutto con docker-compose, 'ai-orchestrator' sarà il nome del servizio.
# Per ora, usiamo una variabile d'ambiente per poterlo testare localmente.
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://agentic-system:8000/new_command")

# Informazioni utente simulate
user = getpass.getuser()
hostname = socket.gethostname()

# Recupera l'IP reale dell'attaccante dalle variabili SSH
ssh_client = os.environ.get('SSH_CLIENT', '')
if ssh_client:
    attacker_ip = ssh_client.split()[0]
else:
    attacker_ip = "127.0.0.1"

# Logging Locale (Opzionale/Backup)
LOG_FILE = "/tmp/fakeshell_local.json"

session_date = time.strftime("%Y-%m-%d")
SESSION_ID = f"{session_date}_{attacker_ip.replace('.','-')}"

def trigger_ai(cmd, cwd):
    """
    Invia l'evento all'Orchestratore AI e attende una risposta.
    Se l'Orchestratore è lento o irraggiungibile, fallisce silenziosamente
    permettendo alla shell di continuare.
    """
    entry = {
        "session_id": SESSION_ID,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ip": attacker_ip,
        "user": user,
        "cwd": cwd,
        "cmd": cmd
    }
    
    # Log di backup locale
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass

    # Chiamata bloccante all'Orchestratore (Max 2.5 secondi)
    try:
        requests.post(ORCHESTRATOR_URL, json=entry, timeout=0.1)
    except Exception:
        # Silenzioso: l'attaccante non deve accorgersi dell'orchestratore (anche se spento)
        pass

# Ambiente realistico
env = os.environ.copy()
env.update({
    "USER": user,
    "HOME": os.path.expanduser("~"),
    "SHELL": "/bin/bash",
    "TERM": "xterm-256color",
})

home_dir = env["HOME"]
cwd = home_dir

aliases = {
    "ll": "ls -alF",
    "la": "ls -A",
    "l": "ls -CF"
}

readline.parse_and_bind("tab: complete")

def list_binaries_in_path():
    bins = []
    for p in os.environ.get("PATH", "").split(":"):
        if os.path.isdir(p):
            for f in os.listdir(p):
                full = os.path.join(p, f)
                if os.access(full, os.X_OK) and os.path.isfile(full):
                    bins.append(f)
    return set(bins)

BINARIES = list_binaries_in_path()

def smart_path_completion(text):
    expanded = os.path.expanduser(text)
    matches = glob.glob(expanded + "*")
    results = []
    for m in matches:
        display = m
        if os.path.isdir(m):
            display += "/"
        results.append(display)
    return results

def completer(text, state):
    buffer = readline.get_line_buffer()
    tokens = buffer.split()
    if len(tokens) == 1 and not buffer.endswith(" "):
        candidates = [b for b in BINARIES if b.startswith(text)]
        for f in os.listdir("."):
            if f.startswith(text) and os.access(f, os.X_OK):
                candidates.append(f)
        candidates = sorted(set(candidates))
        try:
            return candidates[state]
        except IndexError:
            return None
    candidates = smart_path_completion(text)
    try:
        return candidates[state]
    except IndexError:
        return None

readline.set_completer(completer)
readline.set_completer_delims(" \t\n;")

# Stampa finto MOTD per maggiore credibilità
print("Welcome to Ubuntu 22.04.3 LTS (GNU/Linux 5.15.0-82-generic x86_64)\n")
print(" * Documentation:  https://help.ubuntu.com")
print(" * Management:     https://landscape.canonical.com")
print(" * Support:        https://ubuntu.com/advantage\n")
print(f"Last login: {time.strftime('%a %b %d %H:%M:%S %Y')} from {attacker_ip}\n")


while True:
    try:
        symbol = "#" if user == "root" else "$"
        prompt = f"\033[1;32m{user}@{hostname}\033[0m:\033[1;34m{cwd}\033[0m{symbol} "
        cmd = input(prompt)
    except EOFError:
        break

    if not cmd.strip():
        continue

    for a, c in aliases.items():
        if cmd.strip().startswith(a + " "):
            cmd = cmd.replace(a, c, 1)
        if cmd.strip() == a:
            cmd = c

    if cmd in ["exit", "quit", "logout"]:
        print("logout")
        break

    if cmd.startswith("cd"):
        try:
            parts = shlex.split(cmd)
            if len(parts) < 2 or parts[1] == "~":
                target = home_dir
            elif parts[1] == "-":
                target = os.environ.get("OLDPWD", cwd)
            else:
                target = parts[1]

            new_dir = os.path.abspath(os.path.join(cwd, os.path.expanduser(target)))

            if os.path.isdir(new_dir):
                os.environ["OLDPWD"] = cwd
                cwd = new_dir
            else:
                print(f"cd: {target}: No such file or directory")
        except Exception as e:
            print("cd error:", e)
        continue

    # --- CHIAMATA ALL'INTELLIGENZA ARTIFICIALE ---
    trigger_ai(cmd, cwd)

    # --- ESECUZIONE REALE ---
    try:
        pid, fd = pty.fork()
        if pid == 0:
            os.chdir(cwd)
            os.execve("/bin/bash", ["bash", "-c", cmd], env)
        else:
            old_settings = termios.tcgetattr(sys.stdin)
            try:
                tty.setraw(sys.stdin.fileno())
                while True:
                    r, _, _ = select.select([fd, sys.stdin], [], [])
                    for s in r:
                        if s == fd:
                            try:
                                output = os.read(fd, 1024)
                                if not output:
                                    raise OSError
                                os.write(sys.stdout.fileno(), output)
                            except OSError:
                                raise StopIteration
                        elif s == sys.stdin:
                            user_input = os.read(sys.stdin.fileno(), 1024)
                            os.write(fd, user_input)
            except StopIteration:
                pass
            finally:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
    except Exception as e:
        print(f"bash: unexpected error: {str(e)}")