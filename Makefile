# FitFast Downloader
#
#   make tui           Linux/NixOS terminal UI (see devenv.nix, NIXOS.md)
#   make windows-run   Windows GUI, launched on the Windows host from WSL
#
# `windows-run` only makes sense when this Makefile is invoked from inside
# WSL against a Windows host reachable via `powershell.exe`/wslpath — see
# the "Running it locally" notes for that flow. On a plain Linux box (no
# WSL), just use `make tui`.

.DEFAULT_GOAL := help

.PHONY: help tui windows-run

help:
	@echo "make tui           run the terminal UI (devenv shell + fitfast_tui.py)"
	@echo "make windows-run   launch the Windows GUI on the Windows host via WSL"

tui:
	devenv shell -- python fitfast_tui.py

windows-run:
	@command -v wslpath >/dev/null 2>&1 || { echo "windows-run needs WSL (wslpath not found)"; exit 1; }
	@command -v powershell.exe >/dev/null 2>&1 || { echo "windows-run needs WSL interop (powershell.exe not found)"; exit 1; }
	powershell.exe -NoProfile -Command "Start-Process -FilePath pythonw.exe -ArgumentList '$(shell wslpath -w $(CURDIR)/fitfast.py)'"
