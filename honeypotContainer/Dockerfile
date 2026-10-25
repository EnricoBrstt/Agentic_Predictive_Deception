FROM python:3.10-slim-bookworm

# 1. SSH daemon plus the basic tools an attacker expects to find
RUN apt-get update && apt-get install -y \
    openssh-server \
    sudo \
    curl \
    wget \
    nano \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir /var/run/sshd

# 2. requests, used by fakeshell to notify the agentic system
RUN pip install --no-cache-dir requests

# 3. Fake shell
COPY fakeshell.py /usr/local/bin/fakeshell.py
RUN chmod +x /usr/local/bin/fakeshell.py

# 4. 'honeypot' user with fakeshell as login shell
RUN useradd -m -s /usr/local/bin/fakeshell.py honeypot \
    && echo 'honeypot:password123' | chpasswd

# 5. Register fakeshell as a valid login shell
RUN echo "/usr/local/bin/fakeshell.py" >> /etc/shells

# 6. SSH on port 2222 with password login
RUN sed -i 's/#Port 22/Port 2222/' /etc/ssh/sshd_config \
    && sed -i 's/#PasswordAuthentication yes/PasswordAuthentication yes/' /etc/ssh/sshd_config \
    # No standard MOTD: fakeshell prints its own
    && sed -i 's/#PrintMotd yes/PrintMotd no/' /etc/ssh/sshd_config

EXPOSE 2222

CMD ["/usr/sbin/sshd", "-D"]