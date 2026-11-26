#!/bin/sh
# Hook SessionStart del plugin imap-mail: il server MCP si avvia con uv. Se uv manca,
# /mcp mostra solo "Failed to connect"; qui si spiega all'utente (e a Claude) come installarlo.
command -v uv >/dev/null 2>&1 && exit 0

case "$(uname -s 2>/dev/null)" in
  MINGW* | MSYS* | CYGWIN*)
    install='winget install --id=astral-sh.uv -e' ;;
  Darwin)
    install='brew install uv (oppure: curl -LsSf https://astral.sh/uv/install.sh | sh)' ;;
  *)
    install='curl -LsSf https://astral.sh/uv/install.sh | sh' ;;
esac

msg="imap-mail: uv non trovato nel PATH, il server della posta non può partire. Installalo con: $install - poi riavvia Claude Code (Python non serve, lo scarica uv)."
# I messaggi non contengono virgolette né backslash, quindi vanno in JSON senza escape.
printf '{"systemMessage": "%s", "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "%s I tool imap-mail non sono disponibili finché uv non è installato: se l utente chiede della posta, spiegagli come installare uv."}}\n' "$msg" "$msg"
exit 0
