"""A fake SMTP relay on loopback, for the completion-notice tests (#392).

Enough of RFC 5321 for ``smtplib`` to talk to it: EHLO, optional AUTH LOGIN
and AUTH PLAIN, MAIL FROM, RCPT TO, DATA and QUIT. It keeps every message it
accepted, so a test can read the headers that really went out rather than
the ones the harness meant to send.

It speaks no TLS, which is why it is reachable only as ``127.0.0.1``: the
configuration refuses ``tls: none`` for any other host. Two failures a test
needs are settings rather than certificates: ``starttls=False`` leaves
STARTTLS out of the EHLO reply, which is what a relay that cannot be
upgraded looks like to ``smtplib``, and ``password`` set with a different
password answers AUTH with 535.
"""

import base64
import socket
import threading


class FakeSmtpServer:
    """One loopback SMTP listener, in a thread, as a context manager."""

    def __init__(
        self,
        *,
        starttls: bool = False,
        username: str = "",
        password: str = "",
        fail_first: int = 0,
        reject_recipients: bool = False,
        greeting: str = "220 fake-relay ESMTP\r\n",
    ):
        self.starttls = starttls
        self.username = username
        self.password = password
        # Refuse this many deliveries with a temporary failure first, so a
        # test can watch the retries.
        self.fail_first = fail_first
        self.reject_recipients = reject_recipients
        self.greeting = greeting
        self.messages: list[dict] = []
        self.auth_attempts: list[str] = []
        self.deliveries = 0
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Loopback only: this fake has no authentication and no TLS.
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(8)
        self.host, self.port = self._socket.getsockname()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> "FakeSmtpServer":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._stop.set()
        try:
            self._socket.close()
        except OSError:
            pass
        self._thread.join(timeout=5)

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except OSError:
                return
            try:
                self._session(connection)
            except OSError:
                pass
            finally:
                try:
                    connection.close()
                except OSError:
                    pass

    def _session(self, connection: socket.socket) -> None:
        connection.settimeout(10)
        stream = connection.makefile("rwb")
        stream.write(self.greeting.encode())
        stream.flush()
        sender, recipients, authenticated = "", [], not self.username
        while True:
            line = stream.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            word = text.split(" ", 1)[0].upper()
            rest = text[len(word):].strip()
            if word in ("EHLO", "HELO"):
                replies = ["250-fake-relay"]
                if self.username:
                    replies.append("250-AUTH LOGIN PLAIN")
                if self.starttls:
                    replies.append("250-STARTTLS")
                replies.append("250 HELP")
                stream.write(("\r\n".join(replies) + "\r\n").encode())
            elif word == "STARTTLS":
                # Offered only to prove the harness asks for it; this fake
                # has no certificate, so the upgrade itself is refused.
                stream.write(b"454 TLS not available on this fake\r\n")
            elif word == "AUTH":
                authenticated = self._auth(stream, rest)
                stream.write(
                    b"235 authenticated\r\n" if authenticated
                    else b"535 authentication failed\r\n"
                )
            elif word == "MAIL":
                if not authenticated:
                    stream.write(b"530 authentication required\r\n")
                else:
                    sender, recipients = _address(rest), []
                    stream.write(b"250 sender ok\r\n")
            elif word == "RCPT":
                if self.reject_recipients:
                    stream.write(b"550 no such recipient\r\n")
                else:
                    recipients.append(_address(rest))
                    stream.write(b"250 recipient ok\r\n")
            elif word == "DATA":
                self.deliveries += 1
                if self.deliveries <= self.fail_first:
                    stream.write(b"451 try again later\r\n")
                    stream.flush()
                    continue
                stream.write(b"354 send it\r\n")
                stream.flush()
                body = self._read_data(stream)
                self.messages.append({
                    "sender": sender, "recipients": list(recipients), "data": body,
                })
                stream.write(b"250 queued\r\n")
            elif word == "RSET":
                sender, recipients = "", []
                stream.write(b"250 reset\r\n")
            elif word == "NOOP":
                stream.write(b"250 ok\r\n")
            elif word == "QUIT":
                stream.write(b"221 bye\r\n")
                stream.flush()
                return
            else:
                stream.write(b"502 not implemented\r\n")
            stream.flush()

    def _auth(self, stream, rest: str) -> bool:
        mechanism = rest.split(" ", 1)[0].upper()
        initial = rest[len(mechanism):].strip()
        if mechanism == "PLAIN":
            if not initial:
                stream.write(b"334 \r\n")
                stream.flush()
                initial = stream.readline().decode().strip()
            try:
                _, user, password = base64.b64decode(initial).decode().split("\0")
            except (ValueError, UnicodeDecodeError):
                return False
        elif mechanism == "LOGIN":
            if initial:
                # smtplib sends the user name as the initial response.
                user = _decode(initial.encode())
            else:
                stream.write(b"334 VXNlcm5hbWU6\r\n")
                stream.flush()
                user = _decode(stream.readline())
            stream.write(b"334 UGFzc3dvcmQ6\r\n")
            stream.flush()
            password = _decode(stream.readline())
        else:
            return False
        self.auth_attempts.append(user)
        return user == self.username and password == self.password

    def _read_data(self, stream) -> bytes:
        lines = []
        while True:
            line = stream.readline()
            if not line or line.strip() == b".":
                break
            lines.append(line[1:] if line.startswith(b"..") else line)
        return b"".join(lines)


def _address(text: str) -> str:
    """The address out of ``FROM:<a@b>`` or ``TO:<a@b>``."""
    if "<" in text and ">" in text:
        return text[text.index("<") + 1:text.index(">")].strip()
    return text.split(":", 1)[-1].strip()


def _decode(line: bytes) -> str:
    try:
        return base64.b64decode(line.decode().strip()).decode()
    except (ValueError, UnicodeDecodeError):
        return ""
