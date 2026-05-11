"""Build-time stamping. Overwritten by the CI workflow / build scripts
before PyInstaller runs. For local dev builds, the placeholder values
below result in the in-app updater being disabled (version reads as
'0.0.0-dev' which never matches a release tag).
"""
APP_VERSION = "0.0.0-dev"
GITHUB_REPO = "JoshNova1/udp-throughput-tester"
