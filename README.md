# imap-mail-mcp

Server [MCP](https://modelcontextprotocol.io) in **sola lettura** per leggere la posta IMAP da
Claude Code (o da un altro client MCP), installabile come plugin di Claude Code. Un solo file
Python, nessuna installazione: lo avvia `uv`.

- Le cartelle si aprono con `EXAMINE` e i messaggi si leggono con `BODY.PEEK`: nessun flag cambiato,
  niente spostato o cancellato, niente invio.
- La password sta nel portachiavi del sistema (libsecret su Linux, Portachiavi su macOS), mai in un file.
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

Serve [`uv`](https://docs.astral.sh/uv/) nel `PATH` (`brew install uv` su macOS).

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

## Licenza

MIT
