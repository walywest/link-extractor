# FitFast Downloader
#
#   make tui           run the terminal UI (see devenv.nix, NIXOS.md)
#   make web           run the web UI that hands out direct links (see WEB.md)

.DEFAULT_GOAL := tui

.PHONY: tui web

tui:
	devenv shell -- python fitfast_tui.py

web:
	devenv shell -- python fitfast_web.py
