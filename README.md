# imap-mail-mcp

Server [MCP](https://modelcontextprotocol.io) in **sola lettura** per leggere la posta IMAP da
Claude Code (o da un altro client MCP), installabile come plugin di Claude Code. Un solo file
Python, nessuna installazione: lo avvia `uv`.

- Le cartelle si aprono con `EXAMINE` e i messaggi si leggono con `BODY.PEEK`: nessun flag cambiato,
  niente spostato o cancellato, niente invio.
- La password sta nell'archivio del sistema (libsecret su Linux, Portachiavi su macOS, Gestione
  credenziali su Windows), mai in un file.
- Solo IMAP su SSL (porta 993 predefinita) con utente e password.

## Strumenti

| Strumento | Cosa fa |
|---|---|
| `list_accounts` | caselle configurate (la prima è la predefinita) |
| `list_folders` | cartelle della casella |
| `search` | ricerca lato server per mittente, destinatario, oggetto, testo, date (`YYYY-MM-DD` o `7d`), non letti |
| `get_message` | intestazioni, testo (plain o html ripulito) ed elenco degli allegati |
| `save_attachments` | salva gli allegati in una cartella |

## Installazione

Serve [`uv`](https://docs.astral.sh/uv/) nel `PATH`: `curl -LsSf https://astral.sh/uv/install.sh | sh`
su Linux, `brew install uv` su macOS, `winget install --id=astral-sh.uv -e` su Windows. Python non
serve: lo scarica `uv`. Se `uv` manca, il plugin lo segnala all'avvio di ogni sessione con il
comando per installarlo (hook `SessionStart`; su Windows funziona se Claude Code usa Git Bash per gli hook).

### 1. Password nel portachiavi

Una voce per ogni casella; la password non va mai nella configurazione.

macOS (chiede la password senza lasciarla nella cronologia):

```bash
security add-generic-password -s imap-mail -a nome@dominio.it -w
```

Linux (serve `secret-tool`, pacchetto `libsecret-tools` o `libsecret`):

```bash
secret-tool store --label="IMAP nome@dominio.it" service imap-mail account nome@dominio.it
```

Windows (chiede la password; la salva in Gestione credenziali):

```powershell
uvx keyring set imap-mail nome@dominio.it
```

Al primo accesso macOS può chiedere di consentire l'uso del Portachiavi.

### 2. Plugin di Claude Code

Dentro Claude Code:

```
/plugin install imap-mail --marketplace antlanc7/imap-mail-mcp
```

Claude Code chiede server IMAP, porta (993) e caselle (separate da virgole, la prima è la
predefinita). Dalla shell, in alternativa:

```bash
claude plugin marketplace add antlanc7/imap-mail-mcp
claude plugin install imap-mail@imap-mail-mcp \
  --config imap_host=mail.dominio.it --config imap_accounts=nome@dominio.it,altra@dominio.it
```

Per cambiare i valori: `claude plugin configure imap-mail@imap-mail-mcp`. Per gli aggiornamenti:
`claude plugin marketplace update imap-mail-mcp`.

### In alternativa: server MCP senza plugin

Linux e macOS (su Windows lo stesso, con un percorso Windows per `server.py`):

```bash
mkdir -p ~/.claude/mcp/imap-mail
curl -fsSL -o ~/.claude/mcp/imap-mail/server.py \
  https://raw.githubusercontent.com/antlanc7/imap-mail-mcp/main/server.py
claude mcp add imap-mail -s user \
  -e IMAP_HOST=mail.dominio.it \
  -e IMAP_ACCOUNTS=nome@dominio.it,altra@dominio.it \
  -- uv run -q --script ~/.claude/mcp/imap-mail/server.py
```

| Variabile | Significato |
|---|---|
| `IMAP_HOST` | server IMAP (obbligatoria) |
| `IMAP_PORT` | porta SSL, predefinita `993` |
| `IMAP_ACCOUNTS` | caselle separate da virgole, la prima è la predefinita (obbligatoria) |

## Sicurezza

Il contenuto delle mail arriva al modello come testo: va trattato come dato, non come istruzioni.
Gli allegati salvati sono file non fidati: conviene salvarli in una cartella nuova e non eseguirli.

## Test

```bash
uv run --with "mcp>=1.2,<2" --with pytest pytest -q tests
```

La dipendenza `mcp` resta `<2`: dalla 2.x `mcp.server.fastmcp.FastMCP` è stato rinominato
`MCPServer` e il server non partirebbe.

## Licenza

MIT
