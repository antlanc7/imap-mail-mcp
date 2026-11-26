"""Test di search() con un server IMAP finto, usando il vero imaplib per lo spezzettamento.

Esecuzione:  uv run --with "mcp>=1.2,<2" --with pytest pytest -q tests
"""

import imaplib
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402

LONG_NAME = "PrezziScania_2026 vs 2027 x 019372_ 4.1.539.2_6 .xlsx"

HDR_LONG = (
    b"Date: Thu, 26 Nov 2026 10:15:00 +0100\r\n"
    b"From: F Rea <f.rea@example.com>\r\n"
    b"To: Antonio <antonio@example.com>\r\n"
    b"Cc: Lab <lab@example.com>\r\n"
    b"Subject: Prezzi Scania 2026 vs 2027\r\n\r\n"
)
HDR_PLAIN = (
    b"Date: Wed, 25 Nov 2026 09:00:00 +0100\r\n"
    b"From: Mario <mario@example.com>\r\n"
    b"To: Antonio <antonio@example.com>\r\n"
    b"Subject: =?utf-8?q?Caff=C3=A8?=\r\n\r\n"
)


def _lit(b: bytes) -> bytes:
    return b"{%d}\r\n" % len(b) + b


def _fetch_long(order: str) -> bytes:
    """Risposta FETCH con il nome dell'allegato come literal nel BODYSTRUCTURE."""
    name = LONG_NAME.encode()
    bs = (
        b'BODYSTRUCTURE (("text" "plain" ("charset" "utf-8") NIL NIL "quoted-printable" 120 4 NIL NIL NIL NIL)'
        b'("application" "vnd.openxmlformats-officedocument.spreadsheetml.sheet" ("name" ' + _lit(name)
        + b') NIL NIL "base64" 52344 NIL ("attachment" ("filename" ' + _lit(name)
        + b')) NIL NIL) "mixed" ("boundary" "xyz") NIL NIL NIL)'
    )
    hdr = b"BODY[HEADER.FIELDS (DATE FROM TO CC SUBJECT)] " + _lit(HDR_LONG)
    flags = b"FLAGS (\\Seen)"
    parts = {"bs-first": [b"UID 73463", flags, bs, hdr], "hdr-first": [hdr, flags, b"UID 73463", bs]}[order]
    return b"* 12 FETCH (" + b" ".join(parts) + b")\r\n"


def _fetch_plain() -> bytes:
    bs = (
        b'BODYSTRUCTURE (("text" "plain" ("charset" "utf-8") NIL NIL "7bit" 10 1 NIL NIL NIL NIL)'
        b'("application" "pdf" ("name" "offerta 1.pdf") NIL NIL "base64" 100 NIL '
        b'("attachment" ("filename" "offerta 1.pdf")) NIL NIL) "mixed" ("boundary" "b") NIL NIL NIL)'
    )
    return (b"* 11 FETCH (UID 73400 FLAGS () " + bs
            + b" BODY[HEADER.FIELDS (DATE FROM TO CC SUBJECT)] " + _lit(HDR_PLAIN) + b")\r\n")


class FakeIMAP(imaplib.IMAP4):
    """Server IMAP scriptato: risponde ai comandi che imaplib invia."""

    fetch_order = "bs-first"

    def open(self, host="", port=0, timeout=None):
        self._out = bytearray(b"* OK fake ready\r\n")
        self._in = bytearray()

    def readline(self):
        i = self._out.index(b"\n") + 1
        line, self._out[:] = bytes(self._out[:i]), self._out[i:]
        return line

    def read(self, size):
        chunk, self._out[:] = bytes(self._out[:size]), self._out[size:]
        return chunk

    def send(self, data):
        self._in += data
        while b"\r\n" in self._in:
            i = self._in.index(b"\r\n")
            line, self._in[:] = bytes(self._in[:i]), self._in[i + 2:]
            self._reply(line)

    def shutdown(self):
        pass

    def _reply(self, line: bytes):
        tag, cmd = line.split(b" ", 1)
        up = cmd.upper()
        if up.startswith(b"CAPABILITY"):
            self._out += b"* CAPABILITY IMAP4rev1\r\n"
        elif up.startswith(b"EXAMINE"):
            self._out += b"* 12 EXISTS\r\n"
        elif up.startswith(b"UID SEARCH"):
            self._out += b"* SEARCH 73400 73463\r\n"
        elif up.startswith(b"UID FETCH"):
            assert re.search(rb"BODY\.PEEK\[HEADER\.FIELDS", cmd)
            self._out += _fetch_plain() + _fetch_long(self.fetch_order)
        elif up.startswith(b"LOGOUT"):
            self._out += b"* BYE\r\n"
        self._out += tag + b" OK done\r\n"


@pytest.fixture(params=["bs-first", "hdr-first"])
def fake(monkeypatch, request):
    FakeIMAP.fetch_order = request.param

    def connect(account):
        conn = FakeIMAP()
        conn.state = "AUTH"  # come dopo login()
        return conn

    monkeypatch.setattr(server, "_connect", connect)


def test_literal_in_bodystructure_keeps_headers(fake):
    rows = server.search(folder="INBOX", from_="f.rea")
    assert [r["uid"] for r in rows] == ["73463", "73400"]
    r = rows[0]
    assert r["from"] == "F Rea <f.rea@example.com>"
    assert r["to"] == "Antonio <antonio@example.com>"
    assert r["cc"] == "Lab <lab@example.com>"
    assert r["subject"] == "Prezzi Scania 2026 vs 2027"
    assert r["date"] is not None and r["date"].startswith("2026-11-26")
    assert r["seen"] is True
    assert r["attachments"] == [LONG_NAME]


def test_plain_message_still_works(fake):
    r = server.search(folder="INBOX")[1]
    assert r["uid"] == "73400"
    assert r["subject"] == "Caffè"
    assert r["seen"] is False
    assert r["attachments"] == ["offerta 1.pdf"]


def test_parse_rfc2231_and_encoded_names():
    resp = server._parse_fetch(
        b'7 FETCH (UID 5 BODYSTRUCTURE ("application" "pdf" ("name" "=?utf-8?q?pi=C3=B9.pdf?=") NIL NIL '
        b'"base64" 1 NIL ("attachment" ("filename*0*" "utf-8\'\'pi%C3%B9" "filename*1*" ".pdf")) NIL NIL))'
    )
    assert resp["UID"] == "5"
    assert server._bodystructure_names(resp["BODYSTRUCTURE"]) == ["più.pdf"]
