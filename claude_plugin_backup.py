#!/usr/bin/env python3
"""Plugin Backup per Claude Code - riga di comando.

    python claude_plugin_backup.py                 apre l'interfaccia grafica
    python claude_plugin_backup.py backup [--dir CARTELLA]
    python claude_plugin_backup.py restore [FILE.zip] [--dry-run] [--online]
    python claude_plugin_backup.py list [--dir CARTELLA]
"""
import argparse
import sys
from pathlib import Path

import engine
from common import default_backup_dir, human


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] == "gui" or (argv[0].lower().endswith(".zip") and Path(argv[0]).exists()):
        from ui import main as gui
        return gui(["claude_plugin_backup"] + [a for a in argv if a != "gui"])

    ap = argparse.ArgumentParser(description="Backup e ripristino dei plugin di Claude Code")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup", help="crea un backup")
    b.add_argument("--dir", default=default_backup_dir(), help="cartella di destinazione")
    r = sub.add_parser("restore", help="ripristina un backup")
    r.add_argument("file", nargs="?", help="file .zip (default: il più recente)")
    r.add_argument("--dir", default=default_backup_dir())
    r.add_argument("--dry-run", action="store_true", help="mostra il contenuto senza modificare nulla")
    r.add_argument("--online", action="store_true", help="reinstalla anche con la CLI di Claude")
    ls = sub.add_parser("list", help="elenca i backup")
    ls.add_argument("--dir", default=default_backup_dir())
    args = ap.parse_args(argv)

    ctx = engine.Ctx(log=print)
    try:
        if args.cmd == "backup":
            res = engine.create_backup(args.dir, ctx)
            print(f"Fatto: {res.path} ({human(res.size)})")
        elif args.cmd == "list":
            items = engine.list_backups(args.dir)
            if not items:
                print("Nessun backup trovato in", args.dir)
            for p in items:
                m = engine.read_manifest(p)
                info = f"{len(m.get('plugins', []))} plugin" if m else "non valido"
                print(f"{p.name}  {human(p.stat().st_size)}  {info}")
        else:
            path = Path(args.file) if args.file else next(iter(engine.list_backups(args.dir)), None)
            if path is None or not path.exists():
                print("Backup non trovato.")
                return 2
            m = engine.read_manifest(path)
            if m is None:
                print("Il file non è un backup valido.")
                return 2
            if args.dry_run:
                print(f"{path.name}: creato {m.get('created')} su {m.get('host')}, "
                      f"{len(m.get('plugins', []))} plugin, {len(m.get('marketplaces', []))} marketplace. "
                      "Nessuna modifica eseguita.")
                return 0
            res = engine.restore_backup(path, ctx, args.online)
            print(f"Fatto: {res.files} file ripristinati, {res.skipped} saltati.")
    except engine.Cancelled:
        print("Annullato.")
        return 1
    except engine.BackupError as exc:
        print("Errore:", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
