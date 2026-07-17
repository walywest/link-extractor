"""FitFast — terminal UI front-end (Linux / macOS / Nix friendly).

Drives the same resolver / downloader / scraper / extractor backends as the
Windows GUI, but through a Textual TUI so there is no Tkinter or Windows
dependency. Paste a FitGirl page (or FuckingFast links), press Fetch, then
Start; downloads run through aria2c and are auto-extracted with unar/unrar.

Keys:  f Fetch · s Start · p Pause/Resume · c Cancel · i Install browser ·
       d Remove selected link · x Clear all links · q Quit
"""
from __future__ import annotations
import queue
import threading
import time
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Header, Footer, Input, Button, DataTable, RichLog, Checkbox, Label,
)

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app import config as config_mod
    from app.bootstrap import browser_ready, ensure_browser
    from app.downloader import Downloader, Aria2NotFound, Aria2RpcError
    from app.resolver import (
        Resolver, valid_url, filename_from_fragment,
        common_folder_name, unique_folder, sanitize_filename,
    )
    from app.scraper import scrape_fitgirl_page, game_title_from_links, ScrapeError, is_fitgirl_url
    from app.extractor import extract_all, find_unrar
    from app.log import setup_logging, get_logger, log_exception
    from app.format import format_bytes, format_speed, format_eta
else:
    from . import config as config_mod
    from .bootstrap import browser_ready, ensure_browser
    from .downloader import Downloader, Aria2NotFound, Aria2RpcError
    from .resolver import (
        Resolver, valid_url, filename_from_fragment,
        common_folder_name, unique_folder, sanitize_filename,
    )
    from .scraper import scrape_fitgirl_page, game_title_from_links, ScrapeError, is_fitgirl_url
    from .extractor import extract_all, find_unrar
    from .log import setup_logging, get_logger, log_exception
    from .format import format_bytes, format_speed, format_eta


class Job:
    __slots__ = ("url", "filename", "state", "gid", "error",
                 "total", "completed", "speed")

    def __init__(self, url: str, filename: str):
        self.url = url
        self.filename = filename
        self.state = "queued"      # queued|resolving|downloading|done|error
        self.gid: str | None = None
        self.error: str | None = None
        self.total = 0
        self.completed = 0
        self.speed = 0


class FitFastTUI(App):
    TITLE = "FitFast Downloader"
    SUB_TITLE = "FitGirl / FuckingFast · terminal edition"

    CSS = """
    Screen { layout: vertical; }
    #inputs { height: auto; padding: 1 1 0 1; }
    #inputs Label { color: $text-muted; }
    #buttons { height: auto; padding: 1; }
    #buttons Button { margin: 0 1 0 0; }
    #opts { height: auto; padding: 0 1; }
    #pending_label { color: $text-muted; padding: 0 1; }
    #pending { height: 8; margin: 0 1 1 1; }
    DataTable { height: 1fr; margin: 0 1; }
    RichLog { height: 8; margin: 1 1; border: round $primary; padding: 0 1; }
    Input { margin: 0 0 1 0; }
    """

    BINDINGS = [
        ("f", "fetch", "Fetch links"),
        ("s", "start", "Start"),
        ("p", "pause", "Pause/Resume"),
        ("c", "cancel", "Cancel"),
        ("i", "install", "Install browser"),
        ("d", "remove_link", "Remove link"),
        ("x", "clear_pending", "Clear links"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.config = config_mod.load()
        self.jobs: list[Job] = []
        self.jobs_lock = threading.Lock()
        self.dest_folder: Path | None = None
        self.running = False
        self.paused = False

        self.resolver: Resolver | None = None
        self.downloader: Downloader | None = None
        self.resolve_queue: "queue.Queue[Job]" = queue.Queue()
        self.shutdown_event = threading.Event()
        self.resolver_thread: threading.Thread | None = None
        self._fetch_thread: threading.Thread | None = None
        self._extract_thread: threading.Thread | None = None
        self._setup_thread: threading.Thread | None = None
        self._pending_urls: list[str] = []

    # ---- layout -----------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="inputs"):
            yield Label("FitGirl page URL (or paste FuckingFast links, space/line separated):")
            yield Input(placeholder="https://fitgirl-repacks.site/some-game/", id="url")
            yield Label("Destination folder:")
            yield Input(value=str(self.config.get("destination") or Path.home() / "Downloads"),
                        id="dest")
        with Horizontal(id="opts"):
            yield Checkbox("Auto-extract .rar when done",
                           value=bool(self.config.get("auto_extract", True)), id="autoextract")
            yield Checkbox("Delete archives after extract",
                           value=bool(self.config.get("delete_archives_after", False)), id="delete")
        with Horizontal(id="buttons"):
            yield Button("Fetch links", id="fetch", variant="primary")
            yield Button("Remove selected", id="remove")
            yield Button("Clear links", id="clear")
            yield Button("Start", id="start", variant="success")
            yield Button("Pause", id="pause", disabled=True)
            yield Button("Cancel", id="cancel", variant="error", disabled=True)
        yield Label(
            "Links queued to download (select a row, press d or Remove to drop it):",
            id="pending_label",
        )
        yield DataTable(id="pending", zebra_stripes=True, cursor_type="row")
        yield DataTable(id="jobs", zebra_stripes=True, cursor_type="row")
        yield RichLog(id="log", highlight=False, markup=False, wrap=True)
        yield Footer()

    def on_mount(self) -> None:
        try:
            setup_logging()
        except Exception:
            pass
        table = self.query_one("#jobs", DataTable)
        table.add_column("File", key="file", width=44)
        table.add_column("Status", key="status", width=16)
        table.add_column("%", key="pct", width=7)
        table.add_column("Size", key="size", width=20)
        table.add_column("Speed", key="speed", width=12)
        table.add_column("ETA", key="eta", width=9)
        pending = self.query_one("#pending", DataTable)
        pending.add_column("File", key="file", width=44)
        pending.add_column("URL", key="url")
        self.set_interval(0.5, self._poll)
        self._log("Ready. Paste a FitGirl page or FuckingFast links, then Fetch / Start.")
        if not browser_ready():
            self._log("Stealth browser not installed. Press 'i' to install it "
                      "(one-time, a few hundred MB) before fetching.")
        if find_unrar() is None:
            self._log("Note: no unar/unrar found — downloads work but auto-extract is off. "
                      "Install 'unar' to enable it.")

    # ---- small helpers ----------------------------------------------------
    def _log(self, msg: str) -> None:
        self.query_one("#log", RichLog).write(msg)

    def _row_id(self, job: Job) -> str:
        return str(id(job))

    def _set_status(self, job: Job, text: str) -> None:
        table = self.query_one("#jobs", DataTable)
        try:
            table.update_cell(self._row_id(job), "status", text)
        except Exception:
            pass

    # ---- buttons / inputs -------------------------------------------------
    def on_button_pressed(self, event: Button.Pressed) -> None:
        {"fetch": self.action_fetch, "start": self.action_start,
         "pause": self.action_pause, "cancel": self.action_cancel,
         "remove": self.action_remove_link, "clear": self.action_clear_pending}.get(
            event.button.id, lambda: None)()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter in the URL box fetches a FitGirl page, or loads direct
        # links into the review list so they can be checked/removed first.
        if event.input.id == "url":
            url = event.value.strip()
            if is_fitgirl_url(url):
                self.action_fetch()
            else:
                raw = event.value.replace(",", " ").split()
                urls = [u for u in raw if valid_url(u)]
                if urls:
                    self._load_pending(urls)
                else:
                    self._log("Paste a FitGirl page URL, or one or more FuckingFast links.")

    # ---- pending links (reviewed before Start) -----------------------------
    def _load_pending(self, urls: list[str]) -> None:
        self._pending_urls = list(urls)
        self._refresh_pending_table()
        self._log(f"Loaded {len(urls)} link(s). Review below, remove any you "
                  "don't want, then press Start.")

    def _refresh_pending_table(self) -> None:
        table = self.query_one("#pending", DataTable)
        table.clear()
        for u in self._pending_urls:
            name = filename_from_fragment(u) or "(unknown)"
            table.add_row(name, u, key=u)

    def action_remove_link(self) -> None:
        if self.running:
            return
        table = self.query_one("#pending", DataTable)
        if table.row_count == 0:
            return
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return
        url = row_key.value
        if url in self._pending_urls:
            self._pending_urls.remove(url)
        table.remove_row(row_key)
        self._log(f"Removed link: {url}")

    def action_clear_pending(self) -> None:
        if self.running or not self._pending_urls:
            return
        self._pending_urls = []
        self.query_one("#pending", DataTable).clear()
        self._log("Cleared all pending links.")

    # ---- fetch ------------------------------------------------------------
    def action_install(self) -> None:
        if self._setup_thread and self._setup_thread.is_alive():
            return
        if browser_ready():
            self._log("Stealth browser already installed.")
            return
        self._log("Installing stealth browser… this downloads a few hundred MB once.")
        self._setup_thread = threading.Thread(target=self._install_worker, daemon=True)
        self._setup_thread.start()

    def _install_worker(self) -> None:
        try:
            ensure_browser(lambda m: self.call_from_thread(self._log, m))
            self.call_from_thread(self._log, "Browser ready. You can Fetch now.")
        except Exception as e:
            tb = log_exception("installing the stealth browser", e)
            self.call_from_thread(self._log, f"Browser install failed: {e}")

    def action_fetch(self) -> None:
        if self.running:
            return
        if self._fetch_thread and self._fetch_thread.is_alive():
            return
        url = self.query_one("#url", Input).value.strip()
        if not url:
            self._log("Paste a FitGirl page URL first, then Fetch.")
            return
        if not is_fitgirl_url(url):
            self._log("That isn't a fitgirl-repacks.site URL. For direct FuckingFast "
                      "links, skip Fetch and press Start.")
            return
        if not browser_ready():
            self._log("Stealth browser not installed — press 'i' first.")
            return
        self._log(f"Fetching links from {url} (passing Cloudflare)…")
        self._fetch_thread = threading.Thread(
            target=self._fetch_worker, args=(url,), daemon=True)
        self._fetch_thread.start()

    def _fetch_worker(self, url: str) -> None:
        try:
            result = scrape_fitgirl_page(url)
        except ScrapeError as e:
            self.call_from_thread(self._log, f"Fetch failed: {e}")
            return
        except Exception as e:
            tb = log_exception("fetching FitGirl page", e)
            self.call_from_thread(self._log, f"Fetch failed: {e}")
            return
        subfolder = game_title_from_links(result.links) or result.title or ""
        self.call_from_thread(self._fetch_done, result.links, subfolder)

    def _fetch_done(self, links: list[str], subfolder: str) -> None:
        self._load_pending(links)
        if subfolder:
            self._suggested_subfolder = sanitize_filename(subfolder)
        self._log(f"Fetched {len(links)} link(s)"
                  + (f' for "{subfolder}".' if subfolder else ".")
                  + " Review below, remove any you don't want, then press Start.")

    # ---- start ------------------------------------------------------------
    def action_start(self) -> None:
        if self.running:
            return
        urls = list(self._pending_urls)
        if not urls:
            # Fall back to whatever direct links are in the URL box.
            raw = self.query_one("#url", Input).value.replace(",", " ").split()
            urls = [u for u in raw if valid_url(u)]
        if not urls:
            self._log("Nothing to download. Fetch a FitGirl page, or paste FuckingFast links.")
            return

        dest_root = Path(self.query_one("#dest", Input).value.strip()).expanduser()
        try:
            dest_root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._log(f"Destination not writable: {e}")
            return

        names = [filename_from_fragment(u) or "" for u in urls]
        subfolder = getattr(self, "_suggested_subfolder", "") or common_folder_name(
            [n for n in names if n])
        try:
            self.dest_folder = unique_folder(dest_root, subfolder)
        except OSError as e:
            self._log(f"Cannot create subfolder: {e}")
            return

        self.config["destination"] = str(dest_root)
        self.config["auto_extract"] = self.query_one("#autoextract", Checkbox).value
        self.config["delete_archives_after"] = self.query_one("#delete", Checkbox).value
        config_mod.save(self.config)

        try:
            self.downloader = Downloader(
                connections_per_file=int(self.config.get("connections_per_file", 16)),
                concurrent_files=int(self.config.get("concurrent_files", 3)),
            )
            self.downloader.start()
        except (Aria2NotFound, Aria2RpcError) as e:
            self._log(f"Could not start aria2c: {e}")
            self.downloader = None
            return

        table = self.query_one("#jobs", DataTable)
        table.clear()
        with self.jobs_lock:
            self.jobs.clear()
            for i, u in enumerate(urls):
                guess = filename_from_fragment(u) or f"part{i+1:02d}.bin"
                job = Job(u, guess)
                self.jobs.append(job)
                table.add_row(guess, "Queued", "", "", "", "", key=self._row_id(job))

        self.resolver = Resolver()
        self.shutdown_event.clear()
        with self.jobs_lock:
            for job in self.jobs:
                self.resolve_queue.put(job)
        self.resolver_thread = threading.Thread(target=self._resolver_loop, daemon=True)
        self.resolver_thread.start()

        self.running = True
        self.paused = False
        self._pending_urls = []
        self.query_one("#pending", DataTable).clear()
        self._set_controls(running=True)
        self._log(f"Downloading {len(self.jobs)} file(s) → {self.dest_folder}")

    def _resolver_loop(self) -> None:
        while not self.shutdown_event.is_set():
            try:
                job = self.resolve_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if self.shutdown_event.is_set():
                return
            self.call_from_thread(self._set_status, job, "Resolving…")
            job.state = "resolving"
            try:
                signed_url, filename = self.resolver.resolve(job.url)
            except Exception as e:
                job.state = "error"
                job.error = str(e)
                self.call_from_thread(self._set_status, job, "Resolve error")
                continue
            job.filename = filename
            self.call_from_thread(self._rename_row, job)
            try:
                job.gid = self.downloader.add(signed_url, str(self.dest_folder), filename)
            except Exception as e:
                job.state = "error"
                job.error = str(e)
                self.call_from_thread(self._set_status, job, "aria2 error")
                continue
            job.state = "downloading"
            self.call_from_thread(self._set_status, job, "Starting…")

    def _rename_row(self, job: Job) -> None:
        table = self.query_one("#jobs", DataTable)
        try:
            table.update_cell(self._row_id(job), "file", job.filename)
        except Exception:
            pass

    # ---- poll -------------------------------------------------------------
    def _poll(self) -> None:
        if not self.running or self.downloader is None:
            return
        table = self.query_one("#jobs", DataTable)
        with self.jobs_lock:
            jobs = list(self.jobs)
        agg_speed = 0
        done = err = 0
        total_c = total_s = 0
        for job in jobs:
            if job.state in ("done", "error"):
                done += job.state == "done"
                err += job.state == "error"
                continue
            if job.gid is None:
                continue
            try:
                st = self.downloader.status(job.gid)
            except Exception:
                continue
            job.total = int(st.get("totalLength") or 0)
            job.completed = int(st.get("completedLength") or 0)
            job.speed = int(st.get("downloadSpeed") or 0)
            status = st.get("status", "")
            agg_speed += job.speed
            total_c += job.completed
            total_s += job.total
            rid = self._row_id(job)
            frac = (job.completed / job.total) if job.total else 0
            try:
                table.update_cell(rid, "pct", f"{frac*100:.1f}%" if job.total else "")
                table.update_cell(rid, "size",
                                  f"{format_bytes(job.completed)}/{format_bytes(job.total)}"
                                  if job.total else "")
                table.update_cell(rid, "speed", format_speed(job.speed) if job.speed else "")
                if job.speed > 0 and job.total:
                    eta = (job.total - job.completed) / job.speed
                    table.update_cell(rid, "eta", format_eta(eta))
                if status == "active":
                    table.update_cell(rid, "status", "Downloading")
                elif status == "complete":
                    job.state = "done"
                    table.update_cell(rid, "status", "Done")
                    table.update_cell(rid, "pct", "100%")
                    table.update_cell(rid, "eta", "")
                elif status == "error":
                    job.state = "error"
                    job.error = st.get("errorMessage") or "aria2 error"
                    table.update_cell(rid, "status", "Error")
                elif status == "paused":
                    table.update_cell(rid, "status", "Paused")
                elif status == "waiting":
                    table.update_cell(rid, "status", "Waiting")
            except Exception:
                pass

        self.sub_title = (
            f"{done}/{len(jobs)} done"
            + (f" · {err} failed" if err else "")
            + f" · {format_bytes(total_c)}/{format_bytes(total_s)}"
            + f" · {format_speed(agg_speed)}"
        )
        active = len(jobs) - done - err
        if active == 0 and self.running:
            self._all_finished(done, err)

    def _all_finished(self, done: int, err: int) -> None:
        self.running = False
        self._set_controls(running=False)
        self._log(f"All done — {done} ok"
                  + (f", {err} failed" if err else "")
                  + f". Saved to {self.dest_folder}")
        self.shutdown_event.set()
        if self.resolver is not None:
            threading.Thread(target=self.resolver.close, daemon=True).start()
            self.resolver = None
        d = self.downloader
        if d is not None:
            threading.Thread(target=d.stop, daemon=True).start()
            self.downloader = None

        if (done > 0 and self.dest_folder is not None
                and self.query_one("#autoextract", Checkbox).value
                and find_unrar() is not None):
            self._log("Extracting archives…")
            self._extract_thread = threading.Thread(
                target=self._extract_worker,
                args=(self.dest_folder, self.query_one("#delete", Checkbox).value),
                daemon=True)
            self._extract_thread.start()

    def _extract_worker(self, folder: Path, delete_after: bool) -> None:
        def on_start(name, i, total):
            self.call_from_thread(self._log, f"Extracting [{i}/{total}] {name}…")
        try:
            ok, fail = extract_all(folder, delete_after=delete_after, on_set_start=on_start)
            msg = f"Extraction complete: {ok} set(s) unpacked"
            if fail:
                msg += f", {fail} failed"
            self.call_from_thread(self._log, msg + f". Ready in {folder}")
        except Exception as e:
            log_exception("extracting archives", e)
            self.call_from_thread(self._log, f"Extraction error: {e}")

    # ---- pause / cancel ---------------------------------------------------
    def action_pause(self) -> None:
        if self.downloader is None:
            return
        try:
            if self.paused:
                self.downloader.unpause_all()
                self.paused = False
                self.query_one("#pause", Button).label = "Pause"
                self._log("Resumed.")
            else:
                self.downloader.pause_all()
                self.paused = True
                self.query_one("#pause", Button).label = "Resume"
                self._log("Paused.")
        except Exception as e:
            self._log(f"Pause/resume failed: {e}")

    def action_cancel(self) -> None:
        if not self.running:
            return
        self.shutdown_event.set()
        if self.downloader is not None:
            try:
                self.downloader.pause_all()
            except Exception:
                pass
            with self.jobs_lock:
                for job in self.jobs:
                    if job.gid:
                        try:
                            self.downloader.remove(job.gid)
                        except Exception:
                            pass
        done = sum(1 for j in self.jobs if j.state == "done")
        err = sum(1 for j in self.jobs if j.state == "error")
        self.running = False
        self._set_controls(running=False)
        self._log("Cancelled. Partial files kept — paste the same links to resume.")

    def _set_controls(self, running: bool) -> None:
        self.query_one("#fetch", Button).disabled = running
        self.query_one("#start", Button).disabled = running
        self.query_one("#remove", Button).disabled = running
        self.query_one("#clear", Button).disabled = running
        self.query_one("#pause", Button).disabled = not running
        self.query_one("#cancel", Button).disabled = not running

    def action_quit(self) -> None:
        self.shutdown_event.set()
        try:
            if self.downloader is not None:
                self.downloader.stop()
        except Exception:
            pass
        try:
            if self.resolver is not None:
                self.resolver.close()
        except Exception:
            pass
        self.exit()


def main() -> int:
    try:
        setup_logging()
    except Exception:
        pass
    FitFastTUI().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
