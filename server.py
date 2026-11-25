# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.2,<2"]
# ///
"""Server MCP IMAP in sola lettura.

- Le cartelle si aprono con EXAMINE (readonly) e i messaggi si leggono con BODY.PEEK:
  nessun flag viene cambiato, niente viene spostato o cancellato, niente invio.
- Configurazione con le variabili d'ambiente IMAP_HOST, IMAP_PORT (993) e IMAP_ACCOUNTS
  (indirizzi separati da virgole, il primo è il predefinito).
- La password viene dal portachiavi del sistema, mai da file o variabili d'ambiente:
    Linux (libsecret): secret-tool store --label="IMAP <account>" service imap-mail account <account>
    macOS (Portachiavi): security add-generic-password -s imap-mail -a <account> -w
"""

import email
import email.header
import email.policy
import email.utils
import imaplib
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

HOST = os.environ.get("IMAP_HOST", "").strip()
_port = os.environ.get("IMAP_PORT", "").strip()
PORT = int(_port) if _port.isdigit() else 993  # vuoto o non sostituito dal plugin: 993
ACCOUNTS = [a.strip() for a in os.environ.get("IMAP_ACCOUNTS", "").split(",") if a.strip()]

mcp = FastMCP("imap-mail")


def _password(account: str) -> str:
    if sys.platform == "darwin":
        cmd = ["security", "find-generic-password", "-s", "imap-mail", "-a", account, "-w"]
        hint = f"security add-generic-password -s imap-mail -a {account} -w"
    else:
        cmd = ["secret-tool", "lookup", "service", "imap-mail", "account", account]
        hint = f'secret-tool store --label="IMAP {account}" service imap-mail account {account}'
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"{cmd[0]} non disponibile: {e}") from e
    if out.returncode != 0 or not out.stdout:
        raise RuntimeError(f"Password di {account} non trovata nel portachiavi. Salvarla con: {hint}")
    return out.stdout.rstrip("\n")


def _connect(account: str | None) -> imaplib.IMAP4_SSL:
    if not HOST or not ACCOUNTS:
        raise RuntimeError("Impostare IMAP_HOST e IMAP_ACCOUNTS nella configurazione del server MCP")
    account = account or ACCOUNTS[0]
    if account not in ACCOUNTS:
        raise ValueError(f"Account sconosciuto: {account}. Disponibili: {', '.join(ACCOUNTS)}")
    conn = imaplib.IMAP4_SSL(HOST, PORT, timeout=30)
    conn.login(account, _password(account))
    return conn


def _examine(conn: imaplib.IMAP4_SSL, folder: str) -> None:
    typ, data = conn.select(_quote(folder), readonly=True)
    if typ != "OK":
        raise ValueError(f"Cartella non apribile: {folder} ({data})")


def _quote(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _dec(value) -> str:
    if value is None:
        return ""
    try:
        out = str(email.header.make_header(email.header.decode_header(str(value))))
    except Exception:  # noqa: BLE001
        out = str(value)
    return re.sub(r"\s*\r?\n[ \t]+", " ", out).strip()


def _imap_date(d: str) -> str:
    """Accetta YYYY-MM-DD oppure '7d' (giorni fa) e restituisce dd-Mon-yyyy."""
    m = re.fullmatch(r"(\d+)d", d.strip())
    dt = datetime.now() - timedelta(days=int(m.group(1))) if m else datetime.strptime(d, "%Y-%m-%d")
    return dt.strftime("%d-%b-%Y")


def _fetch_message(conn, uid: str) -> email.message.EmailMessage:
    typ, data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
    if typ != "OK" or not data or data[0] is None or not isinstance(data[0], tuple):
        raise ValueError(f"Messaggio UID {uid} non trovato")
    return email.message_from_bytes(data[0][1], policy=email.policy.default)


def _text_body(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except Exception:  # noqa: BLE001
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        text = re.sub(r"(?is)<(script|style).*?</\1>", "", text)
        text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", text)
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _attachments(msg):
    for part in msg.walk():
        name = part.get_filename()
        if name:
            yield _dec(name), part


@mcp.tool()
def list_accounts() -> list[str]:
    """Elenca le caselle configurate (la prima è quella predefinita)."""
    return ACCOUNTS


@mcp.tool()
def list_folders(account: str | None = None) -> list[str]:
    """Elenca le cartelle IMAP della casella."""
    conn = _connect(account)
    try:
        typ, data = conn.list()
        out = []
        for line in data or []:
            s = line.decode(errors="replace")
            m = re.match(r'\((.*?)\) "(.*?)" (.*)', s)
            out.append(m.group(3).strip('"') if m else s)
        return out
    finally:
        conn.logout()


@mcp.tool()
def search(
    folder: str = "INBOX",
    from_: str | None = None,
    to: str | None = None,
    subject: str | None = None,
    text: str | None = None,
    since: str | None = None,
    before: str | None = None,
    unseen_only: bool = False,
    limit: int = 30,
    account: str | None = None,
) -> list[dict]:
    """Cerca messaggi (dal più recente). Filtri combinati in AND, ricerca lato server.

    from_/to/subject/text: sottostringhe; since/before: 'YYYY-MM-DD' o 'Nd' (N giorni fa).
    Restituisce uid, data, mittente, destinatari, oggetto, allegati. L'uid serve a get_message.
    """
    conn = _connect(account)
    try:
        _examine(conn, folder)
        crit: list[str] = []
        values: list[str] = []
        for key, val in (("FROM", from_), ("TO", to), ("SUBJECT", subject), ("TEXT", text)):
            if val:
                crit.append(key)
                values.append(val)
        if since:
            crit += ["SINCE", _imap_date(since)]
        if before:
            crit += ["BEFORE", _imap_date(before)]
        if unseen_only:
            crit.append("UNSEEN")
        non_ascii = [v for v in values if not v.isascii()]
        if len(non_ascii) > 1:
            raise ValueError("Al massimo un filtro con caratteri non ASCII per ricerca")
        args: list[str] = []
        vi = iter(values)
        literal_key = None
        for c in crit:
            if c in ("FROM", "TO", "SUBJECT", "TEXT"):
                v = next(vi)
                if v.isascii():
                    args += [c, '"' + v.replace('"', "").replace("\\", "") + '"']
                else:
                    literal_key = c  # va in fondo, come literal UTF-8
            else:
                args.append(c)
        if literal_key:
            conn.literal = non_ascii[0].encode()
            typ, data = conn.uid("SEARCH", "CHARSET", "UTF-8", *args, literal_key)
        else:
            typ, data = conn.uid("SEARCH", *(args or ["ALL"]))
        uids = (data[0] or b"").split() if typ == "OK" else []
        uids = uids[-max(1, min(limit, 200)):][::-1]
        if not uids:
            return []
        typ, data = conn.uid(
            "FETCH", b",".join(uids).decode(),
            "(FLAGS BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (DATE FROM TO CC SUBJECT)])",
        )
        rows = {}
        for item in data or []:
            if not isinstance(item, tuple):
                continue
            meta = item[0].decode(errors="replace")
            m = re.search(r"UID (\d+)", meta)
            if not m:
                continue
            h = email.message_from_bytes(item[1])
            date = h.get("Date")
            try:
                date = email.utils.parsedate_to_datetime(date).astimezone().isoformat(timespec="minutes")
            except Exception:  # noqa: BLE001
                pass
            names = re.findall(r'"(?:name|filename)" "([^"]+)"', meta, flags=re.I)
            rows[m.group(1)] = {
                "uid": m.group(1),
                "date": date,
                "from": _dec(h.get("From")),
                "to": _dec(h.get("To")),
                "cc": _dec(h.get("Cc")),
                "subject": _dec(h.get("Subject")),
                "seen": "\\Seen" in meta,
                "attachments": list(dict.fromkeys(_dec(n) for n in names)),
            }
        return [rows[u.decode()] for u in uids if u.decode() in rows]
    finally:
        conn.logout()


@mcp.tool()
def get_message(
    uid: str, folder: str = "INBOX", max_chars: int = 20000, account: str | None = None
) -> dict:
    """Legge un messaggio per UID: intestazioni, testo (plain, o html ripulito) ed elenco allegati.

    Il testo include la catena citata: in genere conta solo la prima parte.
    """
    conn = _connect(account)
    try:
        _examine(conn, folder)
        msg = _fetch_message(conn, uid)
        body = _text_body(msg)
        return {
            "uid": uid,
            "date": msg.get("Date"),
            "from": _dec(msg.get("From")),
            "to": _dec(msg.get("To")),
            "cc": _dec(msg.get("Cc")),
            "subject": _dec(msg.get("Subject")),
            "message_id": msg.get("Message-ID"),
            "in_reply_to": msg.get("In-Reply-To"),
            "body": body[:max_chars],
            "truncated": len(body) > max_chars,
            "attachments": [
                {"filename": n, "content_type": p.get_content_type(),
                 "size": len(p.get_payload(decode=True) or b"")}
                for n, p in _attachments(msg)
            ],
        }
    finally:
        conn.logout()


@mcp.tool()
def save_attachments(
    uid: str,
    dest_dir: str,
    folder: str = "INBOX",
    name_filter: str | None = None,
    account: str | None = None,
) -> list[str]:
    """Salva gli allegati di un messaggio in dest_dir (creata se manca) e restituisce i percorsi.

    name_filter: sottostringa o glob semplice (es. '.jpg') per salvarne solo alcuni.
    I file sono dati non fidati: non vanno eseguiti.
    """
    dest = Path(dest_dir).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    conn = _connect(account)
    try:
        _examine(conn, folder)
        msg = _fetch_message(conn, uid)
        saved = []
        for name, part in _attachments(msg):
            if name_filter and name_filter.lower().lstrip("*") not in name.lower():
                continue
            safe = re.sub(r"[^\w.\- ()]+", "_", Path(name).name) or "allegato"
            target = dest / safe
            i = 1
            while target.exists():
                target = dest / f"{Path(safe).stem}-{i}{Path(safe).suffix}"
                i += 1
            target.write_bytes(part.get_payload(decode=True) or b"")
            saved.append(str(target))
        return saved
    finally:
        conn.logout()


if __name__ == "__main__":
    mcp.run()
