import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location("installer", Path(__file__).parents[1] / "scripts/install-retropie.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def run_install(self, home):
        original = Path.is_file
        def existing_font(path):
            return True if str(path) == "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf" else original(path)
        with patch.object(Path, "home", return_value=home), patch.object(Path, "is_file", existing_font):
            installer.main()

    def test_installs_idempotently_without_editing_existing_games(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            roms = home / "RetroPie/roms"
            roms.mkdir(parents=True)
            game = roms / "existing-game.txt"
            game.write_text("untouched")
            self.run_install(home)
            current = home / ".local/share/vm-arcade/current"
            first = current.resolve()
            self.assertTrue((first / "vm_arcade/__main__.py").is_file())
            rollback = home / ".local/share/vm-arcade/previous-release.txt"
            rollback.write_text("/retained/previous-release\n")
            self.run_install(home)
            self.assertEqual(current.resolve(), first)
            self.assertEqual(rollback.read_text(), "/retained/previous-release\n")
            self.assertEqual(game.read_text(), "untouched")
            launcher = home / "RetroPie/roms/ports/VM Arcade.sh"
            self.assertTrue(launcher.stat().st_mode & 0o100)
            # The deployed cabinet has no Ports system. Its existing RetroPie
            # menu must still offer the dashboard without changing system XML.
            menu_launcher = home / "RetroPie/retropiemenu/VM Arcade.sh"
            self.assertTrue(menu_launcher.stat().st_mode & 0o100)
            self.assertEqual((home / ".config/vm-arcade").stat().st_mode & 0o777, 0o700)
            self.assertFalse((home / ".config/vm-arcade/config.json").exists())

    def test_unrelated_launcher_is_preserved(self):
        for location in ("RetroPie/roms/ports", "RetroPie/retropiemenu"):
            with self.subTest(location=location), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp)
                folder = home / location
                folder.mkdir(parents=True)
                (home / "RetroPie/roms").mkdir(exist_ok=True)
                launcher = folder / "VM Arcade.sh"
                launcher.write_text("user-owned content")
                with self.assertRaisesRegex(SystemExit, "Existing unrelated launcher"):
                    self.run_install(home)
                self.assertEqual(launcher.read_text(), "user-owned content")
                self.assertFalse((home / ".local/share/vm-arcade/current").exists())
