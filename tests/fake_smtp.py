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

The session reads and writes through two file objects over the one socket,
as ``socketserver.StreamRequestHandler`` does. One ``makefile("rwb")`` is a
``BufferedRWPair`` over a single raw stream, which the standard library
warns against; on a loaded CI runner it closed sessions mid-greeting.
Whatever a session thread does raise is kept in ``errors`` rather than
disappearing with the thread, so a test sees the cause instead of only
``SMTPServerDisconnected``.
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
        # What each client called itself in EHLO, so a test can check the
        # harness names itself rather than asking DNS what this host is.
        self.client_names: list[str] = []
        self.errors: list[str] = []
        self.deliveries = 0
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Loopback only: this fake has no authentication and no TLS.
        self._socket.bind(("127.0.0.1", 0))
        self._socket.listen(8)
        self.host, self.port = self._socket.getsockname()
        self._stop = threading.Event()
        self._sessions: list[threading.Thread] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> "FakeSmtpServer":
        self._thread.start()
        self._probe()
        return self

    def _probe(self) -> None:
        """Make sure the relay is really serving before a test relies on it.

        A listening socket completes a connection in the kernel's backlog
        whether or not anything ever calls ``accept``, so a session thread
        that died leaves a client waiting for a greeting until its own
        timeout, with nothing to say why. One greeting read here turns that
        into an error naming the cause.
        """
        try:
            with socket.create_connection((self.host, self.port), timeout=20) as probe:
                probe.settimeout(20)
                reader = probe.makefile("rb")
                greeting = reader.readline()
                probe.sendall(b"QUIT\r\n")
                reader.readline()
                reader.close()
        except OSError as error:
            raise RuntimeError(
                f"the fake relay on {self.host}:{self.port} did not answer "
                f"({type(error).__name__}: {error}); thread alive: "
                f"{self._thread.is_alive()}; errors: {self.errors}"
            ) from None
        if not greeting.startswith(b"220"):
            raise RuntimeError(
                f"the fake relay greeted with {greeting!r}; errors: {self.errors}"
            )

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._stop.set()
        try:
            self._socket.close()
        except OSError:
            pass
        self._thread.join(timeout=10)
        for session in list(self._sessions):
            session.join(timeout=10)

    def _serve(self) -> None:
        # A timeout on the accept rather than a blocking one, as the fake SSH
        # in tests/test_deploy_scripts.py does: the loop then notices close()
        # by itself, and a transient error cannot end the service silently.
        self._socket.settimeout(0.2)
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError as error:
                if not self._stop.is_set():
                    self.errors.append(f"accept: {type(error).__name__}: {error}")
                return
            # One thread per connection: a session that is slow to finish
            # must not hold up the next delivery (a retry opens another
            # connection at once).
            session = threading.Thread(
                target=self._serve_one, args=(connection,), daemon=True
            )
            self._sessions.append(session)
            session.start()

    def _serve_one(self, connection: socket.socket) -> None:
        try:
            self._session(connection)
        except Exception as error:  # noqa: BLE001 - kept for the test
            if not self._stop.is_set():
                self.errors.append(f"{type(error).__name__}: {error}")
        finally:
            try:
                connection.close()
            except OSError:
                pass

    def _session(self, connection: socket.socket) -> None:
        connection.settimeout(30)
        reader = connection.makefile("rb")
        writer = connection.makefile("wb")
        try:
            self._converse(reader, writer)
        finally:
            for stream in (writer, reader):
                try:
                    stream.close()
                except OSError:
                    pass

    def _converse(self, reader, writer) -> None:
        def say(text: bytes) -> None:
            writer.write(text)
            writer.flush()

        say(self.greeting.encode())
        sender, recipients, authenticated = "", [], not self.username
        while True:
            line = reader.readline()
            if not line:
                return
            text = line.decode("utf-8", "replace").strip()
            word = text.split(" ", 1)[0].upper()
            rest = text[len(word):].strip()
            if word in ("EHLO", "HELO"):
                self.client_names.append(rest)
                replies = ["250-fake-relay"]
                if self.username:
                    replies.append("250-AUTH LOGIN PLAIN")
                if self.starttls:
                    replies.append("250-STARTTLS")
                replies.append("250 HELP")
                say(("\r\n".join(replies) + "\r\n").encode())
            elif word == "STARTTLS":
                # Offered only to prove the harness asks for it; this fake
                # has no certificate, so the upgrade itself is refused.
                say(b"454 TLS not available on this fake\r\n")
            elif word == "AUTH":
                authenticated = self._auth(reader, say, rest)
                say(
                    b"235 authenticated\r\n" if authenticated
                    else b"535 authentication failed\r\n"
                )
            elif word == "MAIL":
                if not authenticated:
                    say(b"530 authentication required\r\n")
                else:
                    sender, recipients = _address(rest), []
                    say(b"250 sender ok\r\n")
            elif word == "RCPT":
                if self.reject_recipients:
                    say(b"550 no such recipient\r\n")
                else:
                    recipients.append(_address(rest))
                    say(b"250 recipient ok\r\n")
            elif word == "DATA":
                self.deliveries += 1
                if self.deliveries <= self.fail_first:
                    say(b"451 try again later\r\n")
                    continue
                say(b"354 send it\r\n")
                self.messages.append({
                    "sender": sender, "recipients": list(recipients),
                    "data": _read_data(reader),
                })
                say(b"250 queued\r\n")
            elif word == "RSET":
                sender, recipients = "", []
                say(b"250 reset\r\n")
            elif word == "NOOP":
                say(b"250 ok\r\n")
            elif word == "QUIT":
                say(b"221 bye\r\n")
                return
            else:
                say(b"502 not implemented\r\n")

    def _auth(self, reader, say, rest: str) -> bool:
        mechanism = rest.split(" ", 1)[0].upper()
        initial = rest[len(mechanism):].strip()
        if mechanism == "PLAIN":
            if not initial:
                say(b"334 \r\n")
                initial = reader.readline().decode().strip()
            try:
                _, user, password = base64.b64decode(initial).decode().split("\0")
            except (ValueError, UnicodeDecodeError):
                return False
        elif mechanism == "LOGIN":
            if initial:
                # smtplib sends the user name as the initial response.
                user = _decode(initial.encode())
            else:
                say(b"334 VXNlcm5hbWU6\r\n")
                user = _decode(reader.readline())
            say(b"334 UGFzc3dvcmQ6\r\n")
            password = _decode(reader.readline())
        else:
            return False
        self.auth_attempts.append(user)
        return user == self.username and password == self.password


def _read_data(reader) -> bytes:
    lines = []
    while True:
        line = reader.readline()
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
