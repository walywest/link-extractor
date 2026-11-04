# FitFast Downloader
#
#   make tui           run the terminal UI (see devenv.nix, NIXOS.md)

.DEFAULT_GOAL := tui

.PHONY: tui

tui:
	devenv shell -- python fitfast_tui.py
