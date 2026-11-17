"""Test del motore di Plugin Backup (profilo utente isolato, nessun accesso al profilo reale).

    python -m unittest discover -s tests -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import engine  # noqa: E402


class IsolatedProfile(unittest.TestCase):
    """Ogni test gira con USERPROFILE/HOME/APPDATA su una cartella temporanea e PATH ripulito."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.home = self.root / "Users" / "tester"
        self.cd = self.home / ".claude"
        self.cd.mkdir(parents=True)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self._env = dict(os.environ)
        os.environ.update(USERPROFILE=str(self.home), HOME=str(self.home), APPDATA=str(self.home / "AppData"),
                          PATH=str(self.bin) + os.pathsep + r"C:\Windows\System32;C:\Windows")
        os.environ.pop("CLAUDE_CONFIG_DIR", None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    # --- utilità
    def make_zip(self, entries, manifest=None):
        z = self.root / "b.zip"
        m = {"version": 3, "includes": ["plugins", "skills", "settings", "mcp"], "plugins": [], "home": "C:\\Users\\vecchio"}
        m.update(manifest or {})
        with zipfile.ZipFile(z, "w") as zf:
            zf.writestr("manifest.json", json.dumps(m))
            for name, data in entries.items():
                zf.writestr(name, data)
        return z

    def fake_claude(self, extra=""):
        """claude.cmd che registra gli argomenti ricevuti e risponde come un CLI loggato."""
        py = self.bin / "fake.py"
        py.write_text("import sys, json\n"
                      f"open(r'{self.root / 'calls.log'}', 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                      "a = sys.argv[1:]\n" + extra +
                      "print(json.dumps([] if a[-2:-1] != ['--json'] and 'list' in a else {'ok': True}))\n")
        (self.bin / "claude.cmd").write_text("@echo off\r\n" + f'"{sys.executable}" "{py}" %*' + "\r\n")

    def calls(self):
        p = self.root / "calls.log"
        return p.read_text().splitlines() if p.exists() else []


class TestMask(unittest.TestCase):
    def test_args_url_and_userinfo(self):
        cfg = {"command": "uvx", "args": ["--api-key=XYZ", "--token", "ABC", "--ok", "v", "x"],
               "url": "https://h/x?token=SEGRETO&a=1", "u2": "https://utente:password@host/p",
               "env": {"MY_TOKEN": "t", "PAPER": "true", "pwd": "p", "Cookie": "c"}}
        clean, m = engine.mask_secrets(cfg, "mcp.s", server="s")
        blob = json.dumps(clean)
        for s in ("XYZ", "ABC", "SEGRETO", "password", '"t"', '"p"', '"c"'):
            self.assertNotIn(s, blob)
        self.assertIn("true", blob)
        self.assertIn("a=1", blob)
        self.assertTrue(all(x["server"] == "s" for x in m))

    def test_hooks_not_wiped(self):
        st = {"hooks": {"SessionStart": [{"hooks": [{"command": "echo"}]}]}, "model": "x"}
        clean, m = engine.mask_secrets(st, "settings")
        self.assertEqual(clean, st)
        self.assertEqual(m, [])


class TestBackupRestore(IsolatedProfile):
    def populate(self):
        (self.cd / "skills" / "mia").mkdir(parents=True)
        (self.cd / "skills" / "mia" / "SKILL.md").write_text("ciao")
        ext = self.root / "esterna"
        ext.mkdir()
        (ext / "SKILL.md").write_text("linkata")
        subprocess.run(["cmd", "/c", "mklink", "/J", str(self.cd / "skills" / "linkata"), str(ext)],
                       capture_output=True, check=True)
        (self.cd / "agents").mkdir()
        (self.cd / "agents" / "a.md").write_text("agente")
        (self.cd / ".credentials.json").write_text('{"claudeAiOauth": {"accessToken": "sk-ant-SEGRETO"}}')
        (self.cd / "settings.json").write_text(json.dumps({"theme": "dark", "env": {"API_KEY": "SEGRETO2"}}))
        (self.home / ".claude.json").write_text(json.dumps({"mcpServers": {
            "my.server": {"command": "uvx", "args": ["--api-key=SEGRETO3"], "env": {"TOKEN": "SEGRETO4", "X": "1"}}}}))

    def test_roundtrip_without_secrets(self):
        self.populate()
        r = engine.create_backup(self.root / "bk", engine.Ctx())
        with zipfile.ZipFile(r.path) as z:
            names = z.namelist()
            blob = b"".join(z.read(n) for n in names if n.startswith(("user/settings", "user/mcp", "manifest")))
            self.assertIn("user/skills/linkata/SKILL.md", names)          # il collegamento diventa cartella vera
            self.assertNotIn(".credentials.json", " ".join(names))
        for s in (b"SEGRETO", b"sk-ant"):
            self.assertNotIn(s, blob)
        # secondo profilo
        os.environ["USERPROFILE"] = os.environ["HOME"] = str(self.root / "Users" / "altro")
        (self.root / "Users" / "altro" / ".claude").mkdir(parents=True)
        res = engine.restore_backup(r.path, engine.Ctx())
        cd2 = self.root / "Users" / "altro" / ".claude"
        self.assertEqual((cd2 / "skills" / "linkata" / "SKILL.md").read_text(), "linkata")
        self.assertEqual((cd2 / "agents" / "a.md").read_text(), "agente")
        self.assertIn("my.server: TOKEN", res.mcp_secrets)                 # nome con il punto gestito
        self.assertFalse((cd2 / ".credentials.json").exists())

    def test_hostile_names_are_ignored(self):
        (self.cd / "settings.json").write_text('{"deny": "originale"}')
        (self.cd / ".credentials.json").write_text("CRED")
        z = self.make_zip({
            "user/skills/../settings.json": "{}", "user/skills/../.credentials.json": "ATTACCO",
            "plugins/cache/../blocklist.json": "x", "user/skills//x": "x", "/abs.txt": "x",
            "user/skills/C:evil": "x", "user\\skills\\y": "x", "user/skills/ok/SKILL.md": "buono"})
        res = engine.restore_backup(z, engine.Ctx(), categories={"skills", "plugins"})
        self.assertEqual((self.cd / "settings.json").read_text(), '{"deny": "originale"}')
        self.assertEqual((self.cd / ".credentials.json").read_text(), "CRED")
        self.assertFalse((self.cd / "plugins" / "blocklist.json").exists())
        self.assertEqual((self.cd / "skills" / "ok" / "SKILL.md").read_text(), "buono")
        self.assertGreaterEqual(res.invalid, 5)

    def test_write_through_existing_junction(self):
        ext = self.root / "esterna"
        ext.mkdir()
        (self.cd / "skills").mkdir()
        subprocess.run(["cmd", "/c", "mklink", "/J", str(self.cd / "skills" / "k"), str(ext)], capture_output=True, check=True)
        res = engine.restore_backup(self.make_zip({"user/skills/k/SKILL.md": "dentro"}), engine.Ctx(), categories={"skills"})
        self.assertEqual((ext / "SKILL.md").read_text(), "dentro")
        self.assertEqual((res.files, res.skipped, res.invalid), (1, 0, 0))

    def test_cyclic_junction_does_not_loop_or_duplicate(self):
        (self.cd / "agents").mkdir()
        (self.cd / "agents" / "a.md").write_text("a")
        subprocess.run(["cmd", "/c", "mklink", "/J", str(self.cd / "agents" / "up"), str(self.cd)], capture_output=True, check=True)
        files = engine.user_files("agents")
        self.assertEqual([a for _p, a, _n in files], ["user/agents/a.md"])

    def test_v1_installed_plugins_merge(self):
        pd = self.cd / "plugins"
        pd.mkdir()
        (pd / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {"a@m": [{"installPath": "x"}]}}))
        z = self.make_zip({"root/installed_plugins.json": json.dumps({"version": 1, "plugins": {"b@m": {"installPath": "y"}}}),
                           "plugins/cache/m/b/1/f.txt": "f"}, {"includes": None})
        engine.restore_backup(z, engine.Ctx())
        ip = json.loads((pd / "installed_plugins.json").read_text())
        self.assertEqual(ip["version"], 2)
        self.assertEqual(sorted(ip["plugins"]), ["a@m", "b@m"])
        self.assertIsInstance(ip["plugins"]["b@m"], list)

    def test_unc_env_is_dropped_without_touching_network(self):
        z = self.make_zip({"user/mcp.json": json.dumps({"servers": {"s": {"command": "x", "env": {
            "SSL_CERT_FILE": "\\\\attaccante\\share\\ca.pem", "KEEP": "1"}}}})})
        engine.restore_backup(z, engine.Ctx(), categories={"mcp"})
        env = json.loads((self.home / ".claude.json").read_text())["mcpServers"]["s"]["env"]
        self.assertEqual(env, {"KEEP": "1"})

    def test_home_rewrite_boundary(self):
        out = engine._rewrite_home({"a": r"C:\Users\vecchio\x", "b": r"C:\Users\vecchiox\y", "c": "C:/Users/vecchio/z"},
                                   r"C:\Users\vecchio", r"D:\Nuovo")
        self.assertEqual(out["a"], r"D:\Nuovo\x")
        self.assertEqual(out["b"], r"C:\Users\vecchiox\y")
        self.assertEqual(out["c"], "D:/Nuovo/z")
        self.assertEqual(engine._rewrite_home({"a": "/x"}, "/", "D:\\n"), {"a": "/x"})   # home troppo corta: ignorata


class TestOnline(IsolatedProfile):
    def write_registry(self, ids, repos):
        pd = self.cd / "plugins"
        pd.mkdir(parents=True)
        (pd / "installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {i: [{}] for i in ids}}))
        (pd / "known_marketplaces.json").write_text(json.dumps({n: {"source": {"repo": r}} for n, r in repos.items()}))

    def test_injection_is_blocked(self):
        self.fake_claude()
        pwned = self.root / "pwned.txt"
        z = self.make_zip({
            "root/installed_plugins.json": json.dumps({"version": 2, "plugins": {
                f"x@y&whoami>{pwned}&rem": [{}], "--help@z": [{}], "ok@mk": [{}]}}),
            "root/known_marketplaces.json": json.dumps({"m": {"source": {"repo": f"a/b&echo>{pwned}&rem"}}})})
        res = engine.restore_backup(z, engine.Ctx(), online=True, categories={"plugins"})
        self.assertFalse(pwned.exists())
        calls = " ".join(self.calls())
        self.assertNotIn("whoami", calls)
        self.assertNotIn("--help@z", calls)
        self.assertIn("ok@mk", calls)
        self.assertGreaterEqual(res.online_failed, 3)

    def test_not_logged_in_is_reported(self):
        self.fake_claude("print('Not logged in - Please run /login', file=sys.stderr); sys.exit(1)\n")
        z = self.make_zip({"root/installed_plugins.json": json.dumps({"version": 2, "plugins": {"a@m": [{}]}})})
        res = engine.restore_backup(z, engine.Ctx(), online=True, categories={"plugins"})
        self.assertEqual(res.online_state, "not_logged_in")
        self.assertEqual(self.calls(), ["plugin marketplace list --json"])


class TestPreview(IsolatedProfile):
    def test_preview_shows_real_commands_and_mismatch(self):
        z = self.make_zip({
            "user/settings.json": json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "calc"}]}]},
                                              "statusLine": {"command": "x"}}),
            "user/mcp.json": json.dumps({"servers": {"s": {"command": "uvx", "args": ["pkg"]}}}),
            "root/installed_plugins.json": json.dumps({"plugins": {"vero@m": [{}], "di-nascosto@m": [{}], "-x@y": [{}]}})},
            {"plugins": [{"id": "vero@m"}]})
        pv = engine.preview_restore(z)
        self.assertEqual(pv["hooks"], 1)
        self.assertTrue(pv["status_line"])
        self.assertEqual(pv["mcp_commands"], [("s", "uvx pkg")])
        self.assertTrue(pv["mismatch"])
        self.assertIn("-x@y", pv["invalid_ids"])


if __name__ == "__main__":
    unittest.main()
