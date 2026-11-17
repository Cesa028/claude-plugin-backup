# Plugin Backup per Claude Code

Salva tutti i plugin e i marketplace di Claude Code in un unico file `.zip` e rimettili
su un altro computer con un clic. Funziona anche offline (i file dei plugin sono nel backup).

## Uso

1. Installa `ClaudePluginBackup-Setup-1.1.0.exe` (o avvia `ClaudePluginBackup.pyw`).
2. Scegli cosa **includere** e premi **Crea backup** (Ctrl+B): il file finisce in `Documenti\Claude Plugin Backup`.
3. Sul nuovo computer installa Claude Code e fai **/login**, poi apri il programma e **trascina lo `.zip`
   nella finestra** (oppure *Ripristina da file…*, Ctrl+O). Scegli cosa ripristinare, conferma, riavvia Claude Code.

Scorciatoie: Ctrl+B backup · Ctrl+O ripristina da file · F5 aggiorna · Ctrl+, impostazioni ·
F1 informazioni · Esc annulla l'operazione · Ctrl+L registro.

## Cosa contiene un backup

| Categoria | Origine | Note |
|---|---|---|
| Plugin e marketplace | `~/.claude/plugins` | file dei plugin e metadati, percorsi adattati al nuovo utente |
| Skill personali | `~/.claude/skills` | include le skill che sono collegamenti ad altre cartelle (es. `~/.agents/skills`), salvate come cartelle vere |
| Agenti | `~/.claude/agents` | |
| Comandi e regole | `commands`, `rules`, `output-styles`, `themes`, `workflows`, `CLAUDE.md`, `keybindings.json` | |
| Impostazioni | `~/.claude/settings.json` | valori che sembrano segreti omessi; permessi uniti a quelli presenti |
| Server MCP | `mcpServers` di `~/.claude.json` | chiavi API e token omessi: vanno reinseriti; i server già presenti non vengono toccati |

**Non incluso, per scelta:** credenziali di accesso, `history.jsonl`, trascrizioni delle sessioni, cache, e le skill
*sincronizzate dal tuo account claude.ai* (docx, pdf...) e i plugin *integrati* in Claude Code: non sono file,
tornano da soli dopo il `/login`.

## Ripristino

- Tutto viene **unito** a ciò che c'è già; i file con lo stesso nome vengono sostituiti, ma la versione precedente
  resta in `~/.claude/plugin-backup-prima-del-ripristino/<data>`.
- **Reinstalla i plugin tramite Claude** (attivo di default) esegue `claude plugin marketplace add` e
  `claude plugin install` per ogni plugin. Richiede internet e aver fatto `/login`: senza accesso il programma lo
  dice chiaramente invece di fingere che sia andato a buon fine. I plugin dal catalogo `anthropic-plugin-directory`,
  se non raggiungibile, vengono reinstallati da `claude-plugins-official`.

## Sicurezza

- Le **credenziali e i token non vengono mai salvati**. I backup creati prima della 1.0 contenevano per errore i token
  nel manifest: sono segnalati con "Contiene credenziali" e si puliscono dal menu "…" → *Rimuovi credenziali dal file*.

## Riga di comando

```
python claude_plugin_backup.py backup [--dir CARTELLA]
python claude_plugin_backup.py restore [FILE.zip] [--dry-run] [--online]
python claude_plugin_backup.py list
```

`backup.bat` e `restore.bat` (trascina lo zip sopra) fanno la stessa cosa.

## Sviluppo

`pip install PySide6 pyinstaller` · `python build.py` genera `dist\ClaudePluginBackup-Setup-<versione>.exe`
(serve Inno Setup 6). Impostazioni e registro: `%APPDATA%\ClaudePluginBackup`.

Licenza MIT — © 2026 Cesa028. Non affiliato ad Anthropic. Componenti di terze parti in `licenses/`.
