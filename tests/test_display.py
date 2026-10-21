"""Exercise screenshot allocation against a real larger SDL framebuffer."""
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest


class ScreenshotTests(unittest.TestCase):
    def test_screenshot_uses_output_pixels_instead_of_logical_canvas(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'larger.bmp'
            program = '''import sys
from vm_arcade.display import Display, sdl
d = Display(False)
try:
    sdl.SDL_SetWindowSize(d.window, 1024, 768)
    d.color((8, 17, 20))
    sdl.SDL_RenderClear(d.renderer)
    sdl.SDL_RenderPresent(d.renderer)
    d.save(sys.argv[1])
finally:
    d.close()
'''
            result = subprocess.run([sys.executable, '-c', program, str(target)],
                                    env=dict(os.environ, SDL_VIDEODRIVER='dummy'),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            data = target.read_bytes()
            self.assertEqual(data[:2], b'BM')
            width, height = struct.unpack_from('<ii', data, 18)
            self.assertEqual((width, abs(height)), (1024, 768))
