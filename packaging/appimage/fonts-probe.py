"""Runs inside the AppImage in place of main.py (smoke-fonts.sh binds it there).

Loads the image's own libfontconfig under AppRun's environment and prints
how many fonts it sees, how many of those come from the image, and which of
them have U+29C9. libfontconfig is reached through ctypes so no Qt and no
display are involved.
"""
import ctypes
import os
import sys

fc = ctypes.CDLL("libfontconfig.so.1")
fc.FcInitLoadConfigAndFonts.restype = ctypes.c_void_p
fc.FcConfigGetFonts.restype = ctypes.c_void_p
fc.FcConfigGetFonts.argtypes = [ctypes.c_void_p, ctypes.c_int]
fc.FcPatternGetString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
fc.FcPatternGetCharSet.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
fc.FcCharSetHasChar.argtypes = [ctypes.c_void_p, ctypes.c_uint32]


class FontSet(ctypes.Structure):
    _fields_ = [("nfont", ctypes.c_int), ("sfont", ctypes.c_int),
                ("fonts", ctypes.POINTER(ctypes.c_void_p))]


config = fc.FcInitLoadConfigAndFonts()
fonts = ctypes.cast(fc.FcConfigGetFonts(config, 0), ctypes.POINTER(FontSet)).contents
appdir = os.environ["APPDIR"]
files, glyph = [], []
for i in range(fonts.nfont):
    path = ctypes.c_char_p()
    fc.FcPatternGetString(fonts.fonts[i], b"file", 0, ctypes.byref(path))
    files.append(path.value.decode())
    charset = ctypes.c_void_p()
    if fc.FcPatternGetCharSet(fonts.fonts[i], b"charset", 0, ctypes.byref(charset)) == 0 \
            and fc.FcCharSetHasChar(charset, 0x29C9):
        glyph.append(os.path.basename(files[-1]))
print(f"FONTCONFIG_FILE={os.environ.get('FONTCONFIG_FILE')}")
print(f"FcConfigGetFonts nfont={fonts.nfont}")
print(f"from-image={sum(f.startswith(appdir) for f in files)}")
print(f"U+29C9-in={','.join(sorted(glyph)) or 'none'}")
sys.exit(0)
