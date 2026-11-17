"""Plugin Backup - motore di backup e ripristino (nessuna dipendenza da Qt).

Un backup è uno zip con:
  manifest.json            riepilogo (cosa contiene, plugin, marketplace, server MCP, valori oscurati)
  root/<file>.json         metadati di ~/.claude/plugins
  plugins/<cartella>/...   codice dei plugin, marketplace, dati
  user/<percorso>          skill, agenti, comandi, regole, CLAUDE.md... (relativi a ~/.claude)
  user/settings.json       impostazioni di Claude Code (senza valori segreti)
  user/mcp.json            server MCP globali (senza chiavi API e token)
Le credenziali di accesso e i token NON vengono mai inclusi.

Sicurezza del ripristino: uno zip può arrivare da fuori, quindi nomi dei file, id dei plugin e sorgenti dei
marketplace vengono validati prima di scrivere sul disco o di passarli alla CLI di Claude.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from common import APP_ID, APP_VERSION

# --------------------------------------------------------------------------- #
# Categorie
# --------------------------------------------------------------------------- #
CATEGORIES = ["plugins", "skills", "agents", "commands", "settings", "mcp"]
CATEGORY_LABELS = {
    "plugins": "Plugin e marketplace",
    "skills": "Skill personali",
    "agents": "Agenti",
    "commands": "Comandi, regole e CLAUDE.md",
    "settings": "Impostazioni di Claude",
    "mcp": "Server MCP",
}

# File di metadati in ~/.claude/plugins (contengono percorsi assoluti).
ROOT_METADATA_FILES = [
    "installed_plugins.json",
    "known_marketplaces.json",
    "plugin-catalog-cache.json",
    "plugin-directory-bindings.json",
    "plugin-directory-cache-v2.json",
]
MERGED_FILES = {"installed_plugins.json", "known_marketplaces.json", "plugin-directory-bindings.json"}
DATA_SUBDIRS = ["cache", "marketplaces", "data", "synced"]
SETTINGS_KEYS = ("enabledPlugins", "extraKnownMarketplaces")  # le uniche chiavi dei plugin

# Cartelle/file personali di ~/.claude, per categoria.
USER_DIRS = {
    "skills": ["skills"],
    "agents": ["agents"],
    "commands": ["commands", "rules", "output-styles", "themes", "workflows"],
}
USER_FILES = {"commands": ["CLAUDE.md", "keybindings.json"]}
SKIP_DIR_NAMES = {".git", "node_modules", "__pycache__", ".trash"}
SKIP_TOP = {"skills": {"synced"}}  # le skill sincronizzate dall'account tornano da sole con /login

# Impostazioni: chiavi che si uniscono invece di sostituirsi.
DICT_MERGE_KEYS = {"enabledPlugins", "extraKnownMarketplaces", "pluginConfigs", "env"}

_SECRET_WORDS = r"(?:key|token|secret|passw(?:or)?d|pwd|auth|cookie|credential|bearer|dsn|signature)"
SECRET_RE = re.compile(_SECRET_WORDS, re.I)
_ARG_EQ_RE = re.compile(r"^(--?[a-z0-9_.-]*" + _SECRET_WORDS + r"[a-z0-9_.-]*=)(.+)$", re.I | re.S)
_ARG_FLAG_RE = re.compile(r"^--?[a-z0-9_.-]*" + _SECRET_WORDS + r"[a-z0-9_.-]*$", re.I)
_URL_QUERY_RE = re.compile(r"([?&][a-z0-9_.-]*(?:key|token|secret|passw|pwd|auth|sig|session|cookie)[a-z0-9_.-]*=)[^&#\s]+", re.I)
_URL_USERINFO_RE = re.compile(r"(://[^/@\s:]+):[^/@\s]+@")
MANIFEST_VERSION = 3

_ABS_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|/|\\\\)")
_CLAUDE_PLUGINS_RE = re.compile(r"[\\/]\.claude[\\/]plugins[\\/]")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Valori che finiscono come argomenti della CLI: solo caratteri innocui, mai un '-' iniziale.
_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}"
_PLUGIN_ID_RE = re.compile(rf"{_NAME}@{_NAME}")
_REPO_RE = re.compile(rf"{_NAME}/{_NAME}|https://[A-Za-z0-9._~/:-]{{1,300}}")
_PLAIN_NAME_RE = re.compile(_NAME)


class Cancelled(Exception):
    """L'utente ha annullato l'operazione."""


class BackupError(Exception):
    """Errore con un messaggio già comprensibile per l'utente."""


# --------------------------------------------------------------------------- #
# Contesto: progresso, registro, annullamento
# --------------------------------------------------------------------------- #
@dataclass
class Ctx:
    progress: Callable[[Optional[float], str], None] = lambda frac, text: None
    log: Callable[[str], None] = lambda text: None
    cancel: threading.Event = field(default_factory=threading.Event)

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()


# --------------------------------------------------------------------------- #
# Percorsi
# --------------------------------------------------------------------------- #
def home_dir() -> Path:
    for var in ("USERPROFILE", "HOME"):
        v = os.environ.get(var)
        if v:
            return Path(v)
    return Path.home()


def claude_dir() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else home_dir() / ".claude"


def claude_json_path() -> Path:
    """~/.claude.json (o <CLAUDE_CONFIG_DIR>/.claude.json se la cartella è spostata)."""
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        return claude_dir() / ".claude.json"
    return home_dir() / ".claude.json"


def plugins_dir() -> Path:
    return claude_dir() / "plugins"


def find_claude() -> Optional[str]:
    """Preferisce claude.exe (installer nativo) al wrapper .cmd di npm, che passa per cmd.exe."""
    return shutil.which("claude.exe") or shutil.which("claude")


def claude_version() -> str:
    exe = find_claude()
    if not exe:
        return ""
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=10, creationflags=_NO_WINDOW, stdin=subprocess.DEVNULL)
        return r.stdout.strip()
    except Exception:
        return ""


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _is_link(p: Path) -> bool:
    try:
        return p.is_symlink() or os.path.isjunction(p)
    except OSError:
        return True


def _walk_files(root: Path) -> Iterator[Path]:
    """File sotto root, senza seguire symlink né junction (cartelle dei plugin)."""
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not _is_link(Path(dirpath, d))]
        for name in files:
            p = Path(dirpath) / name
            if not _is_link(p):
                yield p


def walk_tree(root: Path, skip_top: set[str] = frozenset()) -> Iterator[tuple[Path, str, int]]:
    """(file, percorso relativo con '/', dimensione). Segue i collegamenti a cartelle (junction/symlink),
    così le skill che sono link ad altre cartelle vengono incluse come cartelle vere; evita i cicli e i
    collegamenti che puntano a una cartella che contiene la radice stessa."""
    try:
        root_real = os.path.realpath(root)
    except OSError:
        return

    def rec(d: Path, rel: str, chain: frozenset, top: bool):
        try:
            real = os.path.realpath(d)
            entries = sorted(os.scandir(d), key=lambda e: e.name.lower())
        except OSError:
            return
        if real in chain:
            return
        chain = chain | {real}
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=True):
                    if e.name in SKIP_DIR_NAMES or (top and e.name in skip_top):
                        continue
                    target = os.path.realpath(e.path)
                    if root_real == target or root_real.startswith(target.rstrip("\\/") + os.sep):
                        continue  # il collegamento punta a un antenato: duplicherebbe tutto
                    yield from rec(Path(e.path), f"{rel}{e.name}/", chain, False)
                elif e.is_file(follow_symlinks=True):
                    yield Path(e.path), f"{rel}{e.name}", e.stat().st_size
            except OSError:
                continue

    if root.is_dir():
        yield from rec(root, "", frozenset(), True)


def user_files(category: str) -> list[tuple[Path, str, int]]:
    """File personali di una categoria: (origine, nome nello zip 'user/...', dimensione)."""
    base = claude_dir()
    out: list[tuple[Path, str, int]] = []
    for sub in USER_DIRS.get(category, []):
        skip = SKIP_TOP.get(category, set()) if sub == category else set()
        for p, rel, n in walk_tree(base / sub, skip):
            out.append((p, f"user/{sub}/{rel}", n))
    for name in USER_FILES.get(category, []):
        p = base / name
        if p.is_file():
            out.append((p, f"user/{name}", p.stat().st_size))
    return out


# --------------------------------------------------------------------------- #
# Segreti e percorsi
# --------------------------------------------------------------------------- #
def _mask_str(s: str) -> tuple[str, list[str]]:
    """Toglie i segreti incorporati in una stringa: '--api-key=XYZ', '?token=XYZ', 'https://utente:password@host'."""
    names: list[str] = []
    m = _ARG_EQ_RE.match(s)
    if m:
        names.append(m.group(1).rstrip("=").lstrip("-"))
        s = m.group(1)
    for m in _URL_QUERY_RE.finditer(s):
        names.append(m.group(1).strip("?&=").strip())
    s = _URL_QUERY_RE.sub(r"\1", s)
    if _URL_USERINFO_RE.search(s):
        names.append("password-url")
        s = _URL_USERINFO_RE.sub(r"\1@", s)
    return s, names


def mask_secrets(data: Any, where: str = "", server: str = "") -> tuple[Any, list[dict]]:
    """Toglie i valori che sembrano segreti (chiavi API, token, argomenti e URL con credenziali).
    Restituisce (dati puliti, elenco di ciò che è stato oscurato)."""
    masked: list[dict] = []

    def note(path, name):
        item = {"where": ".".join(path), "name": str(name)}
        if server:
            item["server"] = server
        masked.append(item)

    def rec(v, path):
        if isinstance(v, dict):
            out = {}
            for k, val in v.items():
                secret_key = bool(SECRET_RE.search(str(k)))
                if secret_key and ((isinstance(val, str) and val) or
                                   (isinstance(val, list) and val and all(isinstance(x, str) for x in val))):
                    note(path, k)
                    continue
                out[k] = rec(val, path + [str(k)])
            return out
        if isinstance(v, list):
            out, skip_next = [], False
            for i, x in enumerate(v):
                if skip_next:
                    skip_next = False
                    continue
                if isinstance(x, str) and _ARG_FLAG_RE.match(x) and i + 1 < len(v) \
                        and isinstance(v[i + 1], str) and not v[i + 1].startswith("-"):
                    note(path, x.lstrip("-"))  # '--token', 'XYZ': tieni il flag, togli il valore
                    out.append(x)
                    skip_next = True
                    continue
                out.append(rec(x, path))
            return out
        if isinstance(v, str):
            new, names = _mask_str(v)
            for n in names:
                note(path, n)
            return new
        return v

    return rec(data, [where] if where else []), masked


def _valid_home(h: str) -> bool:
    parts = [p for p in re.split(r"[\\/]+", h or "") if p and not p.endswith(":")]
    return bool(h) and bool(_ABS_RE.match(h)) and len(parts) >= 2


def _rewrite_home(data: Any, old_home: str, new_home: str) -> Any:
    """Sostituisce la vecchia cartella home con quella di questo computer nelle stringhe."""
    if not _valid_home(old_home) or old_home.lower() == new_home.lower():
        return data
    variants = {old_home.rstrip("\\/"), old_home.rstrip("\\/").replace("\\", "/")}
    pat = re.compile("(?:" + "|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True))
                     + r")(?![A-Za-z0-9_.-])", re.I)

    def rec(v):
        if isinstance(v, dict):
            return {k: rec(x) for k, x in v.items()}
        if isinstance(v, list):
            return [rec(x) for x in v]
        if isinstance(v, str):
            return pat.sub(lambda m: new_home.replace("\\", "/") if "/" in m.group(0) else new_home, v)
        return v

    return rec(data)


# --------------------------------------------------------------------------- #
# Stato di questa macchina
# --------------------------------------------------------------------------- #
def plugin_entries() -> list[dict]:
    pd = plugins_dir()
    installed = _read_json(pd / "installed_plugins.json", {}).get("plugins", {})
    enabled = _read_json(claude_dir() / "settings.json", {}).get("enabledPlugins", {})
    out = []
    for pid, entries in (installed if isinstance(installed, dict) else {}).items():
        first = entries[0] if isinstance(entries, list) and entries and isinstance(entries[0], dict) else {}
        out.append({
            "id": pid,
            "version": first.get("version", ""),
            "scope": first.get("scope", "user"),
            "enabled": bool(enabled.get(pid, False)) if isinstance(enabled, dict) else False,
            "marketplace": pid.split("@", 1)[1] if "@" in pid else "",
        })
    return sorted(out, key=lambda e: e["id"].lower())


def marketplace_entries() -> list[dict]:
    km = _read_json(plugins_dir() / "known_marketplaces.json", {})
    out = []
    for name, info in (km.items() if isinstance(km, dict) else []):
        src = (info or {}).get("source") or {} if isinstance(info, dict) else {}
        out.append({"name": name, "source": src.get("source", ""),
                    "repo": src.get("repo") or src.get("url") or ""})
    return sorted(out, key=lambda e: e["name"].lower())


def skill_names() -> list[str]:
    """Nomi delle skill personali (cartelle, anche se sono collegamenti)."""
    base = claude_dir() / "skills"
    try:
        return sorted(e.name for e in os.scandir(base)
                      if e.is_dir(follow_symlinks=True) and e.name not in SKIP_DIR_NAMES
                      and e.name not in SKIP_TOP["skills"])
    except OSError:
        return []


def mcp_servers() -> dict:
    d = _read_json(claude_json_path(), {})
    s = d.get("mcpServers") if isinstance(d, dict) else None
    return s if isinstance(s, dict) else {}


def machine_status() -> dict:
    """Numeri mostrati nella schermata principale."""
    pd = plugins_dir()
    size = 0
    for sub in DATA_SUBDIRS:
        d = pd / sub
        if d.is_dir():
            for f in _walk_files(d):
                try:
                    size += f.stat().st_size
                except OSError:
                    pass
    plugins = plugin_entries()
    settings = claude_dir() / "settings.json"
    return {
        "found": claude_dir().is_dir(),
        "plugins_dir": str(pd),
        "plugins": len(plugins),
        "enabled": sum(1 for p in plugins if p["enabled"]),
        "marketplaces": len(marketplace_entries()),
        "size": size,
        "skills": len(skill_names()),
        "agents": len(user_files("agents")),
        "commands": len(user_files("commands")),
        "settings": settings.is_file(),
        "mcp": len(mcp_servers()),
        "claude": find_claude() is not None,
    }


# --------------------------------------------------------------------------- #
# Backup
# --------------------------------------------------------------------------- #
@dataclass
class BackupResult:
    path: Path
    files: int
    skipped: int
    size: int
    includes: list[str] = field(default_factory=list)


def create_backup(dest_dir: str | Path, ctx: Ctx, include: Optional[set] = None) -> BackupResult:
    include = set(CATEGORIES if include is None else include) & set(CATEGORIES)
    if not include:
        raise BackupError("Seleziona almeno una categoria da includere nel backup.")
    cd = claude_dir()
    if not cd.is_dir():
        raise BackupError(f"Non trovo la cartella di Claude Code:\n{cd}\n\nClaude Code è installato su questo computer?")
    dest = Path(dest_dir)
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"Non riesco a creare la cartella dei backup:\n{dest}\n\n{exc}")

    ctx.progress(None, "Analisi di ciò che c'è da salvare…")
    files: list[tuple[Path, str, int]] = []
    root_files: list[tuple[Path, str]] = []
    plugins: list[dict] = []
    markets: list[dict] = []
    extra: dict[str, bytes] = {}
    masked: list[dict] = []
    manifest_user: dict[str, Any] = {}

    pd = plugins_dir()
    if "plugins" in include:
        plugins, markets = plugin_entries(), marketplace_entries()
        ctx.log(f"Plugin: {len(plugins)} installati, {len(markets)} marketplace.")
        for sub in DATA_SUBDIRS:
            base = pd / sub
            if not base.is_dir():
                continue
            for f in _walk_files(base):
                try:
                    files.append((f, f"plugins/{sub}/" + f.relative_to(base).as_posix(), f.stat().st_size))
                except OSError:
                    continue
            ctx.check()
        root_files = [(pd / n, f"root/{n}") for n in ROOT_METADATA_FILES if (pd / n).is_file()]
    for cat in ("skills", "agents", "commands"):
        if cat in include:
            items = user_files(cat)
            files.extend(items)
            ctx.log(f"{CATEGORY_LABELS[cat]}: {len(items)} file.")
            ctx.check()
    if "skills" in include:
        manifest_user["skills"] = skill_names()

    settings = _read_json(cd / "settings.json", {})
    if "settings" in include and isinstance(settings, dict) and settings:
        clean, m = mask_secrets(settings, "settings")
        masked += m
        extra["user/settings.json"] = json.dumps(clean, indent=2, ensure_ascii=False).encode("utf-8")
        ctx.log("Impostazioni di Claude: incluse (senza valori segreti).")
    if "mcp" in include:
        servers = mcp_servers()
        clean_servers = {}
        n_masked = 0
        for name, cfg in servers.items():
            c, m = mask_secrets(cfg, f"mcp.{name}", server=str(name))
            clean_servers[name] = c
            masked += m
            n_masked += len(m)
        extra["user/mcp.json"] = json.dumps({"servers": clean_servers}, indent=2, ensure_ascii=False).encode("utf-8")
        manifest_user["mcp_servers"] = sorted(servers)
        ctx.log(f"Server MCP: {len(servers)} (chiavi API e token oscurati: {n_masked}).")

    manifest = {
        "version": MANIFEST_VERSION,
        "app": APP_ID,
        "app_version": APP_VERSION,
        "created": datetime.now().isoformat(timespec="seconds"),
        "host": os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or "sconosciuto",
        "platform": sys.platform,
        "home": str(home_dir()),
        "claude_version": claude_version(),
        "includes": sorted(include),
        "plugins": plugins,
        "marketplaces": markets,
        "settings": {k: settings[k] for k in SETTINGS_KEYS if isinstance(settings.get(k), dict)}
        if "plugins" in include and isinstance(settings, dict) else {},
        "counts": {c: sum(1 for _p, a, _n in files if a.startswith(f"user/{d}/"))
                   for c, d in (("skills", "skills"), ("agents", "agents"))},
        "masked": masked,
        **manifest_user,
    }

    total = sum(n for _p, _a, n in files) or 1
    name = f"claude-plugins-backup-{datetime.now():%Y%m%d_%H%M%S}.zip"
    out = dest / name
    k = 1
    while out.exists():
        k += 1
        out = dest / f"claude-plugins-backup-{datetime.now():%Y%m%d_%H%M%S}-{k}.zip"
    part = out.with_name(out.name + ".part")

    done = written = skipped = 0
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
            for arc, data in extra.items():
                zf.writestr(arc, data)
            for src, arc in root_files:
                zf.write(src, arc)
            ctx.log(f"Compressione di {len(files)} file ({total / 1048576:.1f} MB)…")
            for i, (src, arc, n_bytes) in enumerate(files):
                ctx.check()
                try:
                    zf.write(src, arc)
                    written += 1
                except OSError as exc:
                    skipped += 1
                    ctx.log(f"Saltato (in uso o illeggibile): {src.name} — {exc.strerror or exc}")
                done += n_bytes
                if i % 40 == 0 or i == len(files) - 1:
                    ctx.progress(done / total, f"Compressione… {i + 1} di {len(files)} file")
        os.replace(part, out)
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise
    size = out.stat().st_size
    if masked:
        ctx.log(f"Valori oscurati (da reinserire sul nuovo PC): {', '.join(sorted({m['name'] for m in masked}))}")
    ctx.log(f"Backup creato: {out}  ({size / 1048576:.1f} MB)")
    return BackupResult(out, written + len(extra) + len(root_files), skipped, size, sorted(include))


# --------------------------------------------------------------------------- #
# Lettura di un backup
# --------------------------------------------------------------------------- #
def read_manifest(path: Path) -> Optional[dict]:
    """Manifest del backup, oppure None se il file non è un backup valido."""
    try:
        with zipfile.ZipFile(path) as zf:
            m = json.loads(zf.read("manifest.json"))
    except Exception:
        return None
    if not isinstance(m, dict):
        return None
    m["_legacy_credentials"] = bool(m.get("credentials_redacted"))
    m["_categories"] = manifest_categories(m)
    return m


def manifest_categories(m: dict) -> list[str]:
    """Categorie presenti in un backup (i backup 1.x e 2.x contengono solo i plugin)."""
    inc = m.get("includes")
    if isinstance(inc, list):
        return [c for c in CATEGORIES if c in inc]
    return ["plugins"]


def list_backups(folder: str | Path) -> list[Path]:
    d = Path(folder)
    if not d.is_dir():
        return []
    return sorted(d.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)


def scrub_zip(path: Path) -> None:
    """Toglie da un vecchio backup le credenziali e le impostazioni non necessarie."""
    part = path.with_name(path.name + ".part")
    try:
        with zipfile.ZipFile(path) as zin, zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                if info.filename == "manifest.json":
                    m = json.loads(zin.read(info))
                    m.pop("credentials_redacted", None)
                    st = m.get("settings") or {}
                    m["settings"] = {k: st[k] for k in SETTINGS_KEYS if isinstance(st.get(k), dict)}
                    zout.writestr("manifest.json", json.dumps(m, indent=2, ensure_ascii=False))
                else:
                    new = zipfile.ZipInfo(info.filename, info.date_time)
                    new.compress_type = zipfile.ZIP_DEFLATED
                    new.external_attr = info.external_attr
                    with zin.open(info) as s, zout.open(new, "w") as d:
                        shutil.copyfileobj(s, d, 1 << 20)
        os.replace(part, path)
    except BaseException:
        try:
            part.unlink()
        except OSError:
            pass
        raise


def _count_commands(node: Any) -> int:
    """Quante voci con una chiave 'command' (cioè comandi eseguibili) contiene una struttura di hook."""
    if isinstance(node, dict):
        return (1 if isinstance(node.get("command"), str) else 0) + sum(_count_commands(v) for v in node.values())
    if isinstance(node, list):
        return sum(_count_commands(v) for v in node)
    return 0


def preview_restore(path: Path) -> dict:
    """Ciò che il ripristino farebbe DAVVERO leggendo i file dello zip (non solo il manifest): serve a mostrare
    all'utente i comandi che Claude Code eseguirà e a notare un file manomesso."""
    out = {"plugin_ids": [], "invalid_ids": [], "marketplaces": [], "hooks": 0, "status_line": False,
           "api_key_helper": False, "env_keys": [], "mcp_commands": [], "mismatch": False}
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            manifest = json.loads(zf.read("manifest.json"))
            ip = json.loads(zf.read("root/installed_plugins.json")) if "root/installed_plugins.json" in names else {}
            for pid in (ip.get("plugins") or {}) if isinstance(ip, dict) and isinstance(ip.get("plugins"), dict) else []:
                (out["plugin_ids"] if _PLUGIN_ID_RE.fullmatch(str(pid)) else out["invalid_ids"]).append(str(pid))
            km = json.loads(zf.read("root/known_marketplaces.json")) if "root/known_marketplaces.json" in names else {}
            for name, info in (km.items() if isinstance(km, dict) else []):
                src = (info or {}).get("source") or {} if isinstance(info, dict) else {}
                out["marketplaces"].append((str(name), str(src.get("repo") or src.get("url") or "")))
            declared = {p.get("id") for p in manifest.get("plugins", []) if isinstance(p, dict)}
            if declared and set(out["plugin_ids"]) - declared:
                out["mismatch"] = True
            if "user/settings.json" in names:
                st = json.loads(zf.read("user/settings.json"))
                hooks = st.get("hooks")
                if isinstance(hooks, dict):
                    out["hooks"] = _count_commands(hooks)
                out["status_line"] = bool(st.get("statusLine"))
                out["api_key_helper"] = bool(st.get("apiKeyHelper"))
                out["env_keys"] = sorted((st.get("env") or {}).keys()) if isinstance(st.get("env"), dict) else []
            if "user/mcp.json" in names:
                for name, cfg in (json.loads(zf.read("user/mcp.json")).get("servers") or {}).items():
                    if isinstance(cfg, dict):
                        cmd = " ".join([str(cfg.get("command") or cfg.get("url") or "")] + [str(a) for a in cfg.get("args", [])])
                        out["mcp_commands"].append((str(name), cmd.strip()))
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------- #
# Ripristino
# --------------------------------------------------------------------------- #
def _rewrite_path_string(value: str, target: Path) -> str:
    """Porta un percorso assoluto dentro ~/.claude/plugins sulla cartella di questo computer."""
    if not _ABS_RE.match(value):
        return value
    m = _CLAUDE_PLUGINS_RE.search(value)
    if not m:
        return value
    tail = value[m.end():].replace("\\", os.sep).replace("/", os.sep)
    return str(target) + os.sep + tail


def _rewrite_paths(data: Any, target: Path) -> Any:
    if isinstance(data, dict):
        return {k: _rewrite_paths(v, target) for k, v in data.items()}
    if isinstance(data, list):
        return [_rewrite_paths(v, target) for v in data]
    if isinstance(data, str):
        return _rewrite_path_string(data, target)
    return data


@dataclass
class RestoreResult:
    files: int = 0
    skipped: int = 0              # file in uso / non scrivibili
    invalid: int = 0              # nomi non validi nello zip (ignorati)
    unchanged: int = 0
    snapshot: str = ""
    categories: list = field(default_factory=list)
    mcp_added: list = field(default_factory=list)
    mcp_secrets: list = field(default_factory=list)      # ["alpaca: ALPACA_API_KEY", ...]
    online_state: str = ""        # "" (non richiesto) | "no_cli" | "not_logged_in" | "cli_error" | "done"
    online_message: str = ""
    online_items: list = field(default_factory=list)     # [(etichetta, esito, dettaglio)]
    online_ok: int = 0
    online_failed: int = 0
    remapped: list = field(default_factory=list)         # [(vecchio id, nuovo id)]


def _canonical_parts(name: str) -> Optional[list[str]]:
    """Segmenti di un nome nello zip, oppure None se il nome non è sicuro (.., assoluto, ':' , backslash)."""
    if not name or name.startswith("/") or "\\" in name or "\x00" in name:
        return None
    parts = name.split("/")
    if any(p in ("", ".", "..") or ":" in p for p in parts):
        return None
    return parts


def _member_target(parts: list[str]) -> Optional[tuple[str, Path, list[str]]]:
    """(categoria, cartella base, resto del percorso) per i file ripristinabili; None per tutto il resto."""
    if parts[0] == "plugins" and len(parts) >= 3 and parts[1] in DATA_SUBDIRS:
        return "plugins", plugins_dir() / parts[1], parts[2:]
    if parts[0] == "user":
        if len(parts) >= 3:
            for cat, dirs in USER_DIRS.items():
                if parts[1] in dirs:
                    return cat, claude_dir() / parts[1], parts[2:]
        elif len(parts) == 2:
            for cat, names in USER_FILES.items():
                if parts[1] in names:
                    return cat, claude_dir(), [parts[1]]
    return None


def _inside(base: Path, rel: list[str]) -> Optional[Path]:
    """Percorso base/rel, verificato a livello di testo: non esce da base. Le cartelle che sono collegamenti
    (es. una skill che punta a ~/.agents/skills) restano valide: scriviamo nella cartella collegata."""
    nb = os.path.normpath(str(base))
    t = os.path.normpath(os.path.join(nb, *rel))
    return Path(t) if t.startswith(nb + os.sep) else None


def _same_content(target: Path, info: zipfile.ZipInfo, zf: zipfile.ZipFile) -> bool:
    try:
        if target.stat().st_size != info.file_size:
            return False
        if info.file_size > 20 * 1048576:
            return True
        return target.read_bytes() == zf.read(info)
    except OSError:
        return False


def _as_dict(x: Any) -> dict:
    return x if isinstance(x, dict) else {}


def _merge_root(fname: str, old: dict, new: dict) -> dict:
    if fname == "installed_plugins.json":
        conv = {}
        for k, v in _as_dict(new.get("plugins")).items():
            conv[str(k)] = v if isinstance(v, list) else ([v] if isinstance(v, dict) else [])
        merged = dict(old)
        vers = [x for x in (old.get("version"), new.get("version")) if isinstance(x, int)]
        merged["version"] = max(vers + [2])
        merged["plugins"] = {**_as_dict(old.get("plugins")), **conv}
        return merged
    if fname == "plugin-directory-bindings.json":
        merged = dict(old)
        merged.update({k: v for k, v in new.items() if k != "bindings"})
        merged["bindings"] = {**_as_dict(old.get("bindings")), **_as_dict(new.get("bindings"))}
        return merged
    return {**old, **new}


def _merge_settings(current: dict, new: dict) -> dict:
    out = dict(current)
    for k, v in new.items():
        cur = out.get(k)
        if k in DICT_MERGE_KEYS and isinstance(cur, dict) and isinstance(v, dict):
            out[k] = {**cur, **v}
        elif k == "permissions" and isinstance(cur, dict) and isinstance(v, dict):
            merged = dict(cur)
            for pk, pv in v.items():
                if isinstance(pv, list) and isinstance(cur.get(pk), list):
                    merged[pk] = list(cur[pk]) + [x for x in pv if x not in cur[pk]]
                else:
                    merged[pk] = pv
            out[k] = merged
        else:
            out[k] = v
    return out


def _load_settings() -> Optional[dict]:
    """settings.json attuale: {} se manca, None se illeggibile (in quel caso non lo si tocca)."""
    sf = claude_dir() / "settings.json"
    if not sf.exists():
        return {}
    try:
        cur = json.loads(sf.read_text(encoding="utf-8-sig"))
        return cur if isinstance(cur, dict) else None
    except Exception:
        return None


# --- CLI di Claude -------------------------------------------------------- #
def _kill_tree(p: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True,
                           creationflags=_NO_WINDOW, timeout=15)
        p.kill()
    except Exception:
        pass


_CMD_META_RE = re.compile(r'[&|<>^%"!\r\n]')


def _cli(args: list[str], ctx: Ctx, timeout: float = 240) -> tuple[int, str, str]:
    """Esegue 'claude ...'. Ritorna (codice, stdout, stderr); -1 = tempo scaduto, -2 = non avviabile."""
    exe = find_claude()
    if not exe:
        return -2, "", "comando 'claude' non trovato"
    # Un .cmd/.bat passa da cmd.exe, che interpreta & | < > ^ % : rifiutiamo qualsiasi argomento che li contenga.
    if exe.lower().endswith((".cmd", ".bat")) and any(_CMD_META_RE.search(a) for a in args):
        return -2, "", "argomento non consentito"
    try:
        p = subprocess.Popen([exe] + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                             text=True, encoding="utf-8", errors="replace", creationflags=_NO_WINDOW)
    except OSError as exc:
        return -2, "", str(exc)
    waited = 0.0
    while True:
        try:
            out, err = p.communicate(timeout=0.4)
            return p.returncode, out or "", err or ""
        except subprocess.TimeoutExpired:
            waited += 0.4
            if ctx.cancel.is_set():
                _kill_tree(p)
                raise Cancelled()
            if waited >= timeout:
                _kill_tree(p)
                try:
                    out, err = p.communicate(timeout=5)
                except Exception:
                    out = err = ""
                return -1, out or "", err or ""


def _parse_json_loose(text: str) -> Any:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line[:1] in "[{":
            try:
                return json.loads(line)
            except Exception:
                continue
    return None


def _cli_detail(rc: int, out: str, err: str) -> str:
    if rc == -1:
        return "tempo scaduto"
    data = _parse_json_loose(out)
    if isinstance(data, dict):
        for k in ("error", "message"):
            if isinstance(data.get(k), str) and data[k]:
                return data[k]
    lines = [ln.strip() for ln in (err or out or "").splitlines() if ln.strip()]
    return lines[-1] if lines else f"codice {rc}"


def _not_logged_in(out: str, err: str) -> bool:
    t = (out + " " + err).lower()
    return "not logged in" in t or "please run /login" in t


_DIR_KEY_RE = re.compile(r"^github\.com/([^/]+/[^#/]+)(?:#(.+))?$")
OFFICIAL_REPO = "anthropics/claude-plugins-official"


def _directory_fallback(pid: str, bindings: dict) -> Optional[tuple[str, str]]:
    """Per gli id di 'anthropic-plugin-directory' (catalogo remoto, non un marketplace git): (repo GitHub,
    nome del plugin) ricavati dal file dei collegamenti. Solo valori con caratteri sicuri."""
    b = (_as_dict(bindings.get(pid))).get("repositoryKey", "")
    m = _DIR_KEY_RE.match(b) if isinstance(b, str) else None
    if not m:
        return None
    repo, path = m.group(1), m.group(2) or ""
    name = path.rsplit("/", 1)[-1] if path else pid.split("@", 1)[0]
    if not (_REPO_RE.fullmatch(repo) and _PLAIN_NAME_RE.fullmatch(name)):
        return None
    return repo, name


def _online_reinstall(ctx: Ctx, res: RestoreResult, wanted_enabled: dict) -> None:
    """Reinstalla marketplace e plugin con la CLI di Claude. Serve internet e l'accesso (/login)."""
    pd = plugins_dir()
    if not find_claude():
        res.online_state = "no_cli"
        res.online_message = "Il comando 'claude' non è stato trovato su questo computer."
        ctx.log(res.online_message + " Salto la reinstallazione online.")
        return

    ctx.progress(0.93, "Verifica dell'accesso a Claude…")
    rc, out, err = _cli(["plugin", "marketplace", "list", "--json"], ctx, timeout=90)
    if _not_logged_in(out, err):
        res.online_state = "not_logged_in"
        res.online_message = ("Claude Code non ha effettuato l'accesso su questo computer. "
                              "Apri Claude Code, esegui /login e poi ripeti il ripristino.")
        ctx.log("Reinstallazione online non eseguita: " + res.online_message)
        return
    if rc != 0:
        res.online_state = "cli_error"
        res.online_message = f"La CLI di Claude non risponde: {_cli_detail(rc, out, err)}"
        ctx.log(res.online_message)
        return

    listed = [m for m in (_parse_json_loose(out) or []) if isinstance(m, dict)]
    known = {m.get("name") for m in listed}
    by_repo = {str(m.get("repo")): m.get("name") for m in listed if m.get("repo")}
    res.online_state = "done"

    def add_item(label, status, detail="", ok=True):
        res.online_items.append((label, status, detail))
        ctx.log(f"  {label}: {status}" + (f" ({detail})" if detail else ""))
        if ok:
            res.online_ok += 1
        else:
            res.online_failed += 1

    def lost_login(out_, err_):
        if _not_logged_in(out_, err_):
            res.online_state = "not_logged_in"
            res.online_message = ("L'accesso a Claude è scaduto durante il ripristino. "
                                  "Esegui /login in Claude Code e ripeti il ripristino.")
            ctx.log(res.online_message)
            return True
        return False

    try:
        # 1. Marketplace
        ctx.log("Reinstallazione online: marketplace…")
        km = _read_json(pd / "known_marketplaces.json", {})
        for name, info in (km.items() if isinstance(km, dict) else []):
            ctx.check()
            src = _as_dict(_as_dict(info).get("source"))
            ref = src.get("repo") or src.get("url")
            label = f"marketplace {name}"
            if name in known:
                add_item(label, "già presente")
                continue
            if not isinstance(ref, str) or not _REPO_RE.fullmatch(ref):
                add_item(label, "saltato", "sorgente non valida o sconosciuta", ok=False)
                continue
            ctx.progress(0.94, f"Aggiunta del marketplace {name}…")
            rc, out, err = _cli(["plugin", "marketplace", "add", ref, "--scope", "user", "--json"], ctx)
            if rc == 0:
                known.add(name)
                by_repo[ref] = name
                add_item(label, "aggiunto")
            else:
                if lost_login(out, err):
                    return
                add_item(label, "non riuscito", _cli_detail(rc, out, err), ok=False)

        # 2. Plugin
        rc, out, err = _cli(["plugin", "list", "--json"], ctx, timeout=90)
        present = {p.get("id") for p in (_parse_json_loose(out) or [])
                   if isinstance(p, dict) and p.get("id") and not p.get("errors")}
        ip = _read_json(pd / "installed_plugins.json", {})
        ids = list(_as_dict(_as_dict(ip).get("plugins")))
        bindings = _as_dict(_read_json(pd / "plugin-directory-bindings.json", {}).get("bindings"))
        ctx.log(f"Reinstallazione online: {len(ids)} plugin…")
        for i, pid in enumerate(ids):
            ctx.check()
            ctx.progress(0.95 + 0.05 * i / max(1, len(ids)), f"Plugin {i + 1} di {len(ids)}: {pid}")
            if pid.endswith("@builtin"):
                add_item(pid, "incluso in Claude Code", "non serve installarlo")
                continue
            if not _PLUGIN_ID_RE.fullmatch(pid):
                add_item(pid[:60], "saltato", "id non valido", ok=False)
                continue
            if pid in present:
                add_item(pid, "già presente")
                continue
            rc, out, err = _cli(["plugin", "install", pid, "--scope", "user", "--json", "-y"], ctx)
            if rc == 0:
                add_item(pid, "installato")
                continue
            if lost_login(out, err):
                return
            detail = _cli_detail(rc, out, err)
            fb = _directory_fallback(pid, bindings) if pid.endswith("@anthropic-plugin-directory") else None
            if fb:
                repo, name = fb
                mkt = by_repo.get(repo)
                if not mkt:
                    r2, o2, e2 = _cli(["plugin", "marketplace", "add", repo, "--scope", "user", "--json"], ctx)
                    if r2 == 0:
                        mkt = "claude-plugins-official" if repo == OFFICIAL_REPO else None
                        if mkt:
                            known.add(mkt)
                            by_repo[repo] = mkt
                if mkt and _PLAIN_NAME_RE.fullmatch(str(mkt)):
                    new_id = f"{name}@{mkt}"
                    r3, o3, e3 = _cli(["plugin", "install", new_id, "--scope", "user", "--json", "-y"], ctx)
                    if r3 == 0:
                        res.remapped.append((pid, new_id))
                        add_item(pid, "installato come " + new_id, "il catalogo originale non è raggiungibile")
                        continue
                    detail = _cli_detail(r3, o3, e3)
            add_item(pid, "non riuscito", detail, ok=False)
    finally:
        # Anche se l'utente annulla: id sostituiti e stato attivo/disattivo devono restare coerenti.
        try:
            if res.remapped:
                ip = _read_json(pd / "installed_plugins.json", {})
                if isinstance(ip, dict) and isinstance(ip.get("plugins"), dict):
                    for old, _new in res.remapped:
                        ip["plugins"].pop(old, None)
                    _write_json_atomic(pd / "installed_plugins.json", ip)
            cur = _load_settings()
            if cur is not None and (res.remapped or wanted_enabled):
                ep = dict(cur.get("enabledPlugins", {})) if isinstance(cur.get("enabledPlugins"), dict) else {}
                for old, _new in res.remapped:
                    ep.pop(old, None)
                installed_now = set(_as_dict(_as_dict(_read_json(pd / "installed_plugins.json", {})).get("plugins")))
                for pid, val in wanted_enabled.items():
                    if pid in installed_now or pid in ep:
                        ep[pid] = val
                for old, new in res.remapped:
                    if old in wanted_enabled:
                        ep[new] = wanted_enabled[old]
                cur["enabledPlugins"] = ep
                _write_json_atomic(claude_dir() / "settings.json", cur)
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"Aggiornamento finale dei plugin non riuscito: {exc}")


def restore_backup(path: Path, ctx: Ctx, online: bool = False, categories: Optional[set] = None) -> RestoreResult:
    manifest = read_manifest(path)
    if manifest is None:
        raise BackupError("Questo file non è un backup valido (manca manifest.json o lo zip è danneggiato).")
    available = set(manifest["_categories"])
    cats = available if categories is None else (set(categories) & available)
    if not cats:
        raise BackupError("Nessuna categoria selezionata da ripristinare.")
    cd, pd = claude_dir(), plugins_dir()
    cd.mkdir(parents=True, exist_ok=True)
    res = RestoreResult(categories=sorted(cats))
    old_home = str(manifest.get("home") or "")
    new_home = str(home_dir())
    wanted_enabled: dict = {}

    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        members = []  # (info, categoria, destinazione, nome relativo)
        for info in zf.infolist():
            if info.is_dir():
                continue
            parts = _canonical_parts(info.filename)
            if parts is None:
                if info.filename.split("/")[0] in ("plugins", "user", "root"):
                    res.invalid += 1
                    ctx.log(f"Ignorato (nome non valido nel backup): {info.filename[:120]}")
                continue
            mt = _member_target(parts)
            if mt is None or mt[0] not in cats:
                continue
            cat, base, rel = mt
            target = _inside(base, rel)
            if target is None:
                res.invalid += 1
                ctx.log(f"Ignorato (percorso non valido nel backup): {info.filename[:120]}")
                continue
            members.append((info, cat, target, "/".join(parts[1:]) if parts[0] == "user" else "plugins/" + "/".join(parts[1:])))
        total = sum(m[0].file_size for m in members) or 1

        # Copia di sicurezza: metadati che stanno per cambiare e file che verranno sostituiti.
        ctx.progress(None, "Copia di sicurezza dello stato attuale…")
        snap = cd / "plugin-backup-prima-del-ripristino" / datetime.now().strftime("%Y%m%d_%H%M%S")
        try:
            snap.mkdir(parents=True, exist_ok=True)
            to_save = []
            if "plugins" in cats:
                to_save += [pd / n for n in ROOT_METADATA_FILES]
            if cats & {"plugins", "settings"}:
                to_save.append(cd / "settings.json")
            if "mcp" in cats:
                to_save.append(claude_json_path())
            for src in to_save:
                if src.is_file():
                    shutil.copy2(src, snap / src.name)
            res.snapshot = str(snap)
            ctx.log(f"Copia di sicurezza di ciò che verrà modificato in: {snap}")
        except OSError as exc:
            ctx.log(f"Copia di sicurezza non riuscita ({exc}): continuo comunque.")
        ctx.check()

        # 1. File (plugin e contenuti personali): si uniscono a quelli presenti.
        ctx.log(f"Ripristino di {len(members)} file…")
        done = 0
        for i, (info, cat, target, relname) in enumerate(members):
            ctx.check()
            try:
                if target.exists():
                    if cat != "plugins" and _same_content(target, info, zf):
                        res.unchanged += 1
                        done += info.file_size
                        continue
                    if cat != "plugins" or relname.startswith("plugins/data/"):  # salva ciò che viene sostituito
                        keep = snap / "file-sostituiti" / relname
                        keep.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(target, keep)
                    os.chmod(target, 0o666)
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as s, open(target, "wb") as d:
                    shutil.copyfileobj(s, d, 1 << 20)
                res.files += 1
            except OSError as exc:
                res.skipped += 1
                ctx.log(f"Saltato (in uso?): {target.name} — {exc.strerror or exc}")
            done += info.file_size
            if i % 40 == 0 or i == len(members) - 1:
                ctx.progress(done / total * 0.85, f"Ripristino… {i + 1} di {len(members)} file")

        # 2. Metadati dei plugin (per ultimi: un annullamento prima di qui non altera l'elenco).
        ctx.check()
        ctx.progress(0.88, "Aggiornamento dell'elenco dei plugin…")
        if "plugins" in cats:
            for fname in ROOT_METADATA_FILES:
                arc = f"root/{fname}"
                if arc not in names:
                    continue
                try:
                    new = _rewrite_paths(json.loads(zf.read(arc).decode("utf-8-sig")), pd)
                    dst = pd / fname
                    if fname in MERGED_FILES and isinstance(new, dict):
                        old = _read_json(dst, {})
                        new = _merge_root(fname, old if isinstance(old, dict) else {}, new)
                    _write_json_atomic(dst, new)
                    ctx.log(f"Aggiornato {fname}")
                except Exception as exc:  # noqa: BLE001
                    ctx.log(f"Metadati non ripristinati ({fname}): {exc}")

        # 3. settings.json
        try:
            cur = _load_settings()
            if cur is None:
                ctx.log("settings.json non è leggibile: non lo modifico (i plugin restano da attivare a mano).")
            else:
                changed = False
                if "plugins" in cats:
                    bs = _as_dict(manifest.get("settings"))
                    ep = bs.get("enabledPlugins")
                    wanted_enabled = dict(ep) if isinstance(ep, dict) else {}
                    sub = {k: bs[k] for k in SETTINGS_KEYS if isinstance(bs.get(k), dict)}
                    if sub:
                        cur = _merge_settings(cur, _rewrite_home(sub, old_home, new_home))
                        changed = True
                        ctx.log("Stato attivo/disattivo dei plugin ripristinato.")
                if "settings" in cats and "user/settings.json" in names:
                    new = json.loads(zf.read("user/settings.json").decode("utf-8-sig"))
                    if isinstance(new, dict):
                        cur = _merge_settings(cur, _rewrite_home(new, old_home, new_home))
                        changed = True
                        ctx.log("Impostazioni di Claude ripristinate (unite a quelle presenti).")
                if changed:
                    _write_json_atomic(cd / "settings.json", cur)
        except Exception as exc:  # noqa: BLE001
            ctx.log(f"Impostazioni non ripristinate: {exc}")

        # 4. Server MCP globali: si aggiungono solo quelli che mancano.
        if "mcp" in cats and "user/mcp.json" in names:
            try:
                servers = _as_dict(json.loads(zf.read("user/mcp.json").decode("utf-8-sig")).get("servers"))
                servers = _rewrite_home(servers, old_home, new_home)
                cj = claude_json_path()
                data = {} if not cj.exists() else _read_json(cj, None)
                if not isinstance(data, dict):
                    ctx.log(f"{cj.name} non è leggibile: non lo modifico, i server MCP non sono stati ripristinati.")
                else:
                    have = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else {}
                    masked = [m for m in manifest.get("masked", []) if isinstance(m, dict)]
                    for name, cfg in servers.items():
                        if name in have:
                            ctx.log(f"Server MCP {name}: già presente, non lo modifico.")
                            continue
                        env = cfg.get("env") if isinstance(cfg, dict) else None
                        if isinstance(env, dict):  # file di certificati o simili che non esistono qui: toglili
                            for k in list(env):
                                v = env[k]
                                if (isinstance(v, str) and _ABS_RE.match(v) and
                                        re.search(r"(cert|bundle|ca_?file|_file|_path|_dir)", k, re.I)
                                        and (v.startswith(("\\\\", "//")) or not Path(v).exists())):
                                    ctx.log(f"Server MCP {name}: tolgo {k} (il file non esiste su questo PC).")
                                    env.pop(k)
                        have[name] = cfg
                        res.mcp_added.append(name)
                        for m in masked:
                            if m.get("server") == name:
                                res.mcp_secrets.append(f"{name}: {m.get('name')}")
                    if res.mcp_added:
                        data["mcpServers"] = have
                        _write_json_atomic(cj, data)
                        ctx.log(f"Server MCP aggiunti: {', '.join(res.mcp_added)}")
            except Exception as exc:  # noqa: BLE001
                ctx.log(f"Server MCP non ripristinati: {exc}")

    # 5. Reinstallazione tramite la CLI di Claude (serve internet e l'accesso).
    if online and "plugins" in cats:
        _online_reinstall(ctx, res, wanted_enabled)
    ctx.progress(1.0, "Completato")
    ctx.log("Ripristino completato. Riavvia Claude Code.")
    return res
