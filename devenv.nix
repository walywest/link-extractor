{ pkgs, lib, ... }:

# Dev environment for the FitFast terminal UI on Linux / NixOS.
#   devenv shell          # enter the environment
#   camoufox sync         # refresh the version list (see note below)
#   camoufox fetch        # one-time: download the stealth browser
#   python fitfast_tui.py # launch the TUI
#
# The Windows GUI (customtkinter + bundled .exe binaries) is NOT covered here —
# this shell targets the cross-platform TUI in app/tui.py.

let
  # Runtime libraries needed by the two dynamically-linked binaries FitFast
  # pulls at runtime: Camoufox's patched Firefox and Playwright's node driver.
  runtimeLibs = with pkgs; [
    stdenv.cc.cc.lib            # libstdc++ / libgcc  (node driver + firefox)
    zlib glib nss nspr dbus atk cups libdrm expat libxkbcommon
    gtk3 pango cairo gdk-pixbuf
    fontconfig freetype
    alsa-lib mesa
    xorg.libX11 xorg.libXcomposite xorg.libXdamage xorg.libXext
    xorg.libXfixes xorg.libXrandr xorg.libXrender xorg.libxcb
    xorg.libXtst xorg.libXi
  ];
in
{
  packages = [
    pkgs.aria2   # multi-connection downloader FitFast shells out to
    pkgs.unar    # free RAR5 / multi-volume extractor for the auto-extract step
    pkgs.ffmpeg-headless  # decode video recordings from the shell
  ];

  languages.python = {
    enable = true;
    # tkinter is a compiled stdlib extension (_tkinter.so); on nixpkgs it lives
    # in a separate output, so the interpreter has to be built with it or
    # `import turtle` / `import tkinter` fails with ModuleNotFoundError.
    package = pkgs.python3.withPackages (ps: [ ps.tkinter ]);
    venv = {
      enable = true;
      requirements = ''
        requests>=2.31
        camoufox[geoip]
        textual>=0.60
      '';
    };
  };

  # ── Making the downloaded browser run on NixOS ────────────────────────────
  # Camoufox and Playwright ship prebuilt, dynamically-linked binaries. NixOS
  # has no global /usr/lib or FHS loader, so they need help finding both their
  # ELF interpreter and their shared libraries.
  #
  #   1. Enable nix-ld ONCE in your system configuration.nix (then rebuild):
  #        programs.nix-ld.enable = true;
  #      It provides the /lib64/ld-linux stub the binaries expect and reads
  #      NIX_LD_LIBRARY_PATH below.
  #   2. The two vars below supply the actual libraries.
  #
  # On a regular (non-NixOS) Linux distro nix-ld isn't needed — the system
  # loader already resolves these, and LD_LIBRARY_PATH alone is enough.
  env.NIX_LD_LIBRARY_PATH = lib.makeLibraryPath runtimeLibs;
  env.LD_LIBRARY_PATH = lib.makeLibraryPath runtimeLibs;

  enterShell = ''
    echo "FitFast — terminal edition"
    echo "  first run only:  camoufox sync && camoufox fetch   # version list + stealth browser (~1GB)"
    echo "  if resolves fail:  camoufox sync && camoufox fetch  # a stale version list pins you to an old Firefox"
    echo "  launch:          python fitfast_tui.py"
    echo ""
    aria2c --version | head -n1
    printf 'unar: '; unar -v 2>/dev/null | head -n1 || echo '(not found)'
    if [ -z "''${NIX_LD:-}" ] && [ -e /etc/NIXOS ]; then
      echo "!! nix-ld not detected — set programs.nix-ld.enable = true in configuration.nix"
      echo "   or Camoufox's browser will fail to launch."
    fi
  '';
}
