"""desktop.py: the window launcher. The window itself needs a screen, so these cover
the parts that decide what runs: the port, the server command line, the --serve
switch, and stopping the server however the window ends."""

import socket
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, patch

import desktop


class PortTests(unittest.TestCase):
    def test_uses_8501_when_free(self):
        with patch("socket.socket") as sock_cls:
            sock = sock_cls.return_value.__enter__.return_value
            sock.getsockname.return_value = ("127.0.0.1", 8501)
            self.assertEqual(desktop.free_port(), 8501)

    def test_moves_to_another_port_when_8501_is_taken(self):
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            taken = busy.getsockname()[1]
            port = desktop.free_port(preferred=taken)
        self.assertNotEqual(port, taken)
        self.assertGreater(port, 0)


class ServerCommandTests(unittest.TestCase):
    def test_server_only_listens_locally_and_does_not_watch_files(self):
        args = desktop.streamlit_args(8600)
        self.assertEqual(args[:2], ["streamlit", "run"])
        self.assertTrue(args[2].endswith("app.py"))
        settings = dict(zip(args[3::2], args[4::2]))
        self.assertEqual(settings["--server.address"], "127.0.0.1")
        self.assertEqual(settings["--server.port"], "8600")
        self.assertEqual(settings["--server.headless"], "true")
        self.assertEqual(settings["--server.fileWatcherType"], "none")

    def test_from_source_the_server_is_this_file_with_serve(self):
        with patch.object(desktop, "FROZEN", False), patch("subprocess.Popen") as popen:
            desktop.start_server(8600)
        command = popen.call_args.args[0]
        self.assertEqual(command[0], sys.executable)
        self.assertTrue(command[1].endswith("desktop.py"))
        self.assertEqual(command[2:], ["--serve", "8600"])
        # Its stdin is a pipe the parent holds, so it can exit if the parent dies.
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.PIPE)

    def test_packaged_build_runs_itself_with_serve(self):
        with patch.object(desktop, "FROZEN", True), patch("subprocess.Popen") as popen:
            desktop.start_server(8600)
        self.assertEqual(popen.call_args.args[0], [sys.executable, "--serve", "8600"])

    def test_serve_switch_runs_the_server_not_the_window(self):
        with patch.object(desktop, "serve") as serve, \
                patch.object(desktop, "open_window") as window:
            desktop.main(["--serve", "8600"])
        serve.assert_called_once_with(8600)
        window.assert_not_called()


class LifecycleTests(unittest.TestCase):
    def fake_server(self):
        server = MagicMock()
        server.poll.return_value = None
        return server

    def test_closing_the_window_stops_the_server(self):
        server = self.fake_server()
        with patch.object(desktop, "start_server", return_value=server), \
                patch.object(desktop, "wait_until_ready"), \
                patch.object(desktop, "open_window", return_value=True), \
                patch.object(desktop, "log"):
            desktop.main([])
        server.terminate.assert_called_once()

    def test_no_window_falls_back_to_the_browser(self):
        server = self.fake_server()
        with patch.object(desktop, "start_server", return_value=server), \
                patch.object(desktop, "wait_until_ready"), \
                patch.object(desktop, "open_window", return_value=False), \
                patch.object(desktop, "run_in_browser") as browser, \
                patch.object(desktop, "log"):
            desktop.main([])
        browser.assert_called_once()
        server.terminate.assert_called_once()

    def test_server_that_fails_to_start_is_reported_and_stopped(self):
        server = self.fake_server()
        with patch.object(desktop, "start_server", return_value=server), \
                patch.object(desktop, "wait_until_ready", side_effect=RuntimeError("x")), \
                patch.object(desktop, "open_window") as window, \
                patch.object(desktop, "log"):
            with self.assertRaises(RuntimeError):
                desktop.main([])
        window.assert_not_called()
        server.terminate.assert_called_once()

    def test_server_that_exits_early_is_reported(self):
        server = MagicMock()
        server.poll.return_value = 1
        server.returncode = 1
        with self.assertRaisesRegex(RuntimeError, "stopped while starting"):
            desktop.wait_until_ready("http://127.0.0.1:1", server)


class PackagedAppTests(unittest.TestCase):
    def make_folder(self, root):
        app_src = root / "_internal" / "app_src"
        app_src.mkdir(parents=True)
        (app_src / "app.py").write_text("")
        return app_src

    def test_mac_app_finds_app_src_next_to_the_app(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_src = self.make_folder(root)
            exe = root / "FOT App.app" / "Contents" / "MacOS" / "FOT App"
            exe.parent.mkdir(parents=True)
            exe.write_text("")
            with patch.object(desktop, "FROZEN", True), \
                    patch.object(desktop.sys, "executable", str(exe)):
                self.assertEqual(desktop._find_app_dir(), app_src.resolve())

    def test_windows_and_linux_find_app_src_next_to_the_program(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_src = self.make_folder(root)
            exe = root / "FOT App.exe"
            exe.write_text("")
            with patch.object(desktop, "FROZEN", True), \
                    patch.object(desktop.sys, "executable", str(exe)):
                self.assertEqual(desktop._find_app_dir(), app_src.resolve())

    def test_shortcut_is_made_once_and_never_from_source(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp)
            with patch.object(desktop, "APP_DIR", app_dir), \
                    patch.object(desktop, "FROZEN", False), \
                    patch("subprocess.run") as run:
                desktop.ensure_desktop_shortcut()
            run.assert_not_called()
            (app_dir / "data").mkdir()
            (app_dir / "data" / desktop.SHORTCUT_MARKER).write_text("done")
            with patch.object(desktop, "APP_DIR", app_dir), \
                    patch.object(desktop, "FROZEN", True), \
                    patch.object(desktop.sys, "platform", "win32"), \
                    patch("subprocess.run") as run:
                desktop.ensure_desktop_shortcut()
            run.assert_not_called()


class BackNavigationTests(unittest.TestCase):
    def test_blocks_every_back_and_forward_shortcut(self):
        js = desktop.BLOCK_HISTORY_NAVIGATION_JS
        for shortcut in ("ArrowLeft", "BrowserBack", "Backspace", '"["', "e.button === 3"):
            self.assertIn(shortcut, js)
        # Backspace must still delete text in the patient form.
        self.assertIn("isTyping", js)


if __name__ == "__main__":
    unittest.main()
