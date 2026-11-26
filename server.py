# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.2,<2", "keyring>=24; sys_platform == 'win32'"]
# # mcp va tenuto <2: dalla 2.x mcp.server.fastmcp.FastMCP non esiste più (rinominato MCPServer)
# ///
"""Server MCP IMAP in sola lettura.

- Le cartelle si aprono con EXAMINE (readonly) e i messaggi si leggono con BODY.PEEK:
  nessun flag viene cambiato, niente viene spostato o cancellato, niente invio.
- Configurazione con le variabili d'ambiente IMAP_HOST, IMAP_PORT (993) e IMAP_ACCOUNTS
  (indirizzi separati da virgole, il primo è il predefinito).
- La password viene dal portachiavi del sistema, mai da file o variabili d'ambiente:
    Linux (libsecret): secret-tool store --label="IMAP <account>" service imap-mail account <account>
    macOS (Portachiavi): security add-generic-password -s imap-mail -a <account> -w
    Windows (Gestione credenziali): uvx keyring set imap-mail <account>
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
    if sys.platform == "win32":
        import keyring  # solo su Windows: Gestione credenziali (Windows Credential Manager)

        pw = keyring.get_password("imap-mail", account)
        if not pw:
            raise RuntimeError(
                f"Password di {account} non trovata in Gestione credenziali. "
                f"Salvarla con: uvx keyring set imap-mail {account}"
            )
        return pw
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


# Nomi riservati di Windows, vietati anche con un'estensione (CON.txt)
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


def _attachments(msg):
    for part in msg.walk():
        name = part.get_filename()
        if name:
            yield _dec(name), part


def _fetch_responses(data):
    """Ricompone le risposte FETCH spezzate da imaplib.

    imaplib restituisce, per ogni risposta, una tupla (testo fino a {n}, literal) per ciascun
    literal e poi un bytes con il resto della riga; senza literal, un solo bytes.
    """
    buf: list[bytes] = []
    for item in data or []:
        if isinstance(item, tuple):
            buf += [item[0], item[1]]
        elif isinstance(item, bytes):
            buf.append(item)
            yield buf
            buf = []
    if buf:
        yield buf


def _parse_fetch(resp) -> dict:
    """Interpreta una risposta '<n> FETCH (...)' e restituisce {CHIAVE: valore}.

    resp: bytes oppure lista alternata [testo, literal, testo, literal, ..., testo].
    Valori: str per gli atomi, bytes per stringhe e literal, None per NIL, liste per (...).
    """
    parts = [resp] if isinstance(resp, bytes) else list(resp)
    # Token: ("lit", bytes) per i literal, altrimenti caratteri del testo
    tokens: list = []
    for i, p in enumerate(parts):
        if i % 2:
            tokens.append(("lit", p))
        else:
            text = re.sub(rb"\{\d+\}\r?\n?$", b"", p) if i + 1 < len(parts) else p
            tokens.extend(text[j:j + 1] for j in range(len(text)))
    pos = 0

    def parse_list():
        nonlocal pos
        out = []
        while pos < len(tokens):
            t = tokens[pos]
            if isinstance(t, tuple):
                out.append(t[1])
                pos += 1
            elif t in (b" ", b"\r", b"\n"):
                pos += 1
            elif t == b"(":
                pos += 1
                out.append(parse_list())
            elif t == b")":
                pos += 1
                return out
            elif t == b'"':
                pos += 1
                s = bytearray()
                while pos < len(tokens) and tokens[pos] != b'"':
                    if tokens[pos] == b"\\":
                        pos += 1
                    if pos < len(tokens) and not isinstance(tokens[pos], tuple):
                        s += tokens[pos]
                    pos += 1
                pos += 1
                out.append(bytes(s))
            else:  # atomo, con [...] incluse (es. BODY[HEADER.FIELDS (DATE FROM)])
                s = bytearray()
                depth = 0
                while pos < len(tokens) and not isinstance(tokens[pos], tuple):
                    c = tokens[pos]
                    if depth == 0 and c in (b" ", b"(", b")", b"\r", b"\n"):
                        break
                    depth += (c == b"[") - (c == b"]")
                    s += c
                    pos += 1
                atom = s.decode(errors="replace")
                out.append(None if atom.upper() == "NIL" else atom)
        return out

    top = parse_list()
    items = next((x for x in top if isinstance(x, list)), [])
    result = {}
    for k, v in zip(items[0::2], items[1::2]):
        if isinstance(k, str):
            key = k.upper()
            result["BODY[HEADER]" if key.startswith("BODY[HEADER") else key] = v
    return result


def _bodystructure_names(bs) -> list[str]:
    """Nomi degli allegati (parametri name/filename, anche RFC 2231) da un BODYSTRUCTURE."""
    names: list[str] = []

    def walk(node):
        if not isinstance(node, list):
            return
        if node and len(node) % 2 == 0 and all(isinstance(x, bytes) for x in node):
            pairs = [(k.decode(errors="replace").lower(), v.decode(errors="replace"))
                     for k, v in zip(node[0::2], node[1::2])]
            for base in ("filename", "name"):
                found = [(k, v) for k, v in pairs if k == base or k.startswith(base + "*")]
                if found:
                    if any("*" in k for k, _ in found):
                        try:
                            decoded = email.utils.decode_params([("x", ""), *found])[1:]
                            val = email.utils.unquote(email.utils.collapse_rfc2231_value(decoded[0][1]))
                        except Exception:  # noqa: BLE001
                            val = found[0][1]
                    else:
                        val = found[0][1]
                    names.append(_dec(val))
            return
        for child in node:
            walk(child)

    walk(bs)
    return list(dict.fromkeys(n for n in names if n))


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
        # Le stringhe del BODYSTRUCTURE (nomi lunghi o con caratteri speciali) possono
        # arrivare come literal {n}: si ricompone la risposta e si cercano le voci per nome.
        for resp in _fetch_responses(data if typ == "OK" else []):
            try:
                fields = _parse_fetch(resp)
            except Exception:  # noqa: BLE001
                continue
            uid = fields.get("UID")
            if not uid:
                continue
            hdr = fields.get("BODY[HEADER]")
            h = email.message_from_bytes(hdr if isinstance(hdr, bytes) else b"")
            date = h.get("Date")
            try:
                date = email.utils.parsedate_to_datetime(date).astimezone().isoformat(timespec="minutes")
            except Exception:  # noqa: BLE001
                pass
            flags = fields.get("FLAGS")
            rows[uid] = {
                "uid": uid,
                "date": date,
                "from": _dec(h.get("From")),
                "to": _dec(h.get("To")),
                "cc": _dec(h.get("Cc")),
                "subject": _dec(h.get("Subject")),
                "seen": isinstance(flags, list) and any(
                    isinstance(f, str) and f.lower() == "\\seen" for f in flags),
                "attachments": _bodystructure_names(fields.get("BODYSTRUCTURE")),
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
            safe = re.sub(r"[^\w.\- ()]+", "_", Path(name).name).strip(" .") or "allegato"
            if Path(safe).stem.upper() in _WIN_RESERVED:
                safe = "_" + safe
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
