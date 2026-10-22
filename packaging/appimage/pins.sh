# Every external input of the AppImage build, pinned to a fixed URL and a
# SHA-256 that build.sh checks before using the file. Sourced by build.sh
# and inner-build.sh; nothing here is fetched from a `latest`/`continuous`
# URL. To bump one, change the URL and the hash together and rebuild.

# Playwright is pinned exactly; its Chromium revision is whatever that
# release's browsers.json names, and inner-build.sh records the revision
# and the archive SHA-256 in the build log and checks the latter.
PLAYWRIGHT_VERSION="1.64.0"
# Chrome for Testing archive Playwright 1.64.0 downloads for Chromium revision 1248
# (cdn.playwright.dev/builds/cft/<browserVersion>/linux64/chrome-linux64.zip).
CHROMIUM_ZIP_SHA256="43fd31766c7533a02dda2ded5ad50c2f861cd5400be7509c0abad71129f79d79"

# Ubuntu 22.04 root filesystem the AppImage is built in (glibc 2.35 floor).
UBUNTU_BASE_URL="https://cdimage.ubuntu.com/ubuntu-base/releases/22.04/release/ubuntu-base-22.04.5-base-amd64.tar.gz"
UBUNTU_BASE_SHA256="242cd8898b33ea806ef5f13b1076ed7c76f9f989d18384452f7166692438ff1a"

# Relocatable CPython 3.13 (python-build-standalone).
PBS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20261021/cpython-3.13.17%2B20261021-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz"
PBS_SHA256="f829adc9d888e76c8801b4799ed444188d35239fcae3a9808a99ec5cb0d8d92c"

# appimagetool 1.9.1 (a release, not `continuous`) and the static type-2
# runtime (no libfuse2 needed on the user's machine).
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage"
APPIMAGETOOL_SHA256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
RUNTIME_URL="https://github.com/AppImage/type2-runtime/releases/download/20251108/runtime-x86_64"
RUNTIME_SHA256="2fca8b443c92510f1483a883f60061ad09b46b978b2631c807cd873a47ec260d"

# AppImage project's excludelist (libraries that must come from the host),
# pinned to a commit of AppImageCommunity/pkg2appimage.
EXCLUDELIST_URL="https://raw.githubusercontent.com/AppImageCommunity/pkg2appimage/15a64c20dc23a0154622ba25829364323903b6b5/excludelist"
EXCLUDELIST_SHA256="50db0f894f34b169c47a5cbc0c17dbab61e9edebca5bc8269a1e6ac1bf4bdad9"
